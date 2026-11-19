"""An 880's infrared commands: read them as portable signals, write a changed one back.

A command is a *record* in an IR group (section 5): a carrier and up to sixteen sets of
three duration blocks - the press, what is sent again while the key is held, and the
release. A block is u16 words, a mark with the top bit set and a space without, ended by a
zero. Spaces over 0x7FFF are split across words, and a press starts with a long lead-in
space and ends on a 1 microsecond close word - the same words the Harmony One and 800 use,
and read back identically here from the sixteen published and supplied images.

    record  00 | period ns (u24) | carrier-on ns (u24) | 01 | its own start (u24) |
            sets (u8) | three u24 block addresses for each set, 0 for none

Nothing but the group's table points at a record, and a record's blocks lie below it. A
command is therefore changed by adding its new blocks and then a new record at the end of
the image, and pointing the group's entry at the record: nothing that is there moves, and
what the old record pointed at is left unreferenced.

Format reference: docs/harmony_880/ir.md
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from ... import ir_protocol, ir_signal
from . import tptp

MARK = 0x8000
MAX_DURATION_US = 0x7FFF
DEFAULT_ENTRY_US = 50_000
CLOSE_US = 1
RECORD_CLASS = 1
RECORD_HEADER = 12
SET_BYTES = 9
MAX_SETS = 16
MAX_WORDS = 4000


class IRError(ValueError):
    """A command that cannot be written to an 880 as it stands."""


# -- encoding -------------------------------------------------------------------------------


def _split_space(duration_us: int) -> list[int]:
    if duration_us <= MAX_DURATION_US:
        return [duration_us]
    full, remainder = divmod(duration_us, MAX_DURATION_US)
    if not remainder:
        return [MAX_DURATION_US] * full
    if remainder >= MAX_DURATION_US // 2:
        return [MAX_DURATION_US] * full + [remainder]
    combined = MAX_DURATION_US + remainder
    return [MAX_DURATION_US] * (full - 1) + [combined // 2, combined - combined // 2]


def _merged(pulses_us) -> list[int]:
    out: list[int] = []
    for pulse in pulses_us:
        if isinstance(pulse, bool) or not isinstance(pulse, int) or pulse == 0:
            raise IRError("a waveform's pulses must be non-zero integers")
        if out and (out[-1] > 0) == (pulse > 0):
            out[-1] += pulse
        else:
            out.append(pulse)
    return out


def _words(pulses_us) -> list[int]:
    words: list[int] = []
    for pulse in _merged(pulses_us):
        if pulse > 0:
            if pulse > MAX_DURATION_US:
                raise IRError(f"a mark of {pulse} us is longer than the 15-bit field holds")
            words.append(MARK | pulse)
        else:
            words.extend(_split_space(-pulse))
    return words


def _bytes(words: list[int]) -> bytes:
    if len(words) > MAX_WORDS:
        raise IRError(f"a waveform of {len(words)} words is longer than the {MAX_WORDS} a "
                      "reader walks")
    return b"".join(word.to_bytes(2, "little") for word in words)


def encode_press(pulses_us, entry_us: int | None) -> bytes:
    """A press block: the lead-in space, the frame, the close word and the zero."""
    pulses = _merged(pulses_us)
    if not pulses or pulses[0] < 0:
        raise IRError("a command's waveform must begin with a mark")
    if pulses[-1] > 0:
        raise IRError("a command's waveform needs a final gap before it ends")
    words = []
    if entry_us is not None:
        entry = _split_space(entry_us)
        if entry[0] != MAX_DURATION_US:
            raise IRError("the lead-in space must be longer than 32767 us")
        words.extend(entry)
    words.extend(_words(pulses))
    words.extend([CLOSE_US, 0])
    return _bytes(words)


def encode_hold(pulses_us) -> bytes:
    return encode_press(pulses_us, None)


def encode_release(pulses_us) -> bytes:
    pulses = _merged(pulses_us)
    if not pulses or pulses[0] < 0:
        raise IRError("a release waveform must begin with a mark")
    return _bytes(_words(pulses) + [0])


def carrier_fields(carrier_hz: int, period_ns: int | None = None,
                   on_ns: int | None = None) -> tuple[int, int]:
    """The record's period and carrier-on time. A carrier the record already had keeps the
    nanoseconds it had: a frequency in hertz is a rounding of them."""
    if isinstance(carrier_hz, bool) or not isinstance(carrier_hz, int) or carrier_hz <= 0:
        raise IRError("an 880 command needs a positive integer carrier")
    if period_ns and on_ns and round(1_000_000_000 / period_ns) == carrier_hz:
        return period_ns, on_ns
    period = 1_000_000_000 // carrier_hz
    if not 0 < period <= 0xFFFFFF:
        raise IRError("the carrier's period does not fit its u24 field")
    return period, period // 2


# -- lowering a portable signal to its sets of waveforms ------------------------------------


@dataclass(frozen=True)
class Phases:
    """The pulses of one set of a record: ``press`` and, if there are any, ``hold`` (sent
    again while the key is down) and ``release``."""
    press: tuple[int, ...]
    hold: tuple[int, ...] | None = None
    release: tuple[int, ...] | None = None


@dataclass(frozen=True)
class Lowered:
    carrier_hz: int
    sets: tuple[Phases, ...]            # one, or two for a command that toggles
    entry_us: tuple[int, ...]           # each set's lead-in space before its press
    period_ns: int | None = None        # what the record it replaces had, kept if the
    on_ns: int | None = None            # carrier is still the one they make


def _recording_phases(recording: dict) -> Phases:
    pulses = recording["pulses_us"]
    sections = recording.get("sections")
    if not sections:
        return Phases(tuple(pulses))
    intro, repeat = sections["intro_pulses"], sections["repeat_pulses"]
    release = sections.get("release_pulses", 0)
    press, hold = pulses[:intro], pulses[intro:intro + repeat]
    rel = pulses[intro + repeat:intro + repeat + release]
    return Phases(tuple(press or hold), tuple(hold) or None, tuple(rel) or None)


def _protocol_phases(signal: dict, definition: dict, state, library):
    press, state = ir_protocol.render_transmission(signal, phase="press", state=state,
                                                   library=library)
    hold, state = ir_protocol.render_transmission(signal, phase="hold", state=state,
                                                  library=library)
    release, state = ir_protocol.render_transmission(signal, phase="release", state=state,
                                                     library=library)
    if press is None:
        raise IRError(f"protocol {signal['protocol']!r} has an empty press phase")
    press_pulses = list(press["pulses_us"])
    hold_pulses = list(hold["pulses_us"]) if hold is not None else None
    release_pulses = list(release["pulses_us"]) if release is not None else None
    minimum = ir_protocol.hold_minimum(definition, signal.get("transmission"))
    if minimum and hold_pulses is None:
        raise IRError(f"protocol {signal['protocol']!r} requires hold frames but has none")
    for _ in range(minimum):
        press_pulses.extend(hold_pulses or [])
    if release_pulses and not any(pulse > 0 for pulse in release_pulses):
        press_pulses.extend(release_pulses)         # a wait after the command, not a release
        release_pulses = None
    return Phases(tuple(press_pulses), tuple(hold_pulses) if hold_pulses else None,
                  tuple(release_pulses) if release_pulses else None), state


def lower(signal: dict, *, library=None, native: dict | None = None) -> Lowered:
    """A portable signal as the sets a record holds. ``native`` is what reading the command
    it replaces kept (``decode``): the lead-in space of each set, which is the default
    where there is none to say, and the carrier's period."""
    native = native or {}
    ir_signal.validate(signal)
    if signal["kind"] == "backend-opaque":
        raise IRError("an opaque command has no portable IR meaning to write")
    if signal["kind"] == "waveform":
        carrier = signal.get("carrier_hz")
        if carrier is None:
            raise IRError("an 880 cannot write a waveform without a carrier")
        sets = [_recording_phases(recording) for recording in ir_signal.recordings(signal)]
    else:
        definition = ir_protocol.protocol(signal["protocol"], library)
        state = definition.get("state") or {}
        if any(entry.get("kind") != "toggle" or entry.get("advance", "press") != "press"
               for entry in state.values()):
            raise IRError(f"protocol {signal['protocol']!r} keeps sender state other than a "
                          "toggle that changes with each press")
        modulation = definition["modulation"]
        if modulation["kind"] != "carrier":
            raise IRError(f"protocol {signal['protocol']!r} is unmodulated; an 880 command "
                          "needs a measured carrier")
        carrier = modulation["carrier_hz"]
        first, after = _protocol_phases(signal, definition, None, library)
        sets = [first]
        if state:
            sets.append(_protocol_phases(signal, definition, after, library)[0])
    given = list(native.get("entry_us") or [])
    entries = tuple(given[index] if index < len(given) and given[index] > MAX_DURATION_US
                    else DEFAULT_ENTRY_US for index in range(len(sets)))
    return Lowered(int(carrier), tuple(sets), entries,
                   native.get("period_ns"), native.get("on_ns"))


