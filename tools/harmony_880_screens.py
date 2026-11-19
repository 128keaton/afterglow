#!/usr/bin/env python3
"""Print what an 880 configuration draws on a page: the text, where, and in which font.

    tools/harmony_880_screens.py CONFIG.ezhex                  # the Devices page
    tools/harmony_880_screens.py CONFIG.ezhex --find "Watch TV"  # every page that draws it
    tools/harmony_880_screens.py CONFIG.ezhex --all              # every page there is

The screen is 128 by 160 and a page is a program that draws pictures and text; this lists the
text, read as the image's own glyphs (backends/harmony_880/glyphs.py), by row. A name that
does not fit a cell is two lines, and a cell on the right is drawn right-aligned, so a label
often starts well in from its cell. Pictures are counted, not drawn. Reads the file and
nothing else.
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from afterglow import ezhex                                     # noqa: E402
from afterglow.backends.harmony_880 import labels, tptp          # noqa: E402
from afterglow.backends.harmony_880.decompiler import hconfig    # noqa: E402


def pictures(payload: bytes, program: int) -> int:
    parsed = hconfig.parse_arch9_screen_program(payload, program)
    return sum(1 for step in parsed or [] if step["opcode"] in (2, 3))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("config", type=Path)
    parser.add_argument("--find", default="Devices", metavar="TEXT",
                        help="show the pages that draw exactly this text (default: Devices)")
    parser.add_argument("--all", action="store_true", help="show every page")
    args = parser.parse_args(argv)

    payload = ezhex.read(args.config.read_bytes()).payload
    reading = labels.read(payload)
    by_program = defaultdict(list)
    for run in reading.runs:
        by_program[run.program].append(run)
    wanted = [program for program, runs in sorted(by_program.items())
              if args.all or any(reading.text(run) == args.find for run in runs)]
    if not wanted:
        print(f"nothing draws {args.find!r}", file=sys.stderr)
        return 1
    for program in wanted:
        print(f"page program at {program + tptp.BASE:#08x}, "
              f"{pictures(payload, program)} pictures")
        for run in sorted(by_program[program], key=lambda r: (r.y, r.x, r.at)):
            print(f"  y={run.y:3}  x={run.x:3}  font {run.font:2}  {reading.text(run)!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
