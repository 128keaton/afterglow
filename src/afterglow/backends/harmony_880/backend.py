"""Harmony 880 (arch 8) backend: reads an imported image, relabels it, and refuses what the
remote's start check would refuse.

Import reads the devices an image holds and the infrared commands of each, and keeps the
image itself with the project (``settings["h880_base"]``). A build starts from that image -
never from whatever backup is newest - renames the devices whose labels changed, closes
the image again (``tptp.seal``) and checks it the way the remote does before anything is
written (``tptp.validate``). It cannot yet build a configuration from nothing.

Format reference: docs/harmony_880/tptp.md
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import zlib
from pathlib import Path
from typing import Any

from ..contracts import BuildResult, ImportResult
from . import labels, tptp

log = logging.getLogger(__name__)

MAGIC = tptp.MAGIC
# The project keeps the image it was imported from, so a build never reaches for a file
# somewhere on the disk. {"sha256", "size", "zlib_b64"}.
BASE_KEY = "h880_base"
LEGACY_BASE_KEY = "h880_payload"

# Which key raises which scan code, as far as Logitech's own compiles for skin 15 can say
# (the community's harmony-explorations, tests/harmony_880_885/test_harmony_880_keys.py).
# No 880 has been pressed on a unit; every one of these is the code the same key raised on
# an 885 that was. A code not here is left as "Button <code>", not guessed.
KEY_CODES = {
    "VolumeMute": 11, "VolumeUp": 13, "DirectionLeft": 14, "VolumeDown": 15,
    "DirectionUp": 16, "Menu": 17, "Exit": 18, "Stop": 21, "Record": 22, "Rewind": 23,
    "Number1": 25, "Number4": 26, "Number8": 27, "Number7": 29, "Number0": 32,
    "Select": 33, "DirectionDown": 34, "DirectionRight": 35, "ChannelDown": 36,
    "ChannelUp": 37, "Info": 51, "FastForward": 54, "Pause": 55, "Play": 56,
    "Number2": 57, "Number3": 58, "Number6": 59, "Number5": 61, "Number9": 62,
}
H880_BUTTONS = {code: key for key, code in KEY_CODES.items()}

_TYPE_MAP = {
    "tv": "Television",
    "television": "Television",
    "digital_set_top_box": "CableBox",
    "set_top_box": "CableBox",
    "cable": "CableBox",
    "satellite": "Satellite",
    "game_console": "GameConsole",
    "game": "GameConsole",
    "mini_system": "Receiver",
    "receiver": "Receiver",
    "amplifier": "Amplifier",
    "dvd": "DvdCd",
    "with_dvd": "DvdCd",
    "pvr": "Pvr",
    "dvr": "Pvr",
}

# The Logitech type of each of the 880's activities, by position.
_ACTIVITY_TYPES = ["VirtualTelevisionN", "VirtualDvd", "VirtualCdMulti",
                   "VirtualGameConsole", "VirtualGeneric", "VirtualGeneric"]


def _group_kind(commands: list[dict]) -> str:
    """NEC / Samsung / RC6 from the first command's leader."""
    for c in commands:
        pulses = c.get("pulses") or []
        started = False
        mark = None
        for p in pulses:
            if not started:
                if not p.get("mark"):
                    continue
                started = True
            mark = int(p.get("us") or 0)
            break
        if mark is None:
            continue
        if mark > 7000:
            return "nec"
        if 3000 < mark < 4000:
            return "samsung"
        if 2400 < mark < 2900:
            return "rc6"
    return "other"


def _pair_groups(records: list[dict], groups: list[list]) -> list[tuple]:
    """RC6 is the Xbox. Samsung is the cable box. NEC prefers the TV."""
    if not groups:
        return [(r, "other") for r in records] or [({"stem": "Device_1", "power": 0, "inputs": 0}, "other")]
    kinds = [_group_kind(g) for g in groups]
    unused = list(records)
    paired: list[tuple | None] = [None] * len(groups)

    def take(pred) -> dict | None:
        for i, rec in enumerate(unused):
            if pred(rec["stem"].lower()):
                return unused.pop(i)
        return None

    for i, kind in enumerate(kinds):
        if kind == "rc6":
            rec = take(lambda s: "game" in s or "xbox" in s or "console" in s)
        elif kind == "samsung":
            # Cox/Cisco set-tops use this leader; Hitachi/Pioneer TVs are NEC.
            rec = take(lambda s: "set_top" in s or "cable" in s or "digital" in s)
        elif kind == "nec":
            rec = take(lambda s: s == "tv" or s.startswith("tv_"))
        else:
            rec = None
        if rec:
            paired[i] = (rec, kind)
    for i, kind in enumerate(kinds):
        if paired[i] is not None:
            continue
        rec = None
        if kind == "nec":
            for j, cand in enumerate(unused):
                if "mini" in cand["stem"].lower() or "system" in cand["stem"].lower():
                    rec = unused.pop(j)
                    break
        if rec is None:
            rec = unused.pop(0) if unused else {"stem": f"Device_{i+1}", "power": 0, "inputs": 0}
        paired[i] = (rec, kind)
    return paired


