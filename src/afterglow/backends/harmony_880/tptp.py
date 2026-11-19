"""The Harmony 880's and 885's ``TPTP`` configuration image: the firmware's start check,
and the layout every known image shares.

Two different questions are answered here and kept apart, because the second is only a
measurement and the first is what the remote does:

``start_check``  what the application tests at every start before it runs the user's
                 configuration. Four things, and nothing else. A failure does not
                 brick the remote: it shows a message and runs the application's own
                 built-in configuration, so the user's can be written again.
``validate``     the start check plus the layout all known images share. An image that
                 passes the first and not the second would probably run, and is refused
                 anyway: nobody has ever seen one boot.

Format reference: docs/harmony_880/tptp.md
"""

from __future__ import annotations

import hashlib
import struct

# Where the configuration lives in the remote's address space (libconcord's arch 8
# ``config_base``). The firmware's constant: nothing in the image states it.
BASE = 0x020000
# The staged-install area for a new application starts here, so a configuration may not
# reach it.
LIMIT = 0x1D0000
MAX_SIZE = LIMIT - BASE

MAGIC = b"TPTP"
MARKER = b"WLWL"
MARKER_OFFSET = 0x5F
CLOSE = b"DKDK"
FORMAT = 0x1500
SLOT_COUNT = 20
SLOT_TABLE = 0x0C
# Slots no known image fills. The Harmony One's index lacks only slot 8.
ABSENT_SLOTS = (8, 19)
CHECKSUM_SEED = (0x21, 0x43)

# What the remote shows when its start check fails (firmware message ids).
MESSAGE_WEBSITE = 0x00      # "Go to Website to update settings": a marker was wrong
MESSAGE_CORRUPT = 0x1A      # "Configuration Corrupted": the markers held, the sum did not
_MESSAGE_TEXT = {MESSAGE_WEBSITE: "'Go to Website to update settings'",
                 MESSAGE_CORRUPT: "'Configuration Corrupted'"}


def checksum(data: bytes, end: int) -> tuple[int, int]:
    """The two-lane XOR over every complete pair before ``end``: even bytes into one
    lane, odd bytes into the other, seeded 0x21 and 0x43. An odd last byte is left out."""
    end -= end % 2
    low, high = CHECKSUM_SEED
    return low ^ _xor(data[0:end:2]), high ^ _xor(data[1:end:2])


def _xor(lane: bytes) -> int:
    """Every byte XORed together, folded as one integer so a megabyte is not a Python
    loop (the same routine as the Harmony One backend's)."""
    value, width = int.from_bytes(lane, "little"), len(lane)
    while width > 1:
        half = (width + 1) // 2
        value = (value & ((1 << (8 * half)) - 1)) ^ (value >> (8 * half))
        width = half
    return value


def seal(body: bytes) -> bytes:
    """Close an image body with its checksum and ``DKDK``, and point the header's end
    vector at the marker. ``body`` is everything before the trailer.

    Every writer that changes the length of an image has to do this. The first 880
    backend's label patch did not: new text was put where the trailer had been, and
    the remote's start check then failed (docs/harmony_880/tptp.md).
    """
    out = bytearray(body)
    marker = len(out) + 2
    out += b"\x00\x00" + CLOSE
    struct.pack_into("<I", out, 4, BASE + marker)
    out[marker - 2:marker] = bytes(checksum(out, marker - 2))
    return bytes(out)


def start_check(payload: bytes) -> tuple[int | None, list[str]]:
    """Run the firmware's start check on ``payload`` as the application does.

    Returns the message the remote would show (None when it passes) and a reason for
    each failed test. The tests are the cookie at the base, the marker at base + 0x5F,
    ``DKDK`` where the three bytes at base + 4 say, and the two bytes before it against
    the checksum. Every other byte of the image is ignored.
    """
    problems: list[str] = []
    markers_failed = False
    if payload[:4] != MAGIC:
        problems.append(f"cookie is {bytes(payload[:4])!r}, not {MAGIC!r}")
        markers_failed = True
    if payload[MARKER_OFFSET:MARKER_OFFSET + 4] != MARKER:
        problems.append(f"marker at {MARKER_OFFSET:#x} is "
                        f"{bytes(payload[MARKER_OFFSET:MARKER_OFFSET + 4])!r}, not {MARKER!r}")
        markers_failed = True
    end = int.from_bytes(payload[4:7], "little") - BASE if len(payload) >= 7 else -1
    if not (2 <= end <= len(payload) - 4 and payload[end:end + 4] == CLOSE):
        found = bytes(payload[end:end + 4]) if 0 <= end <= len(payload) else b""
        problems.append(f"the end vector ({BASE + end:#08x}) does not reach {CLOSE!r}: "
                        f"it finds {found!r} in an image of {len(payload)} bytes")
        markers_failed = True
    if markers_failed:
        return MESSAGE_WEBSITE, problems
    stored = tuple(payload[end - 2:end])
    wanted = checksum(payload, end - 2)
    if stored != wanted:
        return MESSAGE_CORRUPT, [
            f"the checksum is {stored[0]:02x}{stored[1]:02x}, the image sums to "
            f"{wanted[0]:02x}{wanted[1]:02x}"]
    return None, problems


