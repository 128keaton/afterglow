"""An 880's infrared commands: read as portable signals, and a changed one written back.

Real configurations are never committed; these read whatever is in ``configs/880/`` and skip
without it.

Format reference: docs/harmony_880/ir.md
"""

from __future__ import annotations

from pathlib import Path

import pytest

from afterglow import ezhex, ir_protocol, remotes
from afterglow.backends.harmony_880 import backend, ir, screens, tptp
from afterglow.backends.harmony_880.decompiler import hconfig
from afterglow.backends.harmony_880.decompiler.ir_reader import read_arch8_ir

ROOT = Path(__file__).resolve().parent.parent
PATTERN = "*.[eE][zZ][hH][eE][xX]"
REAL = sorted((ROOT / "configs" / "880").glob(PATTERN)) + \
    sorted((ROOT / "configs" / "880" / "other").glob(PATTERN))


def _payload(path: Path) -> bytes:
    return ezhex.read(path.read_bytes()).payload


def _named(name: str) -> bytes:
    path = next((p for p in REAL if p.name == name), None)
    if path is None:
        pytest.skip(f"{name} is not in configs/880/")
    return _payload(path)


def _records(payload: bytes):
    return read_arch8_ir(payload, hconfig.decompile(payload)).records


NEC = [9000, -4500] + [560, -560, 560, -1690] * 16 + [560, -40000]


def _waveform(native: dict, pulses=NEC, carrier=38000) -> dict:
    return {"schema": "afterglow-ir-signal/1", "kind": "waveform", "name": "x",
            "pulses_us": list(pulses), "carrier_hz": carrier, "native": native}


# -- the words ------------------------------------------------------------------------------


def test_a_block_is_marks_with_the_top_bit_and_spaces_split_over_32767_and_a_zero():
    raw = ir.encode_press([9000, -4500, 560, -100000], 50_000)
    words = [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)]
    assert words[:2] == [0x7FFF, 0x4351]                    # 50,000 us of lead-in
    assert words[2] == 0x8000 | 9000 and words[3] == 4500 and words[4] == 0x8000 | 560
    assert sum(words[5:-2]) == 100000 and words[-2:] == [1, 0]


@pytest.mark.parametrize("pulses, why", [
    ([-9000, 4500, -560], "begin with a mark"),
    ([9000, -4500, 560], "final gap"),
    ([9000, 0, -560], "non-zero"),
    ([40000, -560], "longer than"),
])
def test_a_waveform_an_880_cannot_hold_is_refused_and_said(pulses, why):
    with pytest.raises(ir.IRError, match=why):
        ir.encode_press(pulses, 50_000)


def test_a_carrier_keeps_the_nanoseconds_it_had_when_it_is_still_that_carrier():
    assert ir.carrier_fields(37773, 26474, 13237) == (26474, 13237)
    assert ir.carrier_fields(38000, 26474, 13237) == (26315, 13157)


# -- reading and writing real images --------------------------------------------------------


@pytest.mark.skipif(not REAL, reason="no real 880 configurations in configs/880/")
@pytest.mark.parametrize("path", REAL, ids=lambda p: p.name)
def test_every_command_of_every_image_is_written_back_as_it_was_read(path):
    """Decode each record, write them all again, read the result: the same carrier, the same
    pulses, the same sections - and the image still passes the start check and every screen
    still reads."""
    payload = _payload(path)
    signals = {}
    for record in _records(payload):
        try:
            signals[(record.group_index, record.command_index)] = ir.decode(record)
        except ir.IRError:
            continue
    assert signals
    changes = {key: ir.lower(signal, native=signal["native"]) for key, signal in signals.items()}
    written, notes = ir.replace(payload, changes)
    tptp.validate(written)
    assert len(notes) == len(changes)
    back = ir.read_back(written)
    assert [key for key in signals if not ir.same_meaning(signals[key], back[key])] == []
    _runs, report = screens.text_runs(written)
    assert report["unreadable"] == 0 and report["unterminated"] == 0


def test_a_decoded_press_is_the_block_the_image_has():
    """The first NEC record of Update.EZHex: its press block, rebuilt from what was read, is
    the bytes that are there (apart from the close word, which the reader folds into the
    last space)."""
    payload = _named("Update.EZHex")
    record = _records(payload)[0]
    signal = ir.decode(record)
    block = record.blocks[0]
    again = ir.encode_press(signal["pulses_us"][:signal["sections"]["intro_pulses"]],
                            signal["native"]["entry_us"][0])
    assert again == payload[block.offset:block.offset + block.byte_length]


def test_a_press_with_a_repeat_is_one_signal_with_sections():
    payload = _named("Update.EZHex")
    signal = ir.decode(_records(payload)[0])
    sections = signal["sections"]
    assert sections["repeat_pulses"] > 0 and sections["intro_pulses"] % 2 == 0
    assert sections["intro_pulses"] + sections["repeat_pulses"] == len(signal["pulses_us"])


def test_changing_one_command_changes_only_that_command_and_adds_a_few_hundred_bytes():
    payload = _named("Update.EZHex")
    before = ir.read_back(payload)
    key = (1, 3)
    native = before[key]["native"]
    low = ir.lower(_waveform(native), native=native)
    written, notes = ir.replace(payload, {key: low})
    tptp.validate(written)
    after = ir.read_back(written)
    assert len(after) == len(before)
    assert [k for k in before if k != key and not ir.same_meaning(before[k], after[k])] == []
    assert after[key]["pulses_us"] == NEC and after[key]["carrier_hz"] in (38000, 38001)
    assert 0 < len(written) - len(payload) < 600
    assert len(notes) == 1