def _device_type(stem: str) -> str:
    return _TYPE_MAP.get(stem.lower().replace(" ", "_"), "HomeAppliance")


def _activities(blob: bytes, devices: list[dict]) -> list[dict]:
    """Activity count from CurrentActivityState_N; labels from the usual 880 set."""
    text = blob.decode("latin-1", errors="ignore") if blob else ""
    count = 0
    m = re.search(r"CurrentActivityState_\d+_(\d+)", text)
    if m:
        count = max(0, int(m.group(1)) - 1)  # state count includes Off
    if count <= 0:
        return []
    labels = ["Watch TV", "Watch a DVD", "Listen to music", "Play a game",
              "Custom", "Activity"]
    tv = next((d["id"] for d in devices if d.get("type") == "Television"), None)
    out = []
    for i in range(count):
        label = labels[i] if i < len(labels) else f"Activity {i + 1}"
        # the activity menu draws each name as its own short run, not the label we guess
        runs = [["Watch", "a DVD"], ["Listen", "to CDs"], ["Play", "Xbox 360"]]
        out.append({
            "id": str(i + 1),
            "label": label,
            "_display": label,
            "_runs": runs[i] if i < len(runs) else [label],
            "type": _ACTIVITY_TYPES[i] if i < len(_ACTIVITY_TYPES) else "VirtualGeneric",
            "power_on_devices": [tv] if tv and i == 0 else [],
            "enter": [],
            "leave": [],
            "display": tv,
            "control": tv,
        })
    return out


def _pack_base(blob: bytes) -> dict:
    """The image as the project keeps it."""
    return {"sha256": hashlib.sha256(blob).hexdigest(), "size": len(blob),
            "zlib_b64": base64.b64encode(zlib.compress(blob, 9)).decode("ascii")}


def _base_image(settings: dict) -> bytes:
    """The image a build starts from: the one the project was imported from, checked.

    Never the newest file in a dumps folder: a damaged image read back from the remote
    would then become the next build's input, which is how one bad write became eleven.
    """
    packed = settings.get(BASE_KEY)
    if packed:
        blob = zlib.decompress(base64.b64decode(packed["zlib_b64"]))
        if hashlib.sha256(blob).hexdigest() != packed.get("sha256"):
            raise ValueError("the image kept with this project does not match its digest")
    else:
        legacy = settings.get(LEGACY_BASE_KEY)
        if not legacy or not Path(legacy).is_file():
            raise NotImplementedError(
                "no imported image to build from. Import a Harmony 880 configuration into "
                "this project first: a configuration cannot be built from nothing yet.")
        blob = Path(legacy).read_bytes()
    tptp.validate(blob)         # a base the remote would refuse is refused here, not patched
    return blob


def _flash_base(blob: bytes) -> int:
    """Where the configuration is mapped: the firmware's constant, which every image shares."""
    return tptp.BASE


def _seal(data: bytearray) -> bytearray:
    """Close an image again: end vector, checksum and DKDK, from its body.

    Refuses an image whose trailer is already gone. Sealing one anyway would cut six bytes
    of real content off it.
    """
    if bytes(data[-4:]) != tptp.CLOSE:
        raise ValueError("the image has lost its closing DKDK; not repairing it by guessing")
    return bytearray(tptp.seal(bytes(data[:-6])))


def validate_payload(payload: bytes, _profile=None) -> dict:
    """The start check and the measured layout (tptp.validate); raises ValueError."""
    return tptp.validate(payload)


