"""The Harmony 880's image checks: what the remote's start check refuses, and what a
writer that changes an image's length must do to keep it.

Real configurations are never committed. Tests that read them take
whatever is in ``configs/880/`` - intact images there, damaged ones in ``configs/880/damaged/``
- and skip when it is empty.

Format reference: docs/harmony_880/tptp.md
"""

from __future__ import annotations

import contextlib
import io
import struct
from pathlib import Path

import pytest

from afterglow import ezhex, remotes
from afterglow.backends import harmony_880  # noqa: F401  (the package exists)
from afterglow.backends.harmony_880 import backend, firmware, tptp

ROOT = Path(__file__).resolve().parent.parent
INTACT = sorted((ROOT / "configs" / "880").glob("*.[eE][zZ][hH][eE][xX]"))
DAMAGED = sorted((ROOT / "configs" / "880" / "damaged").glob("*.[eE][zZ][hH][eE][xX]"))


def _body(extra: int = 0) -> bytes:
    """An image up to, not including, its trailer: the header, 18 filled slots, the marker."""
    body = bytearray(0x64 + 8 * 18 + extra)
    body[0:4] = tptp.MAGIC
    struct.pack_into("<I", body, 8, tptp.FORMAT)
    filled = [i for i in range(tptp.SLOT_COUNT) if i not in tptp.ABSENT_SLOTS]
    for position, slot in enumerate(filled):
        struct.pack_into("<I", body, tptp.SLOT_TABLE + 4 * slot,
                         tptp.BASE + 0x64 + 8 * position)
    body[tptp.MARKER_OFFSET:tptp.MARKER_OFFSET + 4] = tptp.MARKER
    body[tptp.MARKER_OFFSET + 4] = 0x38
    return bytes(body)


def _image(extra: int = 0) -> bytes:
    return tptp.seal(_body(extra))


# -- what the remote checks ------------------------------------------------------------


def test_a_sealed_image_passes_the_start_check_and_the_layout():
    image = _image()
    assert tptp.start_check(image) == (None, [])
    report = tptp.validate(image)
    assert report["kind"] == "tptp-image" and report["size"] == len(image)
    assert len(report["sections"]) == 18


@pytest.mark.parametrize("damage, message", [
    (lambda b: b"XXXX" + b[4:], tptp.MESSAGE_WEBSITE),
    (lambda b: b[:0x5F] + b"XXXX" + b[0x63:], tptp.MESSAGE_WEBSITE),
    (lambda b: b[:-4] + b"XXXX", tptp.MESSAGE_WEBSITE),
    (lambda b: b[:100] + bytes([b[100] ^ 1]) + b[101:], tptp.MESSAGE_CORRUPT),
])
def test_each_failed_test_shows_the_message_the_remote_shows(damage, message):
    damaged = damage(_image())
    shown, reasons = tptp.start_check(damaged)
    assert shown == message and reasons
    with pytest.raises(ValueError):
        tptp.validate(damaged)


def test_text_written_over_the_trailer_is_refused_and_sealing_the_longer_body_repairs_it():
    """The failure the first generated 880 images had: the new text took the place of the
    checksum and DKDK, so the remote could not find the end of its configuration."""
    image = _image()
    text = bytes([0x05, 0x03, 0x0A, 0x31, 0x16, 0x1A])
    overwritten = image[:-6] + text
    shown, _ = tptp.start_check(overwritten)
    assert shown == tptp.MESSAGE_WEBSITE
    with pytest.raises(ValueError, match="Go to Website"):
        tptp.validate(overwritten)
    longer = tptp.seal(image[:-6] + text)
    assert tptp.validate(longer)["size"] == len(image) - 6 + len(text) + 6


def test_an_end_vector_that_is_moved_without_a_closing_marker_is_refused():
    """A longer image whose end vector was moved but whose DKDK was never restored."""
    image = bytearray(_image())
    image[-6:] = b"\x00" * 6 + b"\x15\x00\x84\xb0"
    struct.pack_into("<I", image, 4, tptp.BASE + len(image) - 4)
    assert tptp.start_check(bytes(image))[0] == tptp.MESSAGE_WEBSITE


def test_an_odd_last_byte_is_left_out_of_the_checksum():
    """Measured on an 785's image, whose unpaired byte is not zero."""
    odd = _image(extra=1)
    assert (len(odd) - 6) % 2 == 1
    tampered = bytearray(odd)
    tampered[len(odd) - 7] ^= 0xFF
    assert tptp.start_check(bytes(tampered)) == (None, [])
    tampered[len(odd) - 8] ^= 0xFF
    assert tptp.start_check(bytes(tampered))[0] == tptp.MESSAGE_CORRUPT


