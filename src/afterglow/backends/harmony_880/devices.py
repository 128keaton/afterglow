"""The 880's Devices menu: which devices it lists, and taking one off it.

The Devices menu is a page of cells, a device each, and a screen program draws it. The image
keeps one copy for each state the remote can be in (idle, and each activity), listing the
devices in a different order; every copy is a page of a mode (section 6) and has a binding
list that gives each soft key a selection list (``0x7E`` mode) that enters a device.

A cell is a picture of the device at the cell's corner, up to two lines of its name, and a
frame over the picture:

    02 x y u24      the picture, at (0 or 64, 24 + 32 * row)
    10 n, 05/04 ..  each line of the name (labels.py)
    02 x y u24      the frame, at the same place; one picture for each column

The soft key of a cell is fixed by its place, not by its device - the left column is keys
0x85, 0x86, 0x87, 0x88 from the top and the right one 0xAD, 0xAE, 0xB0 (the fourth is not
known, and a page with a device in that place is left as it is). Read against the images,
each picture of `Update.EZHex` and `H880-Spare-4.EZHex` reaches the same IR group through its
key on all six copies, which is what says the keys are by place.

``remove`` takes a device off every copy: the cells after it move up one place (their
pictures, names and keys with them), and the page's program and binding list are written
again at the end of the image, the page record is pointed at them, and nothing that is there
moves. What it does **not** do is take the device out of the image: its infrared group, its
modes and the activities that use it stay as they were, so what the remote does for an
activity is the same, and the device is no longer offered on the menu.

Format reference: docs/harmony_880/devices.md
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import labels, screens, tptp
from .decompiler import hconfig

BASE = tptp.BASE
CELL_X = (0, 64)
FIRST_Y = 24
ROW_STEP = 32
ROWS = 4
# the soft key of each place, (column, row)
KEYS = {(0, 0): 0x85, (0, 1): 0x86, (0, 2): 0x87, (0, 3): 0x88,
        (1, 0): 0xAD, (1, 1): 0xAE, (1, 2): 0xB0}
PICTURE, TEXT_AT, TEXT_INLINE, FONT = 2, screens.TEXT_AT, screens.TEXT_INLINE, \
    screens.SELECT_FONT
RUN_OF_ACTIONS = 0x7F
SELECT_MODE = 0x7E
SEND = 0x7D


class DevicesError(ValueError):
    """A Devices page this does not understand; it is left as it is."""


def _u24(blob: bytes, at: int) -> int:
    return int.from_bytes(blob[at:at + 3], "little")


def _u16(blob: bytes, at: int) -> int:
    return int.from_bytes(blob[at:at + 2], "little")


# -- the image's tables ---------------------------------------------------------------------


@dataclass
class Tables:
    payload: bytes
    modes: list[int]            # offset of each mode entry
    lists: int                  # offset of the table of action lists

    @classmethod
    def read(cls, payload: bytes) -> "Tables":
        slots = tptp.slots(payload)
        table, lists = slots[6], slots[11]
        if table is None or lists is None:
            raise DevicesError("the image has no mode table or no action lists")
        count = _u24(payload, table)
        modes = [_u24(payload, table + 3 + 3 * index) - BASE for index in range(count)]
        return cls(payload, modes, lists)

    def action_list(self, index: int) -> list[tuple[int, int]]:
        payload = self.payload
        count = _u16(payload, self.lists)
        if not 0 <= index < count:
            return []
        at = _u24(payload, self.lists + 2 + 3 * index) - BASE
        return [(_u16(payload, at + 1 + 3 * step), payload[at + 3 + 3 * step])
                for step in range(payload[at])]

    def tagged(self, at: int) -> list[tuple[int, int, int, int | None]]:
        """(tag, operand, opcode, flags) of a narrow list (no flags, None) or a wide one."""
        payload = self.payload
        if payload[at] == 0:
            return [(payload[at + 3 + 5 * k], _u16(payload, at + 4 + 5 * k),
                     payload[at + 6 + 5 * k], payload[at + 2 + 5 * k])
                    for k in range(payload[at + 1])]
        return [(payload[at + 1 + 4 * k], _u16(payload, at + 2 + 4 * k),
                 payload[at + 4 + 4 * k], None) for k in range(payload[at])]

    def sent_groups(self, index: int, seen: set[int] | None = None) -> set[int]:
        """The IR groups an action list sends to, through the lists it runs."""
        seen = set() if seen is None else seen
        groups: set[int] = set()
        if index in seen:
            return groups
        seen.add(index)
        for operand, opcode in self.action_list(index):
            if opcode == SEND:
                groups.add(operand >> 8)
            elif opcode == RUN_OF_ACTIONS:
                groups |= self.sent_groups(operand, seen)
        return groups

    def mode_group(self, mode: int) -> int | None:
        """The one IR group a mode sends to, or None if it sends to none or several."""
        if not 0 <= mode < len(self.modes):
            return None
        payload, entry = self.payload, self.modes[mode]
        lists = [_u24(payload, entry + 1) - BASE]
        for page in range(_u16(payload, entry + 4)):
            lists.append(_u24(payload, _u24(payload, entry + 6 + 3 * page) - BASE) - BASE)
        groups: set[int] = set()
        for at in lists:
            for _tag, operand, opcode, _flags in self.tagged(at):
                if opcode == RUN_OF_ACTIONS:
                    groups |= self.sent_groups(operand)
        return next(iter(groups)) if len(groups) == 1 else None

    def entered_mode(self, index: int) -> int | None:
        """The mode a selection list enters."""
        modes = [operand for operand, opcode in self.action_list(index) if opcode == SELECT_MODE]
        return modes[0] if len(modes) == 1 else None


# -- a Devices page -------------------------------------------------------------------------


@dataclass
class Cell:
    slot: int
    picture: dict
    items: list[dict] = field(default_factory=list)     # font selects and lines of the name
    frame: dict | None = None
    key: int | None = None
    group: int | None = None
    identity: tuple = ()


@dataclass
class Page:
    mode: int
    number: int
    record: int                 # offset of the page record: binding list, program
    binding_list: int
    program: int
    head: list[dict]
    cells: list[Cell]
    tail: list[dict]
    bindings: list[tuple[int, int, int, int | None]]
    wide: bool


def _place(x: int, y: int) -> int | None:
    if x not in CELL_X or y < FIRST_Y or (y - FIRST_Y) % ROW_STEP:
        return None
    row = (y - FIRST_Y) // ROW_STEP
    return row * 2 + CELL_X.index(x) if row < ROWS else None


def _read_program(payload: bytes, program: int) -> tuple[list, list, list]:
    steps = hconfig.parse_arch9_screen_program(payload, program)
    if not steps:
        raise DevicesError("the program does not read")
    kept = []
    for step in steps:
        step = dict(step)
        step["raw"] = bytes(payload[step["offset"]:step["offset"] + step["length"]])
        kept.append(step)
    first = next((i for i, s in enumerate(kept) if s["opcode"] == PICTURE
                  and s["length"] == 6 and _place(s["raw"][1], s["raw"][2]) is not None), None)
    if first is None:
        raise DevicesError("no cell in the program")
    last = first
    while last < len(kept) and kept[last]["opcode"] in (PICTURE, TEXT_AT, TEXT_INLINE, FONT):
        last += 1
    return kept[:first], kept[first:last], kept[last:]


def _cells(region: list) -> list[Cell]:
    cells: list[Cell] = []
    for step in region:
        if step["opcode"] == PICTURE:
            if step["length"] != 6:
                raise DevicesError("a picture of another shape in the cells")
            slot = _place(step["raw"][1], step["raw"][2])
            if slot is None:
                raise DevicesError("a picture that is not on the grid")
            if cells and cells[-1].slot == slot:
                if cells[-1].frame is not None:
                    raise DevicesError("a cell with two frames")
                cells[-1].frame = step
            else:
                cells.append(Cell(slot, step))
        else:
            if not cells:
                raise DevicesError("text before the first cell")
            cells[-1].items.append(step)
    slots = [cell.slot for cell in cells]
    if slots != sorted(set(slots)):
        raise DevicesError("cells that are not in order")
    return cells


def pages(payload: bytes) -> list[Page]:
    """Every copy of the Devices menu the image has, with each cell's soft key and device.

    A page this does not understand is left out of the list (``skipped`` says which)."""
    return _pages(payload)[0]


def _pages(payload: bytes):
    tables = Tables.read(payload)
    reading = labels.read(payload)
    titled = {run.program for run in reading.runs if reading.text(run) == "Devices"}
    found, skipped = [], []
    for mode, entry in enumerate(tables.modes):
        for number in range(_u16(payload, entry + 4)):
            record = _u24(payload, entry + 6 + 3 * number) - BASE
            program = _u24(payload, record + 3) - BASE
            if program not in titled:
                continue
            binding_list = _u24(payload, record) - BASE
            try:
                head, region, tail = _read_program(payload, program)
                cells = _cells(region)
                bindings = tables.tagged(binding_list)
                by_key = {tag: operand for tag, operand, opcode, _f in bindings
                          if opcode == RUN_OF_ACTIONS}
                if len(by_key) != len(bindings):
                    raise DevicesError("a soft key that does not run a list")
                for cell in cells:
                    if not any(step["opcode"] in (TEXT_AT, TEXT_INLINE) for step in cell.items):
                        raise DevicesError("a cell with no name")
                    if (cell.slot % 2, cell.slot // 2) not in KEYS:
                        raise DevicesError("a device in a place whose soft key is not known")
                    cell.key = KEYS[(cell.slot % 2, cell.slot // 2)]
                    if cell.key in by_key:
                        entered = tables.entered_mode(by_key[cell.key])
                        cell.group = None if entered is None else tables.mode_group(entered)
                if sorted(by_key) != sorted(cell.key for cell in cells):
                    raise DevicesError("the soft keys are not those of the cells")
            except DevicesError as exc:
                skipped.append((mode, number, str(exc)))
                continue
            found.append(Page(mode, number, record, binding_list, program, head, cells, tail,
                              bindings, payload[binding_list] == 0))
    return _agreeing(found, skipped)



def _agreeing(found: list[Page], skipped: list):
    """Keep the pages on which every device has the same IR group as it has on the other
    copies. The name of a device is drawn from the same text on every copy, so a name that
    reaches two groups means the places and soft keys were not read as they are."""
    groups: dict[tuple, set] = {}
    for page in found:
        for cell in page.cells:
            names = tuple(step["raw"][3:] for step in cell.items
                          if step["opcode"] in (TEXT_AT, TEXT_INLINE))
            cell.identity = (cell.picture["raw"][3:6], names)
            groups.setdefault(cell.identity, set()).add(cell.group)
    bad = {identity for identity, seen in groups.items() if len(seen) != 1}
    kept = []
    for page in found:
        if any(cell.identity in bad for cell in page.cells):
            skipped.append((page.mode, page.number, "a device reaches different groups on "
                            "different copies of the menu"))
        else:
            kept.append(page)
    return kept, skipped


def listed(payload: bytes) -> list[list[int | None]]:
    """The IR group of each cell of each copy of the menu, in order; None for a device the
    image does not say."""
    return [[cell.group for cell in page.cells] for page in pages(payload)]


# -- taking a device off --------------------------------------------------------------------


def _frame_pictures(found: list[Page]) -> dict[int, bytes]:
    out: dict[int, bytes] = {}
    for page in found:
        for cell in page.cells:
            if cell.frame is not None:
                out.setdefault(cell.slot % 2, bytes(cell.frame["raw"][3:6]))
    return out


def _moved(cell: Cell, slot: int, reading: labels.Reading, frames: dict[int, bytes],
           font: int | None) -> tuple[bytes, int | None]:
    """The bytes of ``cell`` drawn at ``slot`` instead of where it is, and the font in force
    after it. ``font`` is the one in force before.

    A cell does not always choose the font it draws in: one that follows another in the same
    face draws in what that one left, so every line says its own, wherever it was and
    whatever came before it."""
    column, row = slot % 2, slot // 2
    out = bytearray()

    def place(step: dict, x: int | None = None, y: int | None = None) -> bytearray:
        raw = bytearray(step["raw"])
        if x is not None:
            raw[1] = x
        if y is not None:
            raw[2] = y
        return raw

    picture = place(cell.picture, CELL_X[column], FIRST_Y + ROW_STEP * row)
    out += picture
    for step in cell.items:
        if step["opcode"] == FONT:
            continue
        if step["opcode"] not in (TEXT_AT, TEXT_INLINE):
            raise DevicesError("something in a cell that is not a line of a name")
        run = next((r for r in reading.runs if r.instruction == step["offset"]), None)
        if run is None or run.font is None:
            raise DevicesError(f"a line of a name at {step['offset']:#x} that does not read")
        if run.font != font:
            out += bytes([FONT, run.font])
            font = run.font
        x = labels.LEFT_EDGE if column == 0 else \
            labels.RIGHT_EDGE - reading.pixels(run.font, run.codes)
        if x < 0:
            raise DevicesError("a name too wide for the right column")
        out += place(step, x, run.y + ROW_STEP * (row - cell.slot // 2))
    if cell.frame is not None:
        frame = place(cell.frame, CELL_X[column], FIRST_Y + ROW_STEP * row)
        if column not in frames:
            raise DevicesError("no frame to draw in that column")
        frame[3:6] = frames[column]
        out += frame
    return bytes(out), font


def _font_after(steps: list, reading: labels.Reading, font: int | None) -> int | None:
    """The font in force after ``steps``, given the one before."""
    for step in steps:
        if step["opcode"] == FONT:
            font = step["raw"][1]
    return font


def font_after_cells(page: Page, reading: labels.Reading) -> int | None:
    """The font the page's own cells left in force, which its tail may rely on."""
    font = _font_after(page.head, reading, None)
    for cell in page.cells:
        for step in cell.items:
            if step["opcode"] == FONT:
                font = step["raw"][1]
            else:
                run = next((r for r in reading.runs if r.instruction == step["offset"]), None)
                if run is not None and run.font is not None:
                    font = run.font
    return font