def compare_payloads(recovery: bytes, candidate: bytes, _profile) -> dict:
    """What a write changes, section by section, for review before it is written
    (first_write.py): each section's digest, since a section's contents are not read."""
    def sections(payload):
        return {f"section {index}": hashlib.sha256(payload[start:end]).hexdigest()
                for index, (start, end) in tptp.section_ranges(payload).items()}

    before, after = sections(recovery), sections(candidate)
    return {
        "kind": "tptp-section-sha256",
        "size before": len(recovery),
        "size after": len(candidate),
        "added": sorted(after.keys() - before.keys()),
        "removed": sorted(before.keys() - after.keys()),
        "changed": sorted(name for name in before.keys() & after.keys()
                          if before[name] != after[name]),
        "unchanged": sum(before[name] == after[name] for name in before.keys() & after.keys()),
    }


def afterglow_built(payload: bytes) -> bool | None:
    return False


def _read_payload(source: Any) -> bytes:
    if isinstance(source, (str, Path)):
        p = Path(source)
        if p.is_dir():
            bin_path = p / "payload.bin"
            if bin_path.is_file():
                return bin_path.read_bytes()
        if p.is_file():
            return p.read_bytes()
    if hasattr(source, "read"):
        return source.read()
    if isinstance(source, (bytes, bytearray)):
        return bytes(source)
    raise ValueError(f"cannot read payload from {type(source)}")


def _device_records(blob: bytes) -> list[dict]:
    """One record per device stem, with power/input counts where the name table has them."""
    text = blob.decode("latin-1", errors="ignore")
    found: dict[str, dict] = {}

    def add(stem: str) -> dict:
        stem = stem.strip("_")
        return found.setdefault(stem, {"stem": stem, "power": 0, "inputs": 0})

    for m in re.finditer(
        r"([A-Za-z][A-Za-z0-9]*(?:[_-][A-Za-z0-9]+)*)_(Power|Input|InputType|CompAV|AntInput)_(\d+)",
        text,
    ):
        stem, kind, count = m.group(1), m.group(2), int(m.group(3))
        if stem.startswith("Current") or len(stem) < 2:
            continue
        rec = add(stem)
        if kind == "Power":
            rec["power"] = max(rec["power"], count)
        else:
            rec["inputs"] = max(rec["inputs"], count)
    # Full device titles from the name table, including parenthetical ones.
    for m in re.finditer(
        r"([A-Z][A-Za-z0-9_() ,.-]{2,60}?)_(?:Power|Input|InputType|CompAV|AntInput|TunerInput)_\d+",
        text,
    ):
        stem = m.group(1).strip("_")
        if len(stem) >= 2 and not stem.startswith("Current"):
            add(stem)
    return sorted(found.values(), key=lambda r: r["stem"])


def _ir_and_bindings(blob: bytes):
    """The infrared groups (each a list of commands with its pulses) and the key each
    command is bound to, read with the vendored decompiler. Raises if the image cannot be
    read; the caller says so, nothing is swallowed here."""
    from .decompiler import hconfig
    from .decompiler.ir_reader import read_arch8_ir

    doc = hconfig.decompile(blob)
    db = read_arch8_ir(blob, doc)
    sec11 = next((r for r in doc["blob"]["regions"]
                  if r.get("kind") == "pointer_table" and r.get("section") == 11), None)
    # Action lists have no id; section 11 targets are raw offsets.
    als = {r["offset"]: r for r in doc["blob"]["regions"]
           if r.get("kind") == "action_list"}
    binding: dict[tuple[int, int], str] = {}
    if sec11:
        for kt in doc["blob"]["regions"]:
            if kt.get("kind") != "key_table":
                continue
            for e in kt.get("entries", []):
                try:
                    code = int(e.get("code", "0"), 16)
                except ValueError:
                    continue
                tgt = e.get("target")
                if not isinstance(tgt, int) or tgt >= len(sec11["targets"]):
                    continue
                off = sec11["targets"][tgt]
                if not isinstance(off, int):
                    continue
                al = als.get(off)
                if not al:
                    continue
                for ins in al.get("instructions", []):
                    if ins.get("opcode") == "0x7D":
                        op = ins["operand"]
                        # High bit is a flag; low 7 bits are the button id.
                        binding[(op >> 8, op & 0xFF)] = H880_BUTTONS.get(
                            code & 0x7F, f"Button {code & 0x7F}")
    groups = []
    for gi, g in enumerate(db.groups):
        cmds = []
        used: dict[str, int] = {}
        for ri, rec in enumerate(g.records):
            try:
                pulses = []
                for b in (rec.blocks or []):
                    if b is None or not hasattr(b, "pulses"):
                        continue
                    for p in b.pulses():
                        pulses.append({"mark": bool(p.mark), "us": int(p.microseconds)})
                btn = binding.get((gi, ri))
                # Unbound commands are "Command XX" so Find model apply()
                # renames them to the codeset's names (Input, Guide, ...).
                # Digit-free button names are sampled first.
                if btn:
                    base = btn.replace(" ", "_").replace("+", "Up").replace("-", "Down")
                    label = btn
                else:
                    base = f"Command {ri:02X}"
                    label = f"Command {ri:02X}"
                n = used.get(base, 0)
                used[base] = n + 1
                name = base if n == 0 else f"{base}_{n + 1}"
                cmds.append({
                    "name": name,
                    "label": label,
                    "button": btn,
                    "carrier": round(rec.carrier_hz) if rec.carrier_hz else None,
                    "pulses": pulses[:120],
                })
            except Exception:       # one unreadable record must not hide the others
                log.warning("880 import: group %d record %d is unreadable", gi, ri,
                            exc_info=True)
                continue
        groups.append(cmds)
    return groups, binding


