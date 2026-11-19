"""The 880's text: its fonts, its screen programs, and the glyph codes each image numbers in
its own way.

Real configurations are never committed. Tests that read them take what is in
``configs/880/`` (an 880's own) and ``configs/880/other/`` (other models of the architecture,
and other owners') and skip when it is empty.

Format reference: docs/harmony_880/fonts.md
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from afterglow import ezhex
from afterglow.backends.harmony_880 import font, glyphs, labels, screens, tptp

ROOT = Path(__file__).resolve().parent.parent
PATTERN = "*.[eE][zZ][hH][eE][xX]"
OWN = sorted((ROOT / "configs" / "880").glob(PATTERN))
OTHERS = sorted((ROOT / "configs" / "880" / "other").glob(PATTERN))
REAL = OWN + OTHERS


def _payload(path: Path) -> bytes:
    return ezhex.read(path.read_bytes()).payload


def _named(name: str) -> bytes:
    path = next((p for p in REAL if p.name == name), None)
    if path is None:
        pytest.skip(f"{name} is not in configs/880/")
    return _payload(path)


# -- a glyph -------------------------------------------------------------------------------


def _glyph(*rows: str) -> font.Glyph:
    return font.Glyph(len(rows[0]), tuple(
        tuple(0xFFFF if cell == "#" else (0x0000 if cell == "o" else None) for cell in row)
        for row in rows))


def test_a_glyph_is_runs_of_skips_and_literals_with_a_skip_of_nothing_between_rows():
    glyph = _glyph("..##..", ".#..#.", "#o##o#")
    raw = font.encode_glyph(glyph)
    # width, then row one: skip 2, 2 literals, skip 2
    assert raw[:3] == bytes([6, 0x82, 2])
    assert raw[3:7] == bytes([0xFF] * 4) and raw[7:9] == bytes([0x82, 0x80])
    assert raw[-1] == 0
    assert font.decode_glyph(raw, 0) == font.Glyph(glyph.width, glyph.rows, len(raw))


def test_a_pixel_is_two_bytes_with_the_high_byte_first():
    raw = bytes([1, 1, 0x12, 0x34, 0])
    assert font.decode_glyph(raw, 0).rows == ((0x1234,),)


def test_runs_longer_than_a_byte_can_count_are_split():
    glyph = font.Glyph(200, ((None,) * 200, (0xFFFF,) * 200))
    raw = font.encode_glyph(glyph)
    assert font.decode_glyph(raw, 0).rows == glyph.rows
    assert max(byte for byte in raw[1:3]) <= 0xFF


@pytest.mark.parametrize("raw, complaint", [
    (bytes([2, 0x83, 0]), "wider"),
    (bytes([3, 0x82, 0]), "middle of a row"),
    (bytes([2, 0x82]), "off the image"),
    (bytes([2, 2, 0xFF]), "off the image"),
    (bytes([0, 0]), "no width"),
])
def test_a_glyph_that_does_not_read_says_why(raw, complaint):
    with pytest.raises(font.FontError, match=complaint):
        font.decode_glyph(raw, 0)


def test_the_shape_digest_leaves_the_colours_out():
    one, other = _glyph("#.", ".#"), _glyph("o.", ".o")
    assert one.fingerprint() == other.fingerprint()
    assert one.fingerprint() != _glyph(".#", "#.").fingerprint()


# -- the glyph table ------------------------------------------------------------------------


def test_the_shape_table_holds_shapes_and_characters_and_nothing_else():
    table = json.loads(glyphs.SHAPES_FILE.read_text(encoding="utf-8"))["shapes"]
    assert table
    for key, chars in table.items():
        height, digest = key.split(":")
        assert height.isdigit() and len(digest) == 16 and chars


def test_a_code_with_no_known_shape_has_no_character_and_is_reported():
    code_map = glyphs.CodeMap(chars={1: "a"}, unknown={2})
    assert code_map.decode([1, 2]) == "a" + glyphs.UNKNOWN
    assert code_map.encode("ab") is None and code_map.encode("a") == (1,)
    assert code_map.coverage([1, 2]) == 0.5


# -- real images -----------------------------------------------------------------------------


@pytest.mark.skipif(not REAL, reason="no real 880 configurations in configs/880/")
@pytest.mark.parametrize("path", REAL, ids=lambda p: p.name)
def test_every_glyph_of_a_real_image_is_read_back_by_its_own_encoder(path):
    payload = _payload(path)
    for fs in font.font_sets(payload):
        for code in fs.codes():
            offset = fs.offset(code)
            glyph = font.decode_glyph(payload, offset)
            assert font.encode_glyph(glyph) == payload[offset:offset + glyph.size], (
                fs.index, code)


@pytest.mark.skipif(not REAL, reason="no real 880 configurations in configs/880/")
@pytest.mark.parametrize("path", REAL, ids=lambda p: p.name)
def test_every_screen_program_of_a_real_image_reads(path):
    payload = _payload(path)
    runs, report = screens.text_runs(payload)
    assert report["unreadable"] == 0 and report["unterminated"] == 0
    assert report["programs"] and runs
    assert all(run.font is not None for run in runs)
    assert all(payload[run.instruction] in (screens.TEXT_AT, screens.TEXT_INLINE)
               for run in runs)


@pytest.mark.skipif(not REAL, reason="no real 880 configurations in configs/880/")
@pytest.mark.parametrize("path", REAL, ids=lambda p: p.name)
def test_nearly_all_the_text_a_real_image_draws_is_read(path):
    reading = labels.read(_payload(path))
    drawn = [code for run in reading.runs for code in run.codes]
    read = sum(code in reading.maps[run.font].chars for run in reading.runs for code in run.codes)
    assert read / len(drawn) > 0.85, f"{path.name}: {read} of {len(drawn)}"


def test_two_images_number_the_same_letter_differently_and_both_are_read():
    """The Bedroom sample's capital P is code 16, the Living Room's is 27: what the two
    share is the shape."""
    bedroom, living = _named("H880-Bedroom.EZHex"), _named("H885-LivingRoom.EZHex")
    assert glyphs.code_map(bedroom).chars[16] == "P"
    assert glyphs.code_map(living).chars[27] == "P"
    assert glyphs.code_map(bedroom).chars[27] != "P"
    assert any(labels.find(labels.read(bedroom), "Pana"))
    assert any(labels.find(labels.read(living), "TX-55GZ950B"))


def test_the_digits_of_the_bedroom_sample_are_read_by_their_pixels():
    """Its published reading has 5 and 6 the wrong way round: the two glyphs it numbers 9
    and 8 draw a 6 and a 5, and the Living Room's digits, which agree with its pixels, say so.
    The devices drawn there are read by what they draw, and are 32AS600 and 32AS500."""
    reading = labels.read(_named("H880-Bedroom.EZHex"))
    assert labels.find(reading, "32AS500") and labels.find(reading, "32AS600")


def test_the_users_own_screens_read_as_the_remote_shows_them():
    reading = labels.read(_named("Update.EZHex"))
    for text in ("Watch TV", "Watch a DVD", "Listen to CDs", "Play Xbox 360",
                 "Digital Set Top Box", "Devices", "Everything is OK !"):
        assert labels.find(reading, text), text


# -- changing text where it is drawn ------------------------------------------------------------


def test_a_shorter_name_is_padded_and_the_image_keeps_its_length_and_checksum():
    payload = _named("Update.EZHex")
    changed, notes = labels.rename(payload, {"Digital Set Top Box": "Cable Box"})
    assert len(changed) == len(payload) and notes
    tptp.validate(changed)
    reading = labels.read(changed)
    assert any(reading.text(run).rstrip() == "Cable Box" for run in reading.runs)
    assert not labels.find(reading, "Digital Set Top Box")


def _drawn(payload: bytes, text: str):
    reading = labels.read(payload)
    return reading, [run for run in reading.runs if reading.text(run).strip() == text]


def test_a_longer_name_is_drawn_in_full_on_every_page_that_drew_the_old_one():
    """'TV' has room for two characters. Its inline instruction becomes a reference to the new
    text, and so does each of the pages that referred to it: all seven draw 'Television'."""
    payload = _named("Update.EZHex")
    _reading, before = _drawn(payload, "TV")
    changed, notes = labels.rename(payload, {"TV": "Television"})
    tptp.validate(changed)
    assert len(changed) > len(payload)
    reading, after = _drawn(changed, "Television")
    assert len(after) == len(before) == 7
    assert not _drawn(changed, "TV")[1]
    assert any("grown into a reference" in note for note in notes)
    _runs, report = screens.text_runs(changed)
    assert report["unreadable"] == 0 and report["unterminated"] == 0
    assert all(reading.alignment(run) in ("left", "right") for run in after)


def test_growing_a_name_changes_no_other_text():
    payload = _named("Update.EZHex")
    changed, _notes = labels.rename(payload, {"TV": "Television"})
    before, after = labels.read(payload), labels.read(changed)
    texts = lambda reading: sorted(reading.text(run) for run in reading.runs)  # noqa: E731
    assert [t for t in texts(after) if t != "Television"] == [t for t in texts(before) if t != "TV"]


@pytest.mark.parametrize("old, new", [("Fav", "Favorite"), ("Clear", "Clear all")])
def test_a_string_that_cannot_be_grown_is_not_changed_at_all(old, new):
    """Three characters of room (or one, or five) leave no way to fill what is left of the
    instruction. Nothing is half done: the references to it are not moved either."""
    payload = _named("Update.EZHex")
    changed, notes = labels.rename(payload, {old: new})
    assert changed == payload and any("cannot be grown" in note for note in notes)


def test_a_text_too_wide_for_the_right_edge_is_not_done_in_part():
    payload = _named("Update.EZHex")
    changed, notes = labels.rename(payload, {"Top Box": "Top Box With A Name Far Too Long"})
    assert changed == payload and any("does not fit" in note for note in notes)


def test_a_name_the_image_does_not_draw_is_left_alone_and_said():
    payload = _named("Update.EZHex")
    changed, notes = labels.rename(payload, {"No Such Device": "Cable Box"})
    assert changed == payload
    assert any("not drawn as a whole string" in note for note in notes)


def test_a_character_the_font_does_not_draw_is_left_alone_and_said():
    payload = _named("Update.EZHex")
    changed, notes = labels.rename(payload, {"TV": "\u00e9\u00e9"})
    assert changed == payload and any("draws no" in note for note in notes)


def test_nothing_to_rename_is_the_same_image():
    payload = _named("Update.EZHex")
    assert labels.rename(payload, {}) == (payload, [])


# -- the Devices page, as photographed ------------------------------------------------------------


def _devices_pages(payload: bytes):
    """{program: [(y, x, text)]} for every page that draws the title ``Devices``."""
    reading = labels.read(payload)
    programs = {run.program for run in reading.runs if reading.text(run) == "Devices"}
    return reading, {program: sorted((run.y, run.x, reading.text(run)) for run in reading.runs
                                     if run.program == program and reading.text(run) != "Devices")
                     for program in programs}


@pytest.mark.skipif(not REAL, reason="no real 880 configurations in configs/880/")
@pytest.mark.parametrize("path", REAL, ids=lambda p: p.name)
def test_every_label_of_a_devices_page_is_at_the_left_edge_or_ends_at_the_right_edge(path):
    """876 labels on 110 copies of the page in sixteen images, none an exception: a left-hand
    cell starts at x = 3 and a right-hand cell is right-aligned to x = 125. (The page counter
    at the foot, ``1 OF 2``, is centred and is not a cell.)"""
    reading = labels.read(_payload(path))
    programs = {run.program for run in reading.runs if reading.text(run) == "Devices"}
    for run in reading.runs:
        if run.program in programs and reading.text(run) != "Devices" and run.y < 150:
            assert reading.alignment(run) in ("left", "right"), (run.x, run.y, reading.text(run))


def test_the_remote_shows_the_users_update_config_as_the_page_decodes():
    """A photograph of the 880 running Update.EZHex: TV at the left of the first row, the
    set-top box at the right as two lines, the two names in brackets on the second row, and
    no 'Mini System' or 'Game Console' - the page never draws those words."""
    _reading, pages = _devices_pages(_named("Update.EZHex"))
    first = pages[min(pages)]
    assert [(x, text) for y, x, text in first if y == 42] == [(3, "TV"), (80, "Top Box")]
    assert [text for y, x, text in first if y == 29] == ["Digital Set"]
    assert [text for y, x, text in first if y == 61] == ["(DVD,", "(With"]
    assert [text for y, x, text in first if y == 74] == ["CD, Radio)", "DVD)"]


def test_the_remote_shows_the_spare_4_config_as_one_of_its_six_device_pages():
    """A photograph of the 880 running H880-Spare-4.EZHex: TV left and PVR right on the first
    row, 'Mini System' left and 'Game Console' right on the second. That is the second of six
    copies of the page, each with the same four devices in another order: the first copy in
    the image is not the one the remote showed."""
    _reading, pages = _devices_pages(_named("H880-Spare-4.EZHex"))
    assert len(pages) == 6
    shown = [(3, "TV"), (101, "PVR")]
    matching = [rows for rows in pages.values()
                if [(x, text) for y, x, text in rows if y == 42] == shown]
    assert matching
    rows = matching[0]
    assert [(x, text) for y, x, text in rows if y == 61] == [(3, "Mini"), (93, "Game")]
    assert [(x, text) for y, x, text in rows if y == 74] == [(3, "System"), (79, "Console")]
    assert any(rows != other for other in pages.values())


def test_a_right_hand_label_still_ends_at_the_right_edge_after_it_is_renamed():
    payload = _named("Update.EZHex")
    changed, notes = labels.rename(payload, {"Top Box": "Box"})
    tptp.validate(changed)
    reading = labels.read(changed)
    shown = [run for run in reading.runs if reading.text(run).strip() == "Box"]
    assert shown and all(reading.alignment(run) in ("left", "right") for run in shown)
    assert any(reading.alignment(run) == "right" for run in shown)
    assert any("moved to stay at the right edge" in note for note in notes)


def test_every_copy_of_the_page_keeps_each_label_at_its_own_edge():
    payload = _named("H880-Spare-4.EZHex")
    changed, _notes = labels.rename(payload, {"PVR": "DVR"})
    reading = labels.read(changed)
    shown = [run for run in reading.runs if reading.text(run).strip() == "DVR"]
    assert len(shown) >= 6
    assert all(reading.alignment(run) in ("left", "right") for run in shown)
    assert any(reading.alignment(run) == "right" for run in shown)


def test_a_left_hand_label_stays_where_it_starts():
    payload = _named("Update.EZHex")
    changed, _notes = labels.rename(payload, {"TV": "Hi"})
    reading = labels.read(changed)
    shown = [run for run in reading.runs if reading.text(run).strip() == "Hi"]
    assert shown
    assert all(run.x == 3 for run in shown if reading.alignment(run) == "left")
    assert all(reading.alignment(run) in ("left", "right") for run in shown)


def test_the_build_no_longer_redraws_the_devices_page(tmp_path):
    """The old build replaced the first copy of the page with a program of text alone, which
    lost its pictures and left the other copies as they were."""
    assert not hasattr(__import__("afterglow.backends.harmony_880.backend",
                                  fromlist=["backend"]), "_rewrite_devices_page")