def test_nothing_that_was_there_moves_when_a_command_is_written():
    payload = _named("Update.EZHex")
    native = ir.read_back(payload)[(0, 0)]["native"]
    written, _notes = ir.replace(payload, {(0, 0): ir.lower(_waveform(native), native=native)})
    entry = None
    for group in read_arch8_ir(payload, hconfig.decompile(payload)).groups:
        if group.index == 0:
            entry = group.offset + 3
    old = bytearray(payload[:-6])
    new = bytearray(written[:len(old)])
    differing = [i for i in range(len(old)) if old[i] != new[i]]
    # the group's entry, the header's end vector and the checksum are all that change
    assert set(differing) <= set(range(entry, entry + 3)) | set(range(4, 8))


def test_a_command_the_image_cannot_be_written_to_does_not_change_the_image():
    payload = _named("Update.EZHex")
    with pytest.raises(ir.IRError, match="no command"):
        ir.replace(payload, {(0, 999): ir.lower(_waveform({}))})
    assert ir.replace(payload, {}) == (payload, [])


def test_a_protocol_signal_is_written_as_the_frames_it_renders():
    payload = _named("Update.EZHex")
    signal = ir_protocol_nec()
    native = ir.read_back(payload)[(0, 2)]["native"]
    low = ir.lower(signal, native=native)
    written, _notes = ir.replace(payload, {(0, 2): low})
    back = ir.read_back(written)[(0, 2)]
    assert back["carrier_hz"] in (38000, 38001)
    assert back["pulses_us"][:2] == [9000, -4500] and len(back["pulses_us"]) >= 66


def ir_protocol_nec() -> dict:
    from afterglow import ir_signal

    catalog = ir_protocol.catalog()
    nec = next(pid for pid in sorted(catalog) if pid.lower().startswith("nec"))
    spec = catalog[nec]
    parameters = {name: 0 for name in (spec.get("parameters") or {})}
    return ir_signal.protocol_signal(nec, parameters, name="x")


# -- through the backend --------------------------------------------------------------------


class _Context:
    def __init__(self, root: Path):
        self.root, self.lines = root, []

    def log(self, message):
        self.lines.append(message)


def _import(payload: bytes):
    return backend.import_project(payload, remotes.get("harmony-880"), {}).project


def _build(project: dict, tmp_path: Path):
    project = {**project, "settings": {**project["settings"],
                                       "out_file": str(tmp_path / "built.ezhex")}}
    context = _Context(tmp_path)
    result = backend.build_project(project, remotes.get("harmony-880"), context)
    return result, _payload(Path(result.artifact)), context


def test_an_import_reads_each_command_with_the_record_it_came_from():
    project = _import(_named("Update.EZHex"))
    for device in project["devices"]:
        assert device["signals"]
        for signal in device["signals"].values():
            native = signal["native"]
            assert {"group", "record", "digest"} <= set(native)


def test_a_project_that_changed_no_command_writes_none_and_builds_the_image_it_read(tmp_path):
    payload = _named("Update.EZHex")
    project = _import(payload)
    assert backend._ir_changes(project["devices"]) == ({}, [])
    result, built, _context = _build(project, tmp_path)
    assert built == payload and result.changes["ir"] == []


def test_a_command_changed_in_the_project_is_written_in_its_record(tmp_path):
    payload = _named("Update.EZHex")
    project = _import(payload)
    device = project["devices"][1]
    name = next(iter(device["signals"]))
    native = device["signals"][name]["native"]
    device["signals"][name] = _waveform(native)
    result, built, context = _build(project, tmp_path)
    tptp.validate(built)
    back = ir.read_back(built)[(native["group"], native["record"])]
    assert back["pulses_us"] == NEC
    assert len(result.changes["ir"]) == 1
    assert any(line.startswith("ir: group") for line in context.lines)
    assert result.changes["warnings"] == []


def test_a_renamed_command_is_still_written_in_the_record_it_was_read_from(tmp_path):
    payload = _named("Update.EZHex")
    project = _import(payload)
    device = project["devices"][0]
    old = next(iter(device["signals"]))
    native = device["signals"].pop(old)["native"]
    device["signals"]["Power"] = _waveform(native)
    _result, built, _context = _build(project, tmp_path)
    assert ir.read_back(built)[(native["group"], native["record"])]["pulses_us"] == NEC


def test_a_command_that_cannot_be_written_is_said_and_keeps_what_it_sent(tmp_path):
    payload = _named("Update.EZHex")
    project = _import(payload)
    device = project["devices"][0]
    name = next(iter(device["signals"]))
    native = device["signals"][name]["native"]
    device["signals"][name] = _waveform(native)
    del device["signals"][name]["carrier_hz"]
    result, built, _context = _build(project, tmp_path)
    assert built == payload
    assert any("is not written" in w and "carrier" in w for w in result.changes["warnings"])


def test_a_command_that_is_new_is_said_not_to_be_written(tmp_path):
    payload = _named("Update.EZHex")
    project = _import(payload)
    project["devices"][0]["signals"]["Brand New"] = _waveform({})
    result, built, _context = _build(project, tmp_path)
    assert built == payload
    assert any("'Brand New'" in w and "is new" in w for w in result.changes["warnings"])
