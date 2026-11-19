"""The 880's fonts: the font table (slot 7), and a glyph as it is stored.

A glyph is a width, then a run-length picture of colour pixels, then a zero: a byte with
its top bit set skips that many pixels (the glyph is transparent there), any other byte
is a count of literal pixels that follow, two bytes each, RGB565 with the high byte
first. A row is finished when it holds exactly `width` pixels, and every row but the last
is followed by a skip of nothing (``0x80``).

Which character a glyph code draws is not here: each configuration numbers its glyphs in
its own order (glyphs.py).

Format reference: docs/harmony_880/fonts.md
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from . import tptp

FONT_SLOT = 7
SKIP = 0x80
MAX_RUN = 0x7F


class FontError(ValueError):
    """A font table or glyph that does not read."""


@dataclass(frozen=True)
class Glyph:
    width: int
    rows: tuple[tuple[int | None, ...], ...]       # None is transparent, else RGB565
    size: int = 0                                  # bytes it occupies in the image

    @property
    def height(self) -> int:
        return len(self.rows)

    def mask(self) -> str:
        """The shape alone, one line per row: ``#`` where a pixel is drawn."""
        return "\n".join("".join("." if pixel is None else "#" for pixel in row)
                         for row in self.rows)

    def fingerprint(self) -> str:
        """A short digest of the shape, colours left out: what a glyph table can hold
        without holding Logitech's pixels."""
        return hashlib.sha256(
            f"{self.width}x{self.height}\n{self.mask()}".encode("ascii")).hexdigest()[:16]


@dataclass(frozen=True)
class FontSet:
    index: int
    height: int
    first: int
    pointers: tuple[int | None, ...]               # offsets into the image, by code - first

    def codes(self) -> list[int]:
        """The glyph codes this set draws."""
        return [self.first + i for i, pointer in enumerate(self.pointers) if pointer]

    def offset(self, code: int) -> int | None:
        index = code - self.first
        return self.pointers[index] if 0 <= index < len(self.pointers) else None


def decode_glyph(payload: bytes, offset: int) -> Glyph:
    """Read the glyph at ``offset`` (an offset into the image)."""
    if not 0 <= offset < len(payload):
        raise FontError(f"glyph at {offset:#x} is outside the image")
    width = payload[offset]
    if not width:
        raise FontError(f"glyph at {offset:#x} has no width")
    rows: list[tuple[int | None, ...]] = []
    row: list[int | None] = []
    at = offset + 1
    while True:
        if at >= len(payload):
            raise FontError(f"glyph at {offset:#x} runs off the image")
        op = payload[at]
        at += 1
        if op == 0:
            break
        if op & SKIP:
            row.extend([None] * (op & MAX_RUN))
        else:
            if at + 2 * op > len(payload):
                raise FontError(f"glyph at {offset:#x} runs off the image")
            for _ in range(op):
                row.append((payload[at] << 8) | payload[at + 1])
                at += 2
        if len(row) > width:
            raise FontError(f"a row of the glyph at {offset:#x} is wider than {width}")
        if len(row) == width:
            rows.append(tuple(row))
            row = []
    if row:
        raise FontError(f"the glyph at {offset:#x} ends in the middle of a row")
    return Glyph(width, tuple(rows), at - offset)


def encode_glyph(glyph: Glyph) -> bytes:
    """The bytes ``decode_glyph`` reads back as ``glyph``: runs as long as they can be,
    a ``0x80`` between rows, a zero at the end."""
    out = bytearray([glyph.width])
    for number, row in enumerate(glyph.rows):
        if len(row) != glyph.width:
            raise FontError(f"row {number} is {len(row)} pixels, not {glyph.width}")
        if number:
            out.append(SKIP)
        at = 0
        while at < len(row):
            end = at
            if row[at] is None:
                while end < len(row) and row[end] is None and end - at < MAX_RUN:
                    end += 1
                out.append(SKIP | (end - at))
            else:
                while end < len(row) and row[end] is not None and end - at < MAX_RUN:
                    end += 1
                out.append(end - at)
                for pixel in row[at:end]:
                    out += bytes([pixel >> 8, pixel & 0xFF])
            at = end
    out.append(0)
    return bytes(out)


def font_sets(payload: bytes) -> list[FontSet]:
    """The font table: ``u16`` count, then a ``u24`` pointer to each set; a set is
    ``u8`` height, ``u8`` first code, ``u8`` count and a ``u24`` pointer per code."""
    table = tptp.slots(payload)[FONT_SLOT]
    if table is None or table + 2 > len(payload):
        raise FontError("the image has no font table")
    count = int.from_bytes(payload[table:table + 2], "little")
    sets = []
    for index in range(count):
        at = table + 2 + 3 * index
        if at + 3 > len(payload):
            raise FontError("the font table runs off the image")
        start = int.from_bytes(payload[at:at + 3], "little") - tptp.BASE
        if not 0 <= start < len(payload) - 3:
            raise FontError(f"font set {index} is outside the image")
        height, first, glyphs = payload[start], payload[start + 1], payload[start + 2]
        pointers = []
        for code in range(glyphs):
            pointer = int.from_bytes(payload[start + 3 + 3 * code:start + 6 + 3 * code],
                                     "little")
            offset = pointer - tptp.BASE
            if pointer and not 0 <= offset < len(payload):
                raise FontError(f"glyph {first + code} of font set {index} is outside "
                                "the image")
            pointers.append(offset if pointer else None)
        sets.append(FontSet(index, height, first, tuple(pointers)))
    return sets


def glyphs(payload: bytes, font: FontSet) -> dict[int, Glyph]:
    """Every glyph of ``font`` by code."""
    return {code: decode_glyph(payload, font.offset(code)) for code in font.codes()}
