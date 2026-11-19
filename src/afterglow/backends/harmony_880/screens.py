"""The 880's screen programs: where every string on the remote's screens is drawn.

A page of the remote is a small program in the low region of the image. Its text is
drawn two ways: inline (``05 x y codes... 00``) and by reference (``04 x y u24``, aimed
at the codes of an inline string elsewhere), after ``10 n`` has chosen font set ``n``.
The codes are glyph numbers of that font, not characters (glyphs.py).

The programs are found from two places and followed through every jump and switch: slot
11 is a table of their roots (``u16`` count, ``u24`` each), and the modes of slot 6 name
the root of each of their pages. The walk is the community's (harmony-decompiler's
``parse_arch9_screen_program``, vendored, which is not particular to arch 9): a program
that does not read is dropped whole and counted, never guessed at.

Format reference: docs/harmony_880/fonts.md
"""

from __future__ import annotations

from dataclasses import dataclass

from . import tptp
from .decompiler import hconfig

SELECT_FONT = 16
TEXT_AT = 4
TEXT_INLINE = 5


@dataclass(frozen=True)
class TextRun:
    kind: str                       # "inline" or "reference"
    at: int                         # offset of the first code in the image
    x: int
    y: int
    font: int | None                # font set in effect when it is drawn, if one was chosen
    codes: tuple[int, ...]
    program: int                    # offset of the program that draws it
    instruction: int = -1           # offset of the instruction itself (its opcode byte)


def _codes(payload: bytes, at: int) -> tuple[int, ...] | None:
    """The terminated run of codes at ``at``; a code with its top bit set is two bytes."""
    out = []
    while at < len(payload) and payload[at] != 0:
        if payload[at] & 0x80:
            out.append((payload[at] << 8) | payload[at + 1] if at + 1 < len(payload) else -1)
            at += 2
        else:
            out.append(payload[at])
            at += 1
    return tuple(out) if at < len(payload) else None


def roots(payload: bytes) -> list[int]:
    """Where programs start: the screen of each page of each mode in slot 6.

    Slot 6 is u24 count and a u24 pointer to each mode; a mode holds a u16 page
    count at +4 and a u24 pointer to each page from +6; a page is a binding list and
    the program it draws. Slot 11 is not a table of programs on this architecture - its
    entries start with the opcodes 2 to 8, and five in fifteen hundred read as screens -
    so it is not used, whatever the arch-9 reader does with it.
    """
    table = tptp.slots(payload)[6]
    found: list[int] = []
    if table is None or table + 3 > len(payload):
        return found

    def u24(at: int) -> int | None:
        if at + 3 > len(payload):
            return None
        offset = int.from_bytes(payload[at:at + 3], "little") - tptp.BASE
        return offset if 0 <= offset < len(payload) else None

    count = int.from_bytes(payload[table:table + 3], "little")
    for index in range(min(count, 4096)):
        mode = u24(table + 3 + 3 * index)
        if mode is None or mode + 6 > len(payload):
            continue
        pages = int.from_bytes(payload[mode + 4:mode + 6], "little")
        if pages > 256:
            continue
        for number in range(pages):
            page = u24(mode + 6 + 3 * number)
            root = u24(page + 3) if page is not None else None
            if root is not None:
                found.append(root)
    return sorted(set(found))


def text_runs(payload: bytes) -> tuple[list[TextRun], dict]:
    """Every string drawn, with the font in effect, and a count of what was not read.

    Returns ``(runs, report)``. ``report`` counts the roots followed, the programs that
    did not read (``unreadable``) and the runs whose codes ran off the image.
    """
    pending = [(root, None) for root in roots(payload)]
    seen: set[tuple[int, int | None]] = set(pending)
    runs: dict[tuple[str, int], TextRun] = {}
    report = {"roots": len(pending), "programs": 0, "unreadable": 0, "unterminated": 0}
    while pending:
        start, font = pending.pop()
        program = hconfig.parse_arch9_screen_program(payload, start)
        if program is None:
            report["unreadable"] += 1
            continue
        report["programs"] += 1
        for instruction in program:
            opcode, operands = instruction["opcode"], instruction["operands"]
            if opcode == SELECT_FONT:
                font = operands[0]
            elif opcode == TEXT_INLINE:
                at = instruction["offset"] + 3
                codes = _codes(payload, at)
                if codes is None:
                    report["unterminated"] += 1
                else:
                    runs.setdefault(("inline", at), TextRun(
                        "inline", at, operands[0], operands[1], font, codes, start,
                        instruction["offset"]))
            elif opcode == TEXT_AT:
                target = int.from_bytes(operands[2:5], "little") - tptp.BASE
                codes = _codes(payload, target) if 0 <= target < len(payload) else None
                if codes is None:
                    report["unterminated"] += 1
                else:
                    runs.setdefault(("reference", instruction["offset"]), TextRun(
                        "reference", target, operands[0], operands[1], font, codes, start,
                        instruction["offset"]))
            for target in instruction["targets"]:
                if (target, font) not in seen:
                    seen.add((target, font))
                    pending.append((target, font))
    return sorted(runs.values(), key=lambda run: (run.at, run.kind)), report