# -- reading a record as a portable signal --------------------------------------------------


def _signed(block) -> list[int]:
    return [pulse.microseconds if pulse.mark else -pulse.microseconds
            for pulse in block.pulses()]


def _read_set(blocks) -> tuple[Phases, int | None]:
    press = _signed(blocks[0]) if blocks[0] is not None else []
    entry = None
    if press and press[0] < 0:
        entry = -press[0]
        press = press[1:]
    if not press or press[0] < 0 or press[-1] > 0:
        raise IRError("a press that does not begin with a mark and end on a gap")
    press[-1] += 1                                          # the close word
    if press[-1] == 0:
        raise IRError("a press that ends on its close word")
    hold = _signed(blocks[1]) if blocks[1] is not None else None
    if hold:
        if hold[0] < 0 or hold[-1] > 0:
            raise IRError("a hold that does not begin with a mark and end on a gap")
        hold[-1] += 1
        if hold[-1] == 0:
            raise IRError("a hold that ends on its close word")
    release = _signed(blocks[2]) if blocks[2] is not None else None
    if release and release[0] < 0:
        raise IRError("a release that does not begin with a mark")
    return Phases(tuple(press), tuple(hold) if hold else None,
                  tuple(release) if release else None), entry


def _recording(phases: Phases) -> dict:
    pulses = list(phases.press) + list(phases.hold or []) + list(phases.release or [])
    out: dict = {"pulses_us": pulses}
    if phases.hold or phases.release:
        sections = {"intro_pulses": len(phases.press), "repeat_pulses": len(phases.hold or [])}
        if phases.release:
            sections["release_pulses"] = len(phases.release)
        out["sections"] = sections
    return out