def test_the_start_check_is_not_asked_about_the_layout():
    """The firmware compares four things. An image with a filled slot 8 starts, and only
    the stricter check refuses it - the two are different questions."""
    body = bytearray(_body())
    struct.pack_into("<I", body, tptp.SLOT_TABLE + 4 * 8, tptp.BASE + 0x64)
    image = tptp.seal(bytes(body))
    assert tptp.start_check(image) == (None, [])
    with pytest.raises(ValueError, match="slot 8 is filled"):
        tptp.validate(image)


@pytest.mark.parametrize("edit, complaint", [
    (lambda b: struct.pack_into("<I", b, 8, 0x1600), "format word"),
    (lambda b: struct.pack_into("<I", b, tptp.SLOT_TABLE + 4 * 3, 0), "slot 3 is empty"),
    (lambda b: struct.pack_into("<I", b, tptp.SLOT_TABLE + 4 * 3,
                                tptp.BASE + 0x7FFFFF), "outside the image"),
    (lambda b: struct.pack_into("<I", b, tptp.SLOT_TABLE + 4 * 3,
                                struct.unpack_from("<I", b, tptp.SLOT_TABLE + 4 * 4)[0]),
     "not unique"),
])
def test_a_layout_no_known_image_has_is_refused(edit, complaint):
    body = bytearray(_body())
    edit(body)
    with pytest.raises(ValueError, match=complaint):
        tptp.validate(tptp.seal(bytes(body)))


def test_bytes_after_the_closing_marker_are_refused():
    with pytest.raises(ValueError, match="bytes follow"):
        tptp.validate(_image() + b"\x00\x00")


def test_an_image_may_not_reach_the_staged_install_area():
    image = tptp.seal(_body(tptp.MAX_SIZE))
    with pytest.raises(ValueError, match="staged-install"):
        tptp.validate(image)


def test_sections_run_to_the_next_higher_pointer_not_the_next_slot():
    body = bytearray(_body(64))
    # swap two slots' pointers so slot order and address order disagree, as in real images
    a, b = tptp.SLOT_TABLE + 4 * 0, tptp.SLOT_TABLE + 4 * 1
    first, second = body[a:a + 4], body[b:b + 4]
    body[a:a + 4], body[b:b + 4] = second, first
    ranges = tptp.section_ranges(tptp.seal(bytes(body)))
    assert ranges[1][0] < ranges[0][0] and ranges[1][1] == ranges[0][0]


# -- the flash images --------------------------------------------------------------------


def _application() -> bytes:
    image = bytearray(b"\xff" * firmware.SIZE)
    image[4:6] = b"HG"
    image[firmware.APPLICATION_ENTRY:firmware.APPLICATION_ENTRY + 4] = b"\x06\xef\x50\xf0"
    image[0:2] = bytes(firmware.application_checksum(bytes(image)))
    return bytes(image)


def _bootloader(programmed_to: int = 0x6390) -> bytes:
    image = bytearray(b"\xff" * firmware.SIZE)
    image[0:4] = b"\xbe\xef\x31\xf0"        # GOTO 0x637C
    image[0x08:0x0C] = b"\x00\xef\x82\xf0"  # GOTO 0x10400
    image[0x18:0x1C] = b"\x00\xef\x84\xf0"  # GOTO 0x10800
    image[0x100:programmed_to] = b"\x00" * (programmed_to - 0x100)
    return bytes(image)


def test_a_goto_is_read_as_the_pic18_encodes_it():
    assert firmware.goto_target(b"\xbe\xef\x31\xf0", 0) == 0x637C
    assert firmware.goto_target(b"\x00\xef\x82\xf0", 0) == 0x10400
    assert firmware.goto_target(b"\xff\xff\xff\xff", 0) is None


def test_an_application_is_checked_as_the_bootloader_checks_it():
    image = _application()
    assert firmware.check_application(image) == []
    broken = bytearray(image)
    broken[0x1000] ^= 1
    assert any("checksum" in problem for problem in firmware.check_application(bytes(broken)))
    assert any("magic" in problem for problem in
               firmware.check_application(image[:4] + b"XX" + image[6:]))
    assert firmware.check_application(image[:-1])


def test_the_application_checksum_starts_at_offset_four():
    """Both lanes from offset 4: changing the stored two bytes does not change the sum."""
    image = bytearray(_application())
    wanted = firmware.application_checksum(bytes(image))
    image[0:2] = b"\x00\x00"
    assert firmware.application_checksum(bytes(image)) == wanted


def test_a_bootloader_that_jumps_into_erased_flash_is_refused():
    """What a bootloader image with its tail missing does: reset runs erased flash, and
    nothing can write a bootloader back."""
    assert firmware.check_bootloader(_bootloader()) == []
    truncated = _bootloader(programmed_to=0x6354)
    problems = firmware.check_bootloader(truncated)
    assert any("never start" in problem for problem in problems)