def import_project(source: Any, profile: Any, context: dict) -> ImportResult:
    """Read an image's devices and their infrared commands, and keep the image.

    An image the remote would refuse is refused (ValueError). What cannot be read -
    the infrared records, say - is said in the warnings and counted in ``rejected``; the
    devices are still listed, with no commands.
    """
    blob = _read_payload(source)
    validation = validate_payload(blob, profile)
    warnings = ["Manufacturer/model left blank so Find model can look up the archive."]
    rejected: dict[str, str] = {}
    try:
        ir_groups, binding = _ir_and_bindings(blob)
    except Exception as exc:                        # noqa: BLE001 - reported, below
        log.exception("could not read the infrared records of a Harmony 880 image")
        ir_groups = None
        rejected["infrared"] = f"{type(exc).__name__}: {exc}"
        warnings.append("The infrared records could not be read "
                        f"({type(exc).__name__}: {exc}), so the devices have no commands.")
    records = _device_records(blob)
    order = _pair_groups(records, ir_groups or [])
    log.debug("880 import: %d names, pairing %s", len(records),
              [(r["stem"], k) for r, k in order])
    devices = []
    for i, (rec, kind) in enumerate(order):
        name = rec["stem"]
        commands = []
        signals = {}
        if ir_groups and i < len(ir_groups):
            for c in ir_groups[i][:40]:
                commands.append([c["name"], c["label"], "", "", c.get("button") or ""])
                # Signed pulses for device_match. Drop a leading space: arch-8
                # records often start with a long gap before the NEC/etc leader,
                # and the fingerprint keys on the first mark.
                signed = []
                started = False
                for p in c["pulses"]:
                    us = int(p["us"])
                    if not started:
                        if not p["mark"]:
                            continue
                        started = True
                    signed.append(us if p["mark"] else -us)
                sig = {
                    "schema": "afterglow-ir-signal/1",
                    "kind": "waveform",
                    "name": c["label"],
                    "pulses_us": signed,
                    "carrier_hz": c["carrier"] or 38000,
                    "native": {"button": c["button"], "group_cmd": c["name"]},
                }
                # Non-NEC (RC5 etc.) often fails the waveform fingerprint.
                # If the catalog recognises it, keep a protocol form too so
                # device_match can render and look that up.
                try:
                    from ... import ir_recognise
                    hit = ir_recognise.recognise(sig)
                    if hit:
                        sig = hit
                except Exception:
                    pass
                signals[c["name"]] = sig
        label = name.replace("_", " ")
        _SCREEN = {
            "tv": "TV", "watch": "Watch", "listen": "Listen",
            "game console": "Game Console", "cd": "CD", "dvd": "DVD",
        }
        if label.lower() in _SCREEN:
            label = _SCREEN[label.lower()]
        devices.append({
            "schema": "afterglow-project-device/1",
            "id": f"d{i+1}",
            "label": label,
            "_display": label,
            "_imported": True,
            # Blank so the Devices tab shows "Find model" (device_match).
            "mfr": "",
            "model": "",
            "type": _device_type(name),
            "commands": commands,
            "signals": signals,
            "power": {"mode": "discrete"} if rec.get("power", 0) >= 2 else {"mode": "always_on"},
            "inputs": {"direct": [[f"Input {n+1}", ""] for n in range(rec.get("inputs") or 0)]}
                      if rec.get("inputs") else {},
        })
    log.debug("880 import: emitting %d device(s)", len(devices))

    activities = _activities(blob, devices)
    project = {
        "remote": getattr(profile, "id", "harmony-880"),
        "name": context.get("name") or "Harmony 880 import",
        "devices": devices,
        "activities": activities,
        "settings": {BASE_KEY: _pack_base(blob)},
        "notes": [f"names={len(records)} ir_groups={len(ir_groups) if ir_groups else 0} "
                  f"activities={len(activities)}"],
    }
    return ImportResult(
        project=project,
        preservation={"original_payload": blob, "validation": validation},
        warnings=tuple(warnings),
        rejected=rejected,
    )