def decode(record, *, name: str = "") -> dict:
    """The command a record is, as a portable waveform signal; ``IRError`` if the record
    is in a shape this cannot express (it is then left as it is)."""
    if record.carrier_hz is None:
        raise IRError("a record with no carrier")
    sets, entries = [], []
    for index in range(record.pointer_group_count):
        phases, entry = _read_set(record.blocks[3 * index:3 * index + 3])
        sets.append(phases)
        entries.append(entry)
    first = _recording(sets[0])
    alternate = _recording(sets[1]) if len(sets) > 1 else None
    if len(sets) > 2:
        raise IRError(f"a record of {len(sets)} sets")
    signal = ir_signal.waveform(
        first["pulses_us"], name=name, carrier_hz=round(record.carrier_hz),
        sections=first.get("sections"), alternate=alternate,
        provenance={"kind": "imported", "tool": "harmony-880"},
        native={"group": record.group_index, "record": record.command_index,
                "entry_us": [e or 0 for e in entries],
                "period_ns": record.period_ns, "on_ns": record.carrier_on_ns})
    return signal


# -- telling a changed command from one that is as it was imported --------------------------


def digest(signal: dict) -> str:
    """What a command says, without its name, its provenance or where it came from: what
    changing a command changes."""
    kept = {key: value for key, value in signal.items()
            if key not in ("name", "native", "provenance")}
    return hashlib.sha256(json.dumps(kept, sort_keys=True).encode()).hexdigest()[:16]