def remove(payload: bytes, groups: set[int]) -> tuple[bytes, list[str]]:
    """``payload`` with the devices whose IR group is in ``groups`` taken off every copy of
    the Devices menu, and a note for each copy that was changed or left."""
    if not groups:
        return payload, []
    if bytes(payload[-4:]) != tptp.CLOSE:
        raise ValueError("the image has lost its closing DKDK; not editing it")
    found, skipped = _pages(payload)
    reading = labels.read(payload)
    frames = _frame_pictures(found)
    data = bytearray(payload[:-6])
    added = bytearray()
    notes = [f"{labels.WARNING}the Devices menu at mode {mode} page {number} is not "
             f"understood ({why}), left as it is" for mode, number, why in skipped]
    done: dict[tuple[int, int], tuple[int, int]] = {}

    def append(raw: bytes) -> int:
        address = BASE + len(data) + len(added)
        added.extend(raw)
        return address

    for page in found:
        keep = [cell for cell in page.cells if cell.group not in groups]
        if len(keep) == len(page.cells):
            continue
        try:
            body = b"".join(step["raw"] for step in page.head)
            font = _font_after(page.head, reading, None)
            for slot, cell in enumerate(keep):
                moved, font = _moved(cell, slot, reading, frames, font)
                body += moved
            # what follows the cells goes on in the font they left it in
            was = font_after_cells(page, reading)
            if page.tail and was is not None and font != was and any(
                    step["opcode"] in (TEXT_AT, TEXT_INLINE) for step in page.tail):
                body += bytes([FONT, was])
            body += b"".join(step["raw"] for step in page.tail)
            new_key = {cell.key: KEYS[(slot % 2, slot // 2)] for slot, cell in enumerate(keep)}
            entries = b""
            for tag, operand, opcode, flags in page.bindings:
                if tag in new_key:             # the order of the list is kept
                    entries += (bytes([flags]) if page.wide else b"") + bytes([new_key[tag]]) \
                        + operand.to_bytes(2, "little") + bytes([opcode])
            listing = (bytes([0, len(keep)]) if page.wide else bytes([len(keep)])) + entries
        except (DevicesError, KeyError) as exc:
            notes.append(f"{labels.WARNING}mode {page.mode} page {page.number}: {exc}, left "
                         "as it is")
            continue
        wanted = (page.program, page.binding_list)
        if wanted not in done:
            done[wanted] = (append(listing), append(body))
        listing_at, program_at = done[wanted]
        data[page.record:page.record + 3] = listing_at.to_bytes(3, "little")
        data[page.record + 3:page.record + 6] = program_at.to_bytes(3, "little")
        notes.append(f"mode {page.mode} page {page.number}: {len(page.cells) - len(keep)} "
                     f"device(s) off, {len(keep)} left")
    if not added:
        return payload, notes
    return tptp.seal(bytes(data) + bytes(added)), notes
