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
from .decompiler import hconfig


LEFT_EDGE = 3
RIGHT_EDGE = 125
# A cell of the Devices page is 64 pixels wide and the widest label of the sixteen images
# draws 57. Two lines of a label are 13 pixels apart; a label of one line sits on the lower.
CELL_ROOM = 58
LINE_STEP = 13
FIRST_CELL_Y = 20
LAST_CELL_Y = 150


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


@dataclass(frozen=True)
class Cell:
    """One device's label on the Devices page: a line, or two when the name is wrapped
    (``Digital Set`` over ``Top Box``). Each line is a string of its own, drawn by every copy
    of the page, so a cell is known by where its strings are."""
    lines: tuple[screens.TextRun, ...]

    @property
    def key(self) -> tuple[int, ...]:
        return tuple(run.at for run in self.lines)

    def text(self, reading: Reading) -> str:
        return " ".join(reading.text(run).strip() for run in self.lines).strip()


def cells(reading: Reading) -> list[Cell]:
    """The cells of every copy of the Devices page, each string pair once."""
    pages = {run.program for run in reading.runs if reading.text(run) == "Devices"}
    found: dict[tuple[int, ...], Cell] = {}
    for program in sorted(pages):
        shown = [run for run in reading.runs
                 if run.program == program and FIRST_CELL_Y <= run.y < LAST_CELL_Y]
        for column in ("left", "right"):
            stack = sorted((run for run in shown if reading.alignment(run) == column),
                           key=lambda run: run.y)
            index = 0
            while index < len(stack):
                run = stack[index]
                below = stack[index + 1] if index + 1 < len(stack) else None
                if below is not None and below.y - run.y == LINE_STEP:
                    cell, index = Cell((run, below)), index + 2
                else:
                    cell, index = Cell((run,)), index + 1
                found.setdefault(cell.key, cell)
    return list(found.values())


def _names_cell(cell_text: str, old: str) -> bool:
    """Whether a cell draws the device called ``old``: its whole name, or the bracketed
    end of it - the page draws ``(DVD,`` over ``CD, Radio)`` for ``Mini System (DVD, CD,
    Radio)`` and never the words before the bracket."""
    return bool(cell_text) and (cell_text == old or (
        cell_text.startswith("(") and old.endswith(" " + cell_text)))


def _wrap(reading: Reading, text: str, face: int, room: int = CELL_ROOM) -> list[str] | str:
    """``text`` broken between its words into at most two lines that each fit ``room``, as
    the cell draws a long name, or the reason it cannot be."""
    code_map = reading.maps[face]
    missing = sorted({char for char in text if not code_map.codes_for(char)})
    if missing:
        return f"{text!r}: this font draws no {', '.join(map(repr, missing))}"

    def width(line: str) -> int:
        return reading.pixels(face, code_map.encode(line))

    lines: list[str] = []
    for word in text.split():
        if lines and width(f"{lines[-1]} {word}") <= room:
            lines[-1] += f" {word}"
        else:
            lines.append(word)
    if not lines:
        return "there is no text to draw"
    for line in lines:
        if width(line) > room:
            return f"{line!r} is {width(line)} pixels wide and does not fit in the {room} a cell holds"
    if len(lines) > 2:
        return f"{text!r} needs {len(lines)} lines and a cell has two, and does not fit"
    return lines


def _same_face(reading: Reading, first: int, second: int) -> bool:
    """Whether two font sets draw the same face: every character both have is the same
    picture. The sets of an image are cut down to the characters its pages use, so the
    smaller can borrow what the larger has."""
    sets = {fs.index: fs for fs in reading.sets}
    if first not in sets or second not in sets or sets[first].height != sets[second].height:
        return False
    maps = reading.maps[first], reading.maps[second]
    shared = ({c for c in maps[0].chars.values() if c} & {c for c in maps[1].chars.values() if c})
    for char in shared:
        glyphs_ = [font.decode_glyph(reading.payload, sets[index].offset(code_map.codes_for(char)[0]))
                   for index, code_map in zip((first, second), maps)]
        if glyphs_[0].width != glyphs_[1].width or glyphs_[0].rows != glyphs_[1].rows:
            return False
    return len(shared) >= 8


def _faces(reading: Reading, run: screens.TextRun) -> list[int]:
    """The font sets a string may be drawn in: its own first, then those that draw the same
    face with more of the alphabet."""
    others = [index for index in reading.maps
              if index != run.font and _same_face(reading, run.font, index)]
    others.sort(key=lambda index: -len(reading.maps[index].chars))
    return [run.font, *others]


def _select_font(reading: Reading, run: screens.TextRun) -> int | None:
    """The offset of the ``10 n`` that chooses the font ``run`` is drawn in, when changing it
    changes that string and nothing else: no other text between it and the string, and the
    next text after it chooses its own font."""
    program = hconfig.parse_arch9_screen_program(reading.payload, run.program)
    if not program:
        return None
    at = next((i for i, step in enumerate(program) if step["offset"] == run.instruction), None)
    if at is None:
        return None
    select = None
    for step in reversed(program[:at]):
        if step["opcode"] in (screens.TEXT_AT, screens.TEXT_INLINE):
            return None
        if step["opcode"] == screens.SELECT_FONT:
            select = step
            break
    if select is None or select["operands"][0] != run.font:
        return None
    for step in program[at + 1:]:
        if step["opcode"] in (screens.SELECT_FONT, 0):
            break
        if step["opcode"] in (screens.TEXT_AT, screens.TEXT_INLINE) or step["targets"]:
            return None
    return select["offset"]


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


