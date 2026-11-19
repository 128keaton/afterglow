#!/usr/bin/env python3
"""Record which character each Harmony 880 glyph shape draws, from real configurations.

The 880 draws all its text as glyph codes, and each configuration numbers its glyphs in its
own order: the Bedroom sample's ``P`` is code 16 and the Living Room's is 27. The shapes do
not change from one image to the next, so a few configurations whose codes are known say what
each shape is, and that reads the text of any other (backends/harmony_880/glyphs.py).

This writes those shapes - the height and a digest of the mask, not Logitech's pixels - with
the character they draw, to ``backends/harmony_880/glyph-shapes.json``.

    tools/harmony_880_glyph_shapes.py CONFIG.ezhex [CONFIG.ezhex ...]   # write the table
    tools/harmony_880_glyph_shapes.py --check CONFIG.ezhex ...          # report, write nothing
    tools/harmony_880_glyph_shapes.py --unresolved CONFIG.ezhex ...     # what is still unread

What it starts from is ``tools/harmony_880_glyph_seeds.json``: the codes of a few
configurations as people read them off the remote, and glyphs named one at a time from the
words around them (``--unresolved`` prints those). It then spreads what it knows, under one
rule: a font set shares its image's numbering only when what the table already reads in it
agrees with that numbering, almost everywhere. A set that does not - the 880 keeps small
icon-like faces with numberings of their own - is never filled in from it.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from afterglow import ezhex                                     # noqa: E402
from afterglow.backends.harmony_880 import font, glyphs, screens  # noqa: E402

SEEDS = Path(__file__).with_name("harmony_880_glyph_seeds.json")
ALIGNED = 0.9           # how much of a set the table must read as the image's numbering
MINIMUM = 6             # ... and how many glyphs that is judged on


def image_id(path: Path, payload: bytes) -> str:
    return f"{path.name}:{len(payload)}"


def load(paths):
    images = {}
    for path in paths:
        payload = ezhex.read(Path(path).read_bytes()).payload
        images[image_id(Path(path), payload)] = (payload, font.font_sets(payload))
    return images


def verified_sets(payload, sets, codes):
    """The font sets that draw text made only of ``codes``, and the codes they draw: the
    evidence that these codes mean what the seed says in those sets."""
    runs, _report = screens.text_runs(payload)
    found = defaultdict(set)
    for run in runs:
        if run.font is not None and len(run.codes) >= 2 and all(c in codes for c in run.codes):
            found[run.font].update(run.codes)
    return found


def seed_table(images, seeds):
    """{key: {chars}} from the seeds: the codes of the images they name, read through the
    font sets that draw text made of those codes, and the shapes named one at a time."""
    table: dict[str, set[str]] = defaultdict(set)
    for ident, entry in seeds.get("images", {}).items():
        if ident not in images:
            continue
        payload, sets = images[ident]
        codes = {code: char for char, code in entry["codes"].items()}
        for index, used in verified_sets(payload, sets, codes).items():
            fs = sets[index]
            for code in used:
                glyph = font.decode_glyph(payload, fs.offset(code))
                table[glyphs.shape_key(fs.height, glyph)].add(codes[code])
    for key, char in seeds.get("shapes", {}).items():
        table[key].add(char)
    return table


def spread(images, table):
    """Fill in a set's unread glyphs from its image's numbering where the set shares it."""
    added = Counter()
    for _round in range(8):
        grew = 0
        lookup = {key: "".join(sorted(chars)) for key, chars in table.items()}
        for ident, (payload, sets) in images.items():
            reads = {fs.index: glyphs.code_map(payload, lookup, [fs]) for fs in sets}
            votes: dict[int, Counter] = defaultdict(Counter)
            for read in reads.values():
                for code, char in read.chars.items():
                    votes[code][char] += 1
            main = {code: v.most_common(1)[0][0] for code, v in votes.items()
                    if len(v) == 1}
            for fs in sets:
                read = reads[fs.index]
                judged = [code for code in read.chars if code in main]
                if len(judged) < MINIMUM:
                    continue
                if sum(read.chars[c] == main[c] for c in judged) / len(judged) < ALIGNED:
                    continue
                for code in fs.codes():
                    if code in read.chars or code not in main:
                        continue
                    key = glyphs.shape_key(fs.height, font.decode_glyph(payload, fs.offset(code)))
                    if main[code] not in table[key]:
                        table[key].add(main[code])
                        grew += 1
                        added[ident] += 1
        if not grew:
            break
    return added


def unresolved(images, table):
    """The glyphs the text of these images draws that the table does not read, with the
    words they stand in, so that someone can name them."""
    lookup = {key: "".join(sorted(chars)) for key, chars in table.items()}
    found: dict[str, dict] = {}
    for ident, (payload, sets) in images.items():
        runs, _report = screens.text_runs(payload)
        reads = {fs.index: glyphs.code_map(payload, lookup, [fs]) for fs in sets}
        for run in runs:
            if run.font is None:
                continue
            fs, read = sets[run.font], reads[run.font]
            for code in set(run.codes):
                if code in read.chars or fs.offset(code) is None:
                    continue
                glyph = font.decode_glyph(payload, fs.offset(code))
                entry = found.setdefault(glyphs.shape_key(fs.height, glyph), {
                    "glyph": glyph, "where": [], "words": Counter(), "uses": 0})
                entry["uses"] += run.codes.count(code)
                if (ident, fs.index, code) not in entry["where"]:
                    entry["where"].append((ident, fs.index, code))
                entry["words"][" ".join(
                    "*" if c == code else read.chars.get(c, "?") for c in run.codes)] += 1
    return found


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("configs", nargs="*", type=Path, metavar="CONFIG")
    parser.add_argument("--check", action="store_true", help="report, write nothing")
    parser.add_argument("--unresolved", action="store_true",
                        help="print the glyphs still unread, with their words")
    args = parser.parse_args(argv)
    if not args.configs:
        parser.print_usage(sys.stderr)
        return 2
    images = load(args.configs)
    seeds = json.loads(SEEDS.read_text(encoding="utf-8"))
    table = seed_table(images, seeds)
    seeded = len(table)
    spread(images, table)
    clashes = {key: chars for key, chars in table.items() if len(chars) > 1}
    print(f"{len(images)} images, {seeded} shapes from the seeds, {len(table)} after "
          f"spreading, {len(clashes)} shared by look-alike characters")
    for key, chars in sorted(clashes.items()):
        print(f"  {key}: {''.join(sorted(chars))}")
    lookup = {key: "".join(sorted(chars)) for key, chars in table.items()}
    for ident, (payload, sets) in images.items():
        runs, _report = screens.text_runs(payload)
        total = read = 0
        for run in runs:
            if run.font is None:
                continue
            code_map = glyphs.code_map(payload, lookup, [sets[run.font]])
            total += len(run.codes)
            read += sum(code in code_map.chars for code in run.codes)
        print(f"  {ident:36} reads {read}/{total} drawn glyphs"
              + (f", {100 * read / total:.1f}%" if total else ""))
    if args.unresolved:
        found = unresolved(images, table)
        print(f"\n{len(found)} shapes unread")
        for key, entry in sorted(found.items(), key=lambda kv: -kv[1]["uses"]):
            print(f"\n{key}  drawn {entry['uses']} times, first at "
                  f"{entry['where'][0]}")
            print("\n".join("    " + line for line in entry["glyph"].mask().split("\n")
                            if "#" in line))
            for words, count in entry["words"].most_common(5):
                print(f"    {words!r} x{count}")
    if not args.check:
        out = glyphs.SHAPES_FILE
        out.write_text(json.dumps({
            "_comment": "Written by tools/harmony_880_glyph_shapes.py: the characters each "
                        "glyph shape draws, by the font's height and a digest of its mask. "
                        "No pixels, and no glyph codes - those are each image's own.",
            "shapes": {key: "".join(sorted(chars)) for key, chars in sorted(table.items())},
        }, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {out}")
        glyphs.shapes.cache_clear()
    return 0


if __name__ == "__main__":
    sys.exit(main())