def slots(payload: bytes) -> tuple[int | None, ...]:
    """The twenty section pointers as offsets into the image, None where a slot is empty."""
    out = []
    for index in range(SLOT_COUNT):
        absolute = struct.unpack_from("<I", payload, SLOT_TABLE + 4 * index)[0]
        out.append(absolute - BASE if absolute else None)
    return tuple(out)


def section_ranges(payload: bytes) -> dict[int, tuple[int, int]]:
    """{slot: (start, end)} for each filled slot, a section running to the next higher
    pointer and the last one to the checksum. Pointers are not in slot order."""
    end = int.from_bytes(payload[4:7], "little") - BASE - 2
    present = sorted((offset, index) for index, offset in enumerate(slots(payload))
                     if offset is not None)
    ranges = {}
    for position, (offset, index) in enumerate(present):
        stop = present[position + 1][0] if position + 1 < len(present) else end
        ranges[index] = (offset, stop)
    return ranges


def validate(payload: bytes) -> dict:
    """Check ``payload`` is an image the 880 would start and of the layout measured in
    every known one. Raises ``ValueError`` naming every problem found."""
    if not isinstance(payload, (bytes, bytearray)):
        raise TypeError("a TPTP payload must be bytes")
    payload = bytes(payload)
    if len(payload) < MARKER_OFFSET + len(MARKER) + 1 + 6:
        raise ValueError(f"a TPTP image is at least {MARKER_OFFSET + len(MARKER) + 7} "
                         f"bytes, this is {len(payload)}")
    message, problems = start_check(payload)
    problems = list(problems)
    end = int.from_bytes(payload[4:7], "little") - BASE
    if payload[7]:
        problems.append(f"the end vector's top byte is {payload[7]:#04x}, not 0")
    format_word = struct.unpack_from("<I", payload, 8)[0]
    if format_word != FORMAT:
        problems.append(f"the format word is {format_word:#x}, not {FORMAT:#x}")
    if len(payload) > MAX_SIZE:
        problems.append(f"{len(payload)} bytes reaches the staged-install area at "
                        f"{LIMIT:#x}; a configuration can be at most {MAX_SIZE}")
    if message is None:
        if end + 4 != len(payload):
            problems.append(f"{len(payload) - end - 4} bytes follow {CLOSE!r}")
        found = slots(payload)
        for index, offset in enumerate(found):
            if index in ABSENT_SLOTS:
                if offset is not None:
                    problems.append(f"section slot {index} is filled; no known image fills it")
            elif offset is None:
                problems.append(f"section slot {index} is empty; every known image fills it")
            elif not SLOT_TABLE + 4 * SLOT_COUNT <= offset < end - 2:
                problems.append(f"section slot {index} points outside the image "
                                f"({BASE + offset:#08x})")
        present = [offset for offset in found if offset is not None]
        if len(present) != len(set(present)):
            problems.append("section pointers are not unique")
    if problems:
        raise ValueError(
            "not a Harmony 880/885 configuration image"
            + (f" - the remote would show {_MESSAGE_TEXT[message]} and run its built-in "
               "configuration" if message in _MESSAGE_TEXT else "")
            + ": " + "; ".join(problems))
    return {
        "kind": "tptp-image", "size": len(payload), "base": f"{BASE:#08x}",
        "end": f"{BASE + end:#08x}",
        "checksum": f"{payload[end - 2]:02x}{payload[end - 1]:02x}",
        "sections": {str(index): [start, stop]
                     for index, (start, stop) in section_ranges(payload).items()},
        "marker_byte": payload[MARKER_OFFSET + 4],
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