def test_a_bootloader_whose_interrupt_vectors_are_blank_is_refused():
    image = bytearray(_bootloader())
    image[0x08:0x0C] = b"\xff" * 4
    problems = firmware.check_bootloader(bytes(image))
    assert any("0x08" in problem for problem in problems)


def test_the_product_string_is_read_in_utf16():
    image = bytearray(b"\xff" * 0x100)
    text = "Harmony Remote 0-4.4.2".encode("utf-16-le")
    image[0x20:0x20 + len(text)] = text
    assert firmware.product_string(bytes(image)) == "Harmony Remote 0-4.4.2"
    assert firmware.product_string(b"\xff" * 64) is None


# -- the profile and the backend -----------------------------------------------------------


def _project_from(path: Path, tmp_path) -> dict:
    """Import a real configuration as the application does and return its project."""
    from afterglow import importer
    tree = tmp_path / "tree"
    with contextlib.redirect_stdout(io.StringIO()):
        ezhex.unpack(str(path), str(tree))
    return importer.import_result(str(tree), name="test").project


def _build(project: dict, tmp_path) -> Path:
    from afterglow.build_service import ConfigBuildService
    project = {**project, "settings": {**project["settings"], "remote": "harmony-880",
                                       "out_file": str(tmp_path / "built.ezhex")}}
    with contextlib.redirect_stdout(io.StringIO()):
        return Path(ConfigBuildService(ROOT, lambda _m: None).build(project))


def _synthetic_tree(tmp_path) -> Path:
    tree = tmp_path / "synthetic"
    tree.mkdir()
    (tree / "payload.bin").write_bytes(_image(extra=256))
    return tree


def test_the_profile_identifies_an_880_and_not_an_885():
    profile = remotes.get("harmony-880")
    assert (profile.arch, profile.skin, profile.board, profile.flash) == \
        (8, 15, "1.8.0", "0x01:0x49")
    header = ezhex.header_for_profile(profile)
    assert remotes.identify(header).id == "harmony-880"
    other = header.replace(b"<SKIN>15</SKIN>", b"<SKIN>17</SKIN>").replace(b"1.8.0", b"2.1.0")
    with pytest.raises(remotes.UnknownRemote):
        remotes.identify(other)


def test_the_profile_is_not_verified_until_a_built_image_has_booted():
    assert not remotes.get("harmony-880").verified


def test_a_project_with_no_imported_image_cannot_be_built():
    profile = remotes.get("harmony-880")
    with pytest.raises(NotImplementedError, match="Import a Harmony 880"):
        backend.build_project({"devices": [], "settings": {}}, profile, None)


def test_an_import_keeps_the_image_in_the_project_and_a_build_gives_it_back(tmp_path):
    profile = remotes.get("harmony-880")
    result = backend.import_project(_synthetic_tree(tmp_path), profile, {})
    packed = result.project["settings"][backend.BASE_KEY]
    assert backend._base_image(result.project["settings"]) == _image(extra=256)
    assert packed["sha256"] == result.preservation["validation"]["sha256"]
    built = _build(result.project, tmp_path)
    assert ezhex.read(built.read_bytes()).payload == _image(extra=256)


def test_an_import_refuses_an_image_the_remote_would_refuse(tmp_path):
    tree = tmp_path / "damaged"
    tree.mkdir()
    (tree / "payload.bin").write_bytes(_image()[:-6] + b"\x05\x03\x0a\x31\x16\x1a")
    with pytest.raises(ValueError, match="Go to Website"):
        backend.import_project(tree, remotes.get("harmony-880"), {})


def test_what_cannot_be_read_is_said_and_counted_not_swallowed(tmp_path, monkeypatch):
    def unreadable(_blob):
        raise RuntimeError("no records here")
    monkeypatch.setattr(backend, "_ir_and_bindings", unreadable)
    result = backend.import_project(_synthetic_tree(tmp_path), remotes.get("harmony-880"), {})
    assert "RuntimeError" in result.rejected["infrared"]
    assert any("infrared records could not be read" in note for note in result.warnings)


def test_a_build_never_starts_from_a_file_in_the_dumps_folder(tmp_path, monkeypatch):
    """The first 880 backend took the newest backup as its base, so one damaged image read
    back from the remote became the input of every build after it."""
    home = tmp_path / "home"
    dumps = home / "Documents" / "Afterglow" / "Dumps"
    dumps.mkdir(parents=True)
    (dumps / "harmony-880-before-write-29990101-000000.ezhex").write_bytes(b"garbage")
    monkeypatch.setenv("HOME", str(home))
    result = backend.import_project(_synthetic_tree(tmp_path), remotes.get("harmony-880"), {})
    built = _build(result.project, tmp_path)
    assert ezhex.read(built.read_bytes()).payload == _image(extra=256)


