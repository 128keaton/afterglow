"""Reading and changing the text an 880 image draws.

Text is stored as glyph codes, and a code means what the image's own font table says it
draws (glyphs.py), so every operation here goes through that image's reading and never
through a table of codes carried from another one.

``find`` is every string the image draws that reads as some text. ``rename`` changes it where
it is drawn - written over in place when the new text fits, drawn from text added before the
trailer when it does not - and seals the image again, so what comes back passes the start
check. What it cannot do safely it leaves alone, whole, and says so.

Where a label sits matters as much as what it says. On a page of cells a left-hand label
starts at x = 3 and a right-hand label is *right*-aligned: it ends at x = 125, so it starts at
125 minus the width of its glyphs. That held for every label of every Devices page in the
sixteen images, 121 of them. A new text of another width therefore has to move its right-hand
label too, and ``rename`` does.

Format reference: docs/harmony_880/fonts.md
"""

from __future__ import annotations

from dataclasses import dataclass

from . import font, glyphs, screens, tptp


LEFT_EDGE = 3
RIGHT_EDGE = 125


@dataclass(frozen=True)
class Reading:
    """An image's text and the means to read and write it."""
    payload: bytes
    sets: list
    maps: dict[int, glyphs.CodeMap]          # by font set
    runs: list[screens.TextRun]
    widths: dict[int, dict[int, int]]        # pixel width of each glyph, by font set and code

    def text(self, run: screens.TextRun) -> str:
        return self.maps[run.font].decode(run.codes) if run.font in self.maps else ""

    def pixels(self, font_set: int, codes) -> int:
        """How wide ``codes`` draw in a font set."""
        table = self.widths[font_set]
        return sum(table.get(code, 0) for code in codes)

    def alignment(self, run: screens.TextRun) -> str:
        """``left`` for a label at the left edge, ``right`` for one that ends at the right
        edge, ``other`` for anything else."""
        if run.x == LEFT_EDGE:
            return "left"
        if run.x + self.pixels(run.font, run.codes) == RIGHT_EDGE:
            return "right"
        return "other"


def read(payload: bytes) -> Reading:
    sets = font.font_sets(payload)
    runs, _report = screens.text_runs(payload)
    maps = {fs.index: glyphs.code_map(payload, sets=[fs]) for fs in sets}
    widths = {fs.index: {code: font.decode_glyph(payload, fs.offset(code)).width
                         for code in fs.codes()} for fs in sets}
    return Reading(payload, sets, maps, [run for run in runs if run.font in maps], widths)


def find(reading: Reading, text: str) -> list[screens.TextRun]:
    """The runs that read exactly as ``text``, one for each place the codes stand."""
    seen, found = set(), []
    for run in reading.runs:
        if run.at not in seen and reading.text(run) == text:
            seen.add(run.at)
            found.append(run)
    return found


def _shared(reading: Reading, run: screens.TextRun) -> bool:
    """Whether another string's bytes overlap this one's: two pointers can name the same
    terminated run at different starts, and then a change to one is a change to both."""
    end = run.at + len(run.codes) + 1
    return any(other.at != run.at and other.at < end and other.at + len(other.codes) + 1 > run.at
               for other in reading.runs)


def _filler(run: screens.TextRun, room: int, font_set: int) -> bytes | None:
    """The bytes that make the room of an inline string harmless once its instruction has
    become a six-byte reference, or None if there is no way to: the room is ``room`` bytes
    and the instruction takes six of the ``room + 4`` it had."""
    left = room - 2
    if left == 0:
        return b""
    if left == 2:
        return bytes([screens.SELECT_FONT, font_set])
    if left >= 4:
        after = tptp.BASE + run.instruction + room + 4
        return bytes([20]) + after.to_bytes(3, "little") + bytes(left - 4)
    return None