def _plan(reading: Reading, run: screens.TextRun, new: str, limit: int | None = None,
          face: int | None = None):
    """What it takes to draw ``new`` everywhere the string of ``run`` is drawn: a reason it
    cannot be done (a string), or ``(operations, counts)``. Nothing is changed here, so a
    string that cannot be done wholly is not done in part.

    ``limit`` is the most pixels it may take (a cell of the Devices page). ``face`` is a font
    set that draws the same face as the string's own but has the letters it lacks: each
    place that draws the string then chooses that set instead."""
    face = run.font if face is None else face
    code_map = reading.maps[face]
    exact = code_map.encode(new)
    if exact is None:
        missing = sorted({char for char in new if not code_map.codes_for(char)})
        return f"{new!r}: this font draws no {', '.join(map(repr, missing))}"
    if any(code > 0x7F or code == 0 for code in exact):
        return f"a code of {new!r} is written in two bytes, which is not handled"
    if _shared(reading, run):
        return f"{run.at:#x} shares its bytes with another string"
    drawn_by = [other for other in reading.runs if other.at == run.at]
    width = reading.pixels(face, exact)
    if limit is not None and width > limit:
        return f"{new!r} is {width} pixels wide and does not fit in the {limit} a cell holds"
    operations, counts = [], {"place": 0, "grown": 0, "pointed": 0, "moved": 0}
    if face != run.font:
        for other in drawn_by:
            select = _select_font(reading, other) if other.font == run.font else None
            if select is None:
                return (f"{new!r} needs font set {face}, and the font cannot be changed where "
                        f"{other.at:#x} is drawn at ({other.x}, {other.y})")
            if ("font", select, face) not in operations:
                operations.append(("font", select, face))

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
            filler = _filler(other, room, face)
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
            x = edge(other, reading.pixels(face, codes))
            if x is None:
                return f"{new!r} does not fit before the right edge"
            operations.append(("write", other.at, other.instruction, x, codes))
            counts["place"] += 1
        counts["moved"] += x != other.x
    return operations, counts


def _plan_line(reading: Reading, run: screens.TextRun, text: str):
    """One line of a cell: the plan in the first font set that draws it, or why none can."""
    first = None
    for face in _faces(reading, run):
        plan = _plan(reading, run, text, CELL_ROOM, face)
        if not isinstance(plan, str):
            return plan
        first = first or plan
    return first


def _plan_cell(reading: Reading, cell: Cell, new: str):
    """``[(run, text, plan)]`` for each line of a cell, or why the cell cannot be redrawn.

    The name is wrapped to the cell as the page wraps a long one. Text that fits one line goes
    on the lower line, where the page puts a one-line name, and a second line the old name
    had is blanked. A name that needs two lines in a cell that has one cannot be drawn: the
    page has nothing there to draw a line with, and a line cannot be added without moving
    what follows it. Every line is planned before any is written."""
    lines = first_error = None
    for face in _faces(reading, cell.lines[-1]):
        got = _wrap(reading, new, face)
        if isinstance(got, str):
            first_error = first_error or got
        else:
            lines = got
            break
    if lines is None:
        return first_error
    if len(lines) > len(cell.lines):
        return (f"{new!r} needs two lines ({' / '.join(lines)}) and the cell has one "
                "to draw it on, and does not fit")
    texts = [""] * (len(cell.lines) - len(lines)) + lines
    plans = []
    for run, text in zip(cell.lines, texts):
        plan = _plan_line(reading, run, text)
        if isinstance(plan, str):
            return plan
        plans.append((run, text, plan))
    return plans


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

    def apply(operations) -> None:
        for operation in operations:
            if operation[0] == "font":
                _kind, select, face = operation
                data[select + 1] = face
            elif operation[0] == "point":
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

    def summary(counts: dict[str, int]) -> str:
        parts = [f"{counts['place']} written in place", f"{counts['grown']} grown into a "
                 f"reference", f"{counts['pointed']} references pointed at the new text"]
        extra = ", ".join(part for part, key in zip(parts, ("place", "grown", "pointed"))
                          if counts[key])
        if counts["moved"]:
            extra += f", {counts['moved']} moved to stay at the right edge"
        return extra

    pages = cells(reading)
    in_a_cell = {run.at for cell in pages for run in cell.lines}
    for old, new in renames.items():
        named = [cell for cell in pages if _names_cell(cell.text(reading), old)]
        handled = {run.at for cell in named for run in cell.lines}
        for cell in named:
            plans = _plan_cell(reading, cell, new)
            where_ = f"the Devices page cell at {cell.lines[0].at:#x}"
            if isinstance(plans, str):
                notes.append(f"{old!r} in {where_}: {plans}, left as it is")
                continue
            total = {"place": 0, "grown": 0, "pointed": 0, "moved": 0}
            for _run, _text, (operations, counts) in plans:
                apply(operations)
                for key, n in counts.items():
                    total[key] += n
            drawn = [text for _run, text, _how in plans if text]
            notes.append(f"{old!r} -> {new!r} on {len(drawn)} line(s) as "
                         f"{' over '.join(map(repr, drawn))} ({summary(total)})")
        runs = [run for run in find(reading, old) if run.at not in handled]
        if not runs and not named:
            notes.append(f"{old!r}: not drawn as a whole string in the image, left as it is")
            continue
        total = {"place": 0, "grown": 0, "pointed": 0, "moved": 0}
        for run in runs:
            plan = _plan(reading, run, new, CELL_ROOM if run.at in in_a_cell else None)
            if isinstance(plan, str):
                notes.append(f"{old!r} at {run.at:#x}: {plan}, left as it is")
                continue
            operations, counts = plan
            apply(operations)
            for key, n in counts.items():
                total[key] += n
        if total["place"] or total["grown"] or total["pointed"]:
            notes.append(f"{old!r} -> {new!r} ({summary(total)})")
    return tptp.seal(bytes(data) + bytes(added)), notes
