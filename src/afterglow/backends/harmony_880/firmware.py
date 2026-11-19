"""The Harmony 880's and 885's two 64 KiB flash images, checked the way the bootloader
checks them.

A remote's first 128 KiB hold a bootloader (``0x000000``) and the application
(``0x010000``). The application can be written again over USB whenever the bootloader
runs; **nothing can write the bootloader back if it is destroyed**, so an image that is
about to be offered for ``0x000000`` is checked more strictly than anything else here.

Nothing in this module writes to a remote.

Format reference: docs/harmony_880/firmware.md
"""

from __future__ import annotations

SIZE = 0x10000
APPLICATION_BASE = 0x010000
APPLICATION_MAGIC = b"HG"
APPLICATION_MAGIC_OFFSET = 4
# The application's entry, where the bootloader jumps: a GOTO at this offset.
APPLICATION_ENTRY = 0xC00
CHECKSUM_SEED = (0x21, 0x43)
# Where the bootloader hands its two interrupt vectors, in both bootloaders read.
INTERRUPT_HANDOFF = {0x08: 0x010400, 0x18: 0x010800}


def goto_target(image: bytes, offset: int) -> int | None:
    """The byte address a PIC18 ``GOTO`` at ``offset`` jumps to, or None if the four
    bytes there are not one (``EFkk Fkkk``: a 20-bit word address in two words)."""
    if offset + 4 > len(image):
        return None
    first = image[offset] | image[offset + 1] << 8
    second = image[offset + 2] | image[offset + 3] << 8
    if first >> 8 != 0xEF or second >> 12 != 0xF:
        return None
    return 2 * (((second & 0xFFF) << 8) | (first & 0xFF))


def _lanes(image: bytes, start: int) -> tuple[int, int]:
    low, high = CHECKSUM_SEED
    for byte in image[start::2]:
        low ^= byte
    for byte in image[start + 1::2]:
        high ^= byte
    return low, high


def application_checksum(image: bytes) -> tuple[int, int]:
    """The two bytes the bootloader sums the application to: the even and odd bytes
    from offset 4 to the end, seeded 0x21 and 0x43. The two stored at offset 0 are this."""
    return _lanes(image, 4)


def last_programmed(image: bytes) -> int:
    """The offset of the last byte that is not erased (``0xFF``), or -1 for a blank image."""
    stripped = image.rstrip(b"\xff")
    return len(stripped) - 1


def check_application(image: bytes) -> list[str]:
    """Why ``image`` would not be started by the bootloader, as a list - empty if it would."""
    problems = []
    if len(image) != SIZE:
        return [f"an application is {SIZE} bytes, this is {len(image)}"]
    magic_at = APPLICATION_MAGIC_OFFSET
    if image[magic_at:magic_at + len(APPLICATION_MAGIC)] != APPLICATION_MAGIC:
        problems.append(f"no {APPLICATION_MAGIC!r} magic at {APPLICATION_BASE + magic_at:#08x}")
    stored, wanted = tuple(image[0:2]), application_checksum(image)
    if stored != wanted:
        problems.append(f"the checksum is {stored[0]:02x}{stored[1]:02x}, the image sums to "
                        f"{wanted[0]:02x}{wanted[1]:02x}")
    if goto_target(image, APPLICATION_ENTRY) is None:
        problems.append(f"no GOTO to the entry point at {APPLICATION_BASE + APPLICATION_ENTRY:#08x}")
    return problems


def check_bootloader(image: bytes) -> list[str]:
    """Why ``image`` would leave the remote with no program to run, as a list.

    Only what can be read off the bytes is claimed: that the reset vector jumps to code
    that is there, and that the interrupt vectors reach the application. An image that
    passes is not thereby safe to write; it has been read as the bootloaders of two
    remotes are, and no more.
    """
    if len(image) != SIZE:
        return [f"a bootloader is {SIZE} bytes, this is {len(image)}"]
    problems = []
    reset = goto_target(image, 0)
    if reset is None:
        problems.append("the first instruction is not a GOTO")
    elif reset >= SIZE or reset > last_programmed(image):
        problems.append(
            f"the reset vector jumps to {reset:#06x}, but nothing is programmed from "
            f"{last_programmed(image) + 1:#06x} on: the remote would run erased flash "
            "and never start")
    for vector, application in INTERRUPT_HANDOFF.items():
        target = goto_target(image, vector)
        if target != application:
            problems.append(
                f"the interrupt vector at {vector:#04x} goes to "
                f"{'nowhere' if target is None else format(target, '#08x')}, not to the "
                f"application at {application:#08x}: the application would never be "
                "interrupted")
    return problems


def product_string(image: bytes) -> str | None:
    """The USB product string an application carries (``Harmony Remote 0-4.4.2``: the
    software type, a dash, the version), or the bootloader's, or None."""
    for text in ("Harmony Remote ", "Harmony Bootloader"):
        at = image.find(text.encode("utf-16-le"))
        if at < 0:
            continue
        end = at
        while end + 1 < len(image) and 0x20 <= image[end] < 0x7F and image[end + 1] == 0:
            end += 2
        return image[at:end].decode("utf-16-le")
    return None


def describe(image: bytes) -> dict:
    """What a 64 KiB image is, by the checks above."""
    return {
        "size": len(image),
        "application": not check_application(image),
        "bootloader": not check_bootloader(image),
        "product": product_string(image),
        "last_programmed": last_programmed(image),
    }