def test_a_base_the_remote_would_refuse_is_not_patched(tmp_path):
    result = backend.import_project(_synthetic_tree(tmp_path), remotes.get("harmony-880"), {})
    project = result.project
    damaged = _image(extra=256)[:-6] + b"\x00" * 6
    project["settings"][backend.BASE_KEY] = backend._pack_base(damaged)
    with pytest.raises(ValueError, match="Go to Website"):
        _build(project, tmp_path)


def test_a_seal_that_would_cut_content_off_is_refused():
    with pytest.raises(ValueError, match="DKDK"):
        backend._seal(bytearray(_image()[:-4] + b"XXXX"))


def test_comparing_two_images_reports_the_sections_that_changed():
    before = _image(extra=64)
    changed = bytearray(_body(64))
    changed[0x64 + 8 * 18 + 3] = 0xAB
    report = backend.compare_payloads(before, tptp.seal(bytes(changed)), None)
    assert report["changed"] and report["added"] == [] == report["removed"]


def test_a_remote_that_writes_can_say_what_a_write_changes():
    assert callable(getattr(backend, "compare_payloads", None))
    assert callable(getattr(backend, "validate_payload", None))


# -- real images, when there are some ---------------------------------------------------------


def _payload(path: Path) -> bytes:
    return ezhex.read(path.read_bytes()).payload


@pytest.mark.skipif(not INTACT, reason="no real 880 configurations in configs/880/")
@pytest.mark.parametrize("path", INTACT, ids=lambda p: p.name)
def test_a_real_image_passes_and_identifies_as_an_880(path):
    contents = ezhex.read(path.read_bytes())
    assert ezhex.checksum(contents.payload) == contents.declared
    assert tptp.start_check(contents.payload) == (None, [])
    tptp.validate(contents.payload)
    assert remotes.identify(contents.header).id == "harmony-880"


@pytest.mark.skipif(not INTACT, reason="no real 880 configurations in configs/880/")
@pytest.mark.parametrize("path", INTACT, ids=lambda p: p.name)
def test_a_real_image_survives_being_resealed_unchanged(path):
    """Closing an image again with ``seal`` gives back the bytes it was read as: the
    trailer is one function of the body, and nothing else is in it."""
    payload = _payload(path)
    assert tptp.seal(payload[:-6]) == payload


@pytest.mark.skipif(not INTACT, reason="no real 880 configurations in configs/880/")
@pytest.mark.parametrize("path", INTACT, ids=lambda p: p.name)
def test_a_real_import_builds_back_the_same_image_when_nothing_changed(path, tmp_path):
    project = _project_from(path, tmp_path)
    assert project["devices"]
    assert ezhex.read(_build(project, tmp_path).read_bytes()).payload == _payload(path)


@pytest.mark.skipif(not INTACT, reason="no real 880 configurations in configs/880/")
def test_a_built_file_passes_the_checks_a_write_makes(tmp_path):
    from afterglow import concord, first_write
    profile = remotes.get("harmony-880")
    project = _project_from(INTACT[0], tmp_path)
    project["devices"][0]["label"] = "Butt"
    built = _build(project, tmp_path)
    header = ezhex.read(built.read_bytes()).header
    assert b">None<" not in header
    live = {"arch": profile.arch, "skin": profile.skin, "flash": profile.flash,
            "board": profile.board, "software_type": profile.software_type}
    assert concord.file_mismatches(built, live) == []
    first_write._artifact(built)


@pytest.mark.skipif(not INTACT, reason="no real 880 configurations in configs/880/")
@pytest.mark.parametrize("path", INTACT, ids=lambda p: p.name)
def test_a_renamed_device_builds_an_image_the_remote_would_start(path, tmp_path):
    """The edit that broke the first images: a device renamed, to a longer name."""
    project = _project_from(path, tmp_path)
    project["devices"][0]["label"] = "Butt Butt Butt"
    payload = ezhex.read(_build(project, tmp_path).read_bytes()).payload
    assert tptp.start_check(payload) == (None, [])
    tptp.validate(payload)


@pytest.mark.skipif(not DAMAGED, reason="no damaged 880 configurations in configs/880/damaged/")
@pytest.mark.parametrize("path", DAMAGED, ids=lambda p: p.name)
def test_a_damaged_real_image_is_refused_before_it_is_written(path):
    with pytest.raises(ValueError, match="Go to Website|Configuration Corrupted"):
        tptp.validate(_payload(path))