def build_project(project: dict, profile: Any, context: Any) -> BuildResult:
    """Relabel the imported image for the devices whose names changed, close it, check it.

    The image is the one the project was imported from (``_base_image``). What is checked
    afterwards is what the remote's start check tests, plus the layout every known image
    has (``tptp.validate``): an image that fails is never written out.
    """
    import tempfile

    from ... import ezhex

    profile.require_buildable()
    settings = project.get("settings") or {}
    blob = _base_image(settings)
    original = len(blob)
    devices = project.get("devices") or []
    context.log(f"overwriting {len(devices)} device slot(s) in place")
    # names are changed where the image draws them (labels.rename); IR stays as imported
    renames = {}
    for item in devices:
        shown = item.get("_display") or ""
        want = item.get("label") or ""
        if shown and want and shown != want:
            renames[shown] = want
    # Activity menu runs are left alone. Replacing one corrupts the screen
    # program (old and new text both draw, and the remote drops into the
    # assistant). Device names only until the menu layout is understood.
    for item in project.get("activities") or []:
        want = item.get("label") or ""
        shown = item.get("_display") or ""
        if want and shown and want != shown:
            context.log(f"label: activity {shown!r} left as drawn")
    blob, notes = labels.rename(blob, renames)
    warnings = [note for note in notes if note.startswith(labels.WARNING)]
    for note in notes:
        context.log(note if note in warnings else "label: " + note)
    if warnings:
        context.log(f"{len(warnings)} name(s) did not fit and were cut off - see the warning "
                    "line(s) above")
    blob = bytes(_seal(bytearray(blob)))
    context.log(f"payload {len(blob)} bytes (imported was {original})")
    validation = tptp.validate(blob)       # raises, naming what the remote would refuse
    context.log("start check passes: cookie, marker, end marker, checksum "
                f"{validation['checksum']}")
    work = Path(tempfile.mkdtemp(prefix="h880_build_"))
    (work / "payload.bin").write_bytes(blob)
    output = Path(settings.get("out_file") or "home.ezhex")
    if not output.is_absolute():
        output = Path(context.root) / output
    if output.suffix.lower() != ".ezhex":
        output = output.with_suffix(".EZHex")
    output.parent.mkdir(parents=True, exist_ok=True)
    ezhex.pack_standalone(str(work), str(output), profile=profile, log=context.log)
    return BuildResult(artifact=output, validation=validation,
                       changes={"renamed": renames, "notes": notes, "warnings": warnings},
                       metadata={"backend": "harmony-880", "generator": "relabel-import",
                                 "size": len(blob)})


def migrate_legacy_device(source: dict, *, device_id: str = "",
                          library: Path | None = None) -> dict:
    out = dict(source)
    out.setdefault("schema", "afterglow-project-device/1")
    out.setdefault("label", out.get("name") or "Imported")
    out.setdefault("mfr", "Unknown")
    out.setdefault("model", out.get("label") or "Imported")
    out.setdefault("commands", [])
    out.setdefault("signals", {})
    if device_id:
        out.setdefault("id", device_id)
    return out


def name_activity_keys(project: dict) -> None:
    return None


def capability(signal: dict, profile, *, library=None) -> dict:
    """Whether an arch-8 remote can send a signal. Waveforms and named protocols are
    accepted; an opaque command from another remote is not."""
    kind = signal.get("kind")
    if kind == "backend-opaque":
        return {"supported": False, "strategy": "unavailable",
                "reason": "an opaque command from another remote has no meaning here"}
    if kind in ("waveform", "protocol"):
        return {"supported": True, "strategy": kind,
                "reason": "sent as the remote's own IR record"}
    return {"supported": False, "strategy": "unavailable",
            "reason": f"no arch-8 lowering for {kind!r}"}
