#!/usr/bin/env python3
"""Say whether a Harmony 880 file is one the remote would accept, before anything is written.

    python3 tools/harmony_880_check.py config.ezhex [more files ...]

Reads and writes nothing but the files it is given, and never talks to a remote. It knows
three kinds of file:

  *.ezhex   a configuration: its envelope, then the TPTP image inside it checked the way
            the remote's start check does (backends/harmony_880/tptp.py)
  *.EZUp    a firmware package: each phase's data checked as the application or the
            bootloader it claims to be
  *.bin     a 64 KiB flash image, read as an application and as a bootloader

Exit status is 1 if any file is not one the remote would accept. An image that fails is
the reason to stop: a bootloader that fails cannot be written back by anything.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from afterglow import ezhex, remotes                      # noqa: E402
from afterglow.backends.harmony_880 import firmware, tptp  # noqa: E402


def check_configuration(path: Path) -> list[str]:
    raw = path.read_bytes()
    try:
        contents = ezhex.read(raw)
    except ValueError as exc:
        return [f"not a configuration file: {exc}"]
    problems = []
    if contents.outside:
        problems.append(f"{contents.outside} bytes outside the declared payload")
    if contents.declared is None or ezhex.checksum(contents.payload) != contents.declared:
        problems.append("the envelope's CHECKSUM does not match the payload")
    try:
        profile = remotes.identify(contents.header)
        print(f"  remote   : {profile.model} [{profile.status}]")
    except remotes.UnknownRemote as exc:
        problems.append(f"no profile: {exc}")
    try:
        report = tptp.validate(contents.payload)
        print(f"  image    : {report['size']} bytes, checksum {report['checksum']}, "
              f"{len(report['sections'])} sections")
    except ValueError as exc:
        problems.append(str(exc))
    return problems


def check_flash_image(label: str, image: bytes) -> list[str]:
    application, bootloader = firmware.check_application(image), firmware.check_bootloader(image)
    product = firmware.product_string(image)
    print(f"  {label}: {len(image)} bytes, last programmed byte {firmware.last_programmed(image):#06x}"
          + (f", {product!r}" if product else ""))
    if not application:
        print("  reads as : a valid application")
        return []
    if not bootloader:
        print("  reads as : a valid bootloader")
        return []
    return ["is neither a valid application nor a valid bootloader",
            *(f"as an application: {problem}" for problem in application[:3]),
            *(f"as a bootloader: {problem}" for problem in bootloader)]


def check_package(path: Path) -> list[str]:
    text = path.read_bytes().decode("latin-1")
    phases = re.findall(r"<PHASE>(.*?)</PHASE>", text, re.S)
    if not phases:
        return ["no <PHASE> in this package"]
    problems = []
    for number, phase in enumerate(phases, 1):
        kind = re.search(r"<TYPE>(.*?)</TYPE>", phase)
        data = "".join(re.findall(r"<DATA>([0-9A-Fa-f]*)</DATA>", phase))
        name = f"phase {number} ({kind.group(1) if kind else '?'})"
        problems += [f"{name} {problem}" for problem in
                     check_flash_image(name, bytes.fromhex(data))]
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="*", type=Path, metavar="FILE",
                        help="configuration (.ezhex), firmware package (.EZUp) or 64 KiB image")
    args = parser.parse_args(argv)
    paths = args.files
    if not paths:
        parser.print_usage(sys.stderr)
        return 2
    failed = False
    for path in paths:
        print(path.name)
        suffix = path.suffix.lower()
        if suffix == ".ezhex":
            problems = check_configuration(path)
        elif suffix == ".ezup":
            problems = check_package(path)
        else:
            problems = check_flash_image("image", path.read_bytes())
        for problem in problems:
            print(f"  REFUSED  : {problem}")
        print("  verdict  : " + ("would not be accepted" if problems else "ok"))
        failed = failed or bool(problems)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