def _plan(reading: Reading, run: screens.TextRun, new: str):
    """What it takes to draw ``new`` everywhere the string of ``run`` is drawn: a reason it
    cannot be done (a string), or ``(operations, counts)``. Nothing is changed here, so a
    string that cannot be done wholly is not done in part."""
    code_map = reading.maps[run.font]
    exact = code_map.encode(new)
    if exact is None:
        missing = sorted({char for char in new if not code_map.codes_for(char)})
        return f"{new!r}: this font draws no {', '.join(map(repr, missing))}"
    if any(code > 0x7F or code == 0 for code in exact):
        return f"a code of {new!r} is written in two bytes, which is not handled"
    if _shared(reading, run):
        return f"{run.at:#x} shares its bytes with another string"
    drawn_by = [other for other in reading.runs if other.at == run.at]
    width = reading.pixels(run.font, exact)
    operations, counts = [], {"place": 0, "grown": 0, "pointed": 0, "moved": 0}

    def edge(other, pixels):
        if reading.alignment(other) != "right":
            return other.x
        x = RIGHT_EDGE - pixels
        return x if x >= 0 else None

    for other in (r for r in drawn_by if r.kind == "reference"):
        x = edge(other, width)
        if x is None:
            return f"{new!r} is {width} pixels wide and does not fit before the right edge"
        operations.append(("point", other.instruction, x, exact))
        counts["pointed"] += 1
        counts["moved"] += x != other.x
    for other in (r for r in drawn_by if r.kind == "inline"):
        room = len(other.codes)
        if len(exact) > room:
            filler = _filler(other, room, other.font)
            if filler is None:
                return (f"it has room for {room} characters and {new!r} needs {len(exact)}: a "
                        "string that short cannot be grown")
            x = edge(other, width)
            if x is None:
                return f"{new!r} is {width} pixels wide and does not fit before the right edge"
            operations.append(("convert", other.instruction, x, other.y, exact, room, filler))
            counts["grown"] += 1
        else:
            right = reading.alignment(other) == "right"
            codes = exact
            if len(exact) < room:
                space = code_map.encode(" ")
                if space is None:
                    return f"this font draws no space to pad {new!r} with"
                padding = space * (room - len(exact))
                codes = padding + exact if right else exact + padding
            x = edge(other, reading.pixels(run.font, codes))
            if x is None:
                return f"{new!r} does not fit before the right edge"
            operations.append(("write", other.at, other.instruction, x, codes))
            counts["place"] += 1
        counts["moved"] += x != other.x
    return operations, counts


def rename(payload: bytes, renames: dict[str, str]) -> tuple[bytes, list[str]]:
    """Write each new text over the old where the image draws it. Returns the new image
    and a note for everything that was done or not done.

    A page's strings are stored once, drawn inline by one instruction and referenced by
    others, and the pages that share a string can draw it at different edges (a device sits
    at the left of one copy of the Devices page and the right of another). So, for each
    string that reads exactly as the old text:

    - each reference is pointed at a copy of the new text added before the trailer, exactly
      as long as it is and, at the right edge, with the x that keeps it ending there;
    - the inline string is written over in place when the new text fits, padded with spaces
      after it at the left edge and before it, with a new x, at the right edge;
    - when it does not fit, the inline instruction (``05 x y codes 00``, four bytes more than
      its codes) becomes a reference like the others (``04 x y u24``, six bytes), and what is
      left of its room is made harmless: nothing for a two-character string, a ``10 font``
      (select the font already in force) for four, a jump over it for six or more. A string of
      one, three or five characters cannot be grown;
    - a string that cannot be done wholly - a glyph the font lacks, a text too wide for the
      right edge, bytes shared with another string, too short to grow - is not done in part:
      it is left as it is and the note says why.

    The text added goes before the trailer, so nothing moves, and the image is sealed again.
    """
    if not renames:
        return payload, []
    if bytes(payload[-4:]) != tptp.CLOSE:
        raise ValueError("the image has lost its closing DKDK; not editing it")
    reading = read(payload)
    data = bytearray(payload[:-6])
    added = bytearray()
    where: dict[bytes, int] = {}                    # text added so far, by its codes
    notes = []

    def append(codes) -> int:
        key = bytes(codes)
        if key not in where:
            where[key] = tptp.BASE + len(data) + len(added)
            added.extend(key + b"\x00")
        return where[key]

    for old, new in renames.items():
        runs = find(reading, old)
        if not runs:
            notes.append(f"{old!r}: not drawn as a whole string in the image, left as it is")
            continue
        total = {"place": 0, "grown": 0, "pointed": 0, "moved": 0}
        for run in runs:
            plan = _plan(reading, run, new)
            if isinstance(plan, str):
                notes.append(f"{old!r} at {run.at:#x}: {plan}, left as it is")
                continue
            operations, counts = plan
            for operation in operations:
                if operation[0] == "point":
                    _kind, instruction, x, codes = operation
                    data[instruction + 1] = x
                    data[instruction + 3:instruction + 6] = append(codes).to_bytes(3, "little")
                elif operation[0] == "convert":
                    _kind, instruction, x, y, codes, room, filler = operation
                    data[instruction:instruction + 6] = bytes([screens.TEXT_AT, x, y]) \
                        + append(codes).to_bytes(3, "little")
                    data[instruction + 6:instruction + room + 4] = filler
                else:
                    _kind, at, instruction, x, codes = operation
                    data[at:at + len(codes)] = bytes(codes)
                    data[instruction + 1] = x
            for key, n in counts.items():
                total[key] += n
        if total["place"] or total["grown"] or total["pointed"]:
            parts = [f"{total['place']} written in place", f"{total['grown']} grown into a "
                     f"reference", f"{total['pointed']} references pointed at the new text"]
            extra = ", ".join(part for part, key in zip(parts, ("place", "grown", "pointed"))
                              if total[key])
            if total["moved"]:
                extra += f", {total['moved']} moved to stay at the right edge"
            notes.append(f"{old!r} -> {new!r} ({extra})")
    return tptp.seal(bytes(data) + bytes(added)), notes