# -- writing --------------------------------------------------------------------------------


def _record_sets(lowered: Lowered) -> list[tuple[bytes, bytes | None, bytes | None]]:
    out = []
    for phases, entry in zip(lowered.sets, lowered.entry_us):
        out.append((
            encode_press(phases.press, entry),
            encode_hold(phases.hold) if phases.hold else None,
            encode_release(phases.release) if phases.release else None,
        ))
    return out


def replace(blob: bytes, changes: dict[tuple[int, int], Lowered]) -> tuple[bytes, list[str]]:
    """``blob`` with each command (group, record) written as ``changes`` says, sealed again.

    The image is read first, so a group or record that does not read stops the write
    before anything is changed. Returns the new image and a note for each command."""
    from .decompiler import hconfig
    from .decompiler.ir_reader import read_arch8_ir

    if not changes:
        return blob, []
    if bytes(blob[-4:]) != tptp.CLOSE:
        raise ValueError("the image has lost its closing DKDK; not editing it")
    database = read_arch8_ir(blob, hconfig.decompile(blob))
    data = bytearray(blob[:-6])
    notes = []
    for (group_index, command), lowered in sorted(changes.items()):
        if group_index >= len(database.groups) or \
                command >= len(database.groups[group_index].records):
            raise IRError(f"group {group_index} has no command {command}")
        group = database.groups[group_index]
        sets = _record_sets(lowered)
        addresses: list[int] = []
        for press, hold, release in sets:
            for raw in (press, hold, release):
                if raw is None:
                    addresses.append(0)
                else:
                    addresses.append(tptp.BASE + len(data))
                    data.extend(raw)
        start = tptp.BASE + len(data)
        period, half = carrier_fields(lowered.carrier_hz, lowered.period_ns, lowered.on_ns)
        header = (b"\x00" + period.to_bytes(3, "little") + half.to_bytes(3, "little")
                  + bytes([RECORD_CLASS]) + start.to_bytes(3, "little") + bytes([len(sets)])
                  + b"".join(address.to_bytes(3, "little") for address in addresses))
        assert len(header) == RECORD_HEADER + SET_BYTES * len(sets)
        data.extend(header)
        entry = group.offset + 3 + 3 * command
        data[entry:entry + 3] = (start + 7).to_bytes(3, "little")
        notes.append(f"group {group_index} command {command}: {lowered.carrier_hz} Hz, "
                     f"{len(sets)} set(s), record at {start - tptp.BASE:#x}")
    return tptp.seal(bytes(data)), notes


def read_back(blob: bytes) -> dict[tuple[int, int], dict]:
    """Every command of an image as a portable signal, by (group, record); one that does
    not read is left out."""
    from .decompiler import hconfig
    from .decompiler.ir_reader import read_arch8_ir

    database = read_arch8_ir(blob, hconfig.decompile(blob))
    out = {}
    for group in database.groups:
        for record in group.records:
            try:
                out[(group.index, record.command_index)] = decode(record)
            except (IRError, ValueError):
                continue
    return out


def same_meaning(first: dict, second: dict) -> bool:
    """Whether two waveform signals send the same thing: their carrier, pulses, sections
    and alternate."""
    keep = ("carrier_hz", "pulses_us", "sections", "alternate")
    return all(first.get(key) == second.get(key) for key in keep)
