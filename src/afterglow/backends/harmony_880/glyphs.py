"""Which character a glyph code draws, for one image.

Each configuration numbers its glyphs in its own order, so a code means nothing outside the
image it is read in: the Bedroom sample's ``P`` is code 16, the Living Room's is 27. What is
the same in every image is the shape a glyph draws. ``glyph-shapes.json`` records, for each
shape (its height and a digest of its mask - the shape only, never Logitech's pixels), the
character it is, and a code is read by decoding its glyph in this image's font table and
looking the shape up. Nothing numbered is carried from one image to another.

An image has several font sets (faces, sizes) that share its numbering, so a code is read
through every set that has it: the characters its shapes allow must agree. A code whose
shapes disagree, or are not known, has no character and is reported, never guessed.

Format reference: docs/harmony_880/fonts.md
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from . import font

SHAPES_FILE = Path(__file__).with_name("glyph-shapes.json")
UNKNOWN = "\ufffd"


def shape_key(height: int, glyph: font.Glyph) -> str:
    return f"{height}:{glyph.fingerprint()}"


@lru_cache
def shapes() -> dict[str, str]:
    """{``height:digest``: the characters that shape is} - one, or the look-alikes a shape
    is shared by (a zero and a capital O in a face that draws them alike)."""
    if not SHAPES_FILE.is_file():
        return {}
    return json.loads(SHAPES_FILE.read_text(encoding="utf-8"))["shapes"]


@dataclass
class CodeMap:
    """What each glyph code of one image draws."""
    chars: dict[int, str] = field(default_factory=dict)
    clashes: dict[int, list[str]] = field(default_factory=dict)     # shapes that disagree
    ambiguous: dict[int, str] = field(default_factory=dict)         # look-alikes left open
    unknown: set[int] = field(default_factory=set)                  # no shape is known

    def codes_for(self, char: str) -> list[int]:
        """Every code that draws ``char`` here: a face may draw one character with two."""
        return sorted(code for code, drawn in self.chars.items() if drawn == char)

    def decode(self, codes) -> str:
        """The text of ``codes``; a code with no character is shown as U+FFFD."""
        return "".join(self.chars.get(code, UNKNOWN) for code in codes)

    def encode(self, text: str) -> tuple[int, ...] | None:
        """The codes that draw ``text`` here, or None if a character has none."""
        out = []
        for char in text:
            codes = self.codes_for(char)
            if not codes:
                return None
            out.append(codes[0])
        return tuple(out)

    def coverage(self, codes) -> float:
        codes = list(codes)
        return sum(code in self.chars for code in codes) / len(codes) if codes else 1.0


def code_map(payload: bytes, table: dict[str, str] | None = None,
             sets: list[font.FontSet] | None = None) -> CodeMap:
    """Read every glyph code of this image's font table by the shape it draws."""
    table = shapes() if table is None else table
    sets = font.font_sets(payload) if sets is None else sets
    allowed: dict[int, list[set[str]]] = {}
    seen: set[int] = set()
    for fs in sets:
        for code in fs.codes():
            seen.add(code)
            key = shape_key(fs.height, font.decode_glyph(payload, fs.offset(code)))
            if key in table:
                allowed.setdefault(code, []).append(set(table[key]))
    result = CodeMap()
    for code in sorted(seen):
        votes = allowed.get(code)
        if not votes:
            result.unknown.add(code)
            continue
        agreed = set.intersection(*votes)
        if not agreed:
            result.clashes[code] = sorted("".join(sorted(v)) for v in votes)
        elif len(agreed) > 1:
            result.ambiguous[code] = "".join(sorted(agreed))
        else:
            result.chars[code] = next(iter(agreed))
    return result


def learn(payload: bytes, chars_by_code: dict[int, str],
          sets: list[font.FontSet] | None = None) -> dict[str, set[str]]:
    """The shapes this image draws for the codes it is told, as ``{key: {chars}}``: what a
    shape table is made of (tools/harmony_880_glyph_shapes.py)."""
    sets = font.font_sets(payload) if sets is None else sets
    out: dict[str, set[str]] = {}
    for fs in sets:
        for code in fs.codes():
            if code in chars_by_code:
                key = shape_key(fs.height, font.decode_glyph(payload, fs.offset(code)))
                out.setdefault(key, set()).add(chars_by_code[code])
    return out
