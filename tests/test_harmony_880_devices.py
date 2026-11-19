"""The 880's Devices menu: reading its copies, and taking a device off them.

Real configurations are never committed; these read whatever is in ``configs/880/`` and skip
without it.

Format reference: docs/harmony_880/devices.md
"""

from __future__ import annotations

from pathlib import Path

import pytest

from afterglow import ezhex, remotes
from afterglow.backends.harmony_880 import backend, devices, ir, labels, screens, tptp

ROOT = Path(__file__).resolve().parent.parent
PATTERN = "*.[eE][zZ][hH][eE][xX]"
REAL = sorted((ROOT / "configs" / "880").glob(PATTERN)) + \
    sorted((ROOT / "configs" / "880" / "other").glob(PATTERN))
IMAGES = ["Update.EZHex", "H880-Spare-4.EZHex"]


def _named(name: str) -> bytes:
    path = next((p for p in REAL if p.name == name), None)
    if path is None:
        pytest.skip(f"{name} is not in configs/880/")
    return ezhex.read(path.read_bytes()).payload


def _names(payload: bytes, page) -> list[str]:
    reading = labels.read(payload)
    out = []
    for cell in page.cells:
        runs = [next(r for r in reading.runs if r.instruction == step["offset"])
                for step in cell.items if step["opcode"] in (4, 5)]
        out.append(" ".join(reading.text(run).strip() for run in runs))
    return out


@pytest.mark.parametrize("name", IMAGES)
def test_every_copy_of_the_menu_reads_with_each_device_at_a_place_and_a_soft_key(name):
    payload = _named(name)
    found, skipped = devices._pages(payload)
    assert skipped == [] and len(found) == 6
    for page in found:
        assert [cell.slot for cell in page.cells] == [0, 1, 2, 3]
        assert sorted(cell.group for cell in page.cells) == [0, 1, 2, 3]
        assert [cell.key for cell in page.cells] == [0x85, 0xAD, 0x86, 0xAE]
        assert sorted(tag for tag, *_rest in page.bindings) == sorted(
            cell.key for cell in page.cells)


@pytest.mark.parametrize("name", IMAGES)
def test_a_device_is_the_same_group_through_its_key_on_every_copy(name):
    """What says a soft key is by place: each device's picture reaches one group on all six."""
    found, _skipped = devices._pages(_named(name))
    seen: dict[bytes, set] = {}
    for page in found:
        for cell in page.cells:
            seen.setdefault(bytes(cell.picture["raw"][3:6]), set()).add(cell.group)
    assert len(seen) == 4 and all(len(groups) == 1 for groups in seen.values())


def _removed(name: str, groups: set[int]):
    payload = _named(name)
    return payload, *devices.remove(payload, groups)


@pytest.mark.parametrize("name", IMAGES)
@pytest.mark.parametrize("group", [0, 1, 2, 3])
def test_a_device_taken_off_leaves_the_others_in_order_at_the_places_from_the_top(name, group):
    payload, changed, notes = _removed(name, {group})
    tptp.validate(changed)
    before = devices.listed(payload)
    after = devices.listed(changed)
    assert after == [[g for g in groups if g != group] for groups in before]
    found, skipped = devices._pages(changed)
    assert skipped == [] and len(found) == 6 and len(notes) == 6
    reading = labels.read(changed)
    for page in found:
        assert [cell.slot for cell in page.cells] == [0, 1, 2]
        for cell in page.cells:
            column, row = cell.slot % 2, cell.slot // 2
            assert (cell.picture["raw"][1], cell.picture["raw"][2]) == (
                devices.CELL_X[column], devices.FIRST_Y + devices.ROW_STEP * row)
            assert (cell.frame["raw"][1], cell.frame["raw"][2]) == (
                devices.CELL_X[column], devices.FIRST_Y + devices.ROW_STEP * row)
            assert cell.key == devices.KEYS[(column, row)]
            for step in cell.items:
                if step["opcode"] in (4, 5):
                    run = next(r for r in reading.runs if r.instruction == step["offset"])
                    if column == 0:
                        assert run.x == labels.LEFT_EDGE
                    else:
                        assert run.x + reading.pixels(run.font, run.codes) == labels.RIGHT_EDGE
        assert sorted(tag for tag, *_rest in page.bindings) == sorted(
            cell.key for cell in page.cells)
    _runs, report = screens.text_runs(changed)
    assert report["unreadable"] == 0 and report["unterminated"] == 0


def test_a_device_keeps_its_name_its_picture_and_what_its_key_enters_when_it_moves_up():
    payload, changed, _notes = _removed("Update.EZHex", {0})
    old, _ = devices._pages(payload)
    new, _ = devices._pages(changed)
    tables_old, tables_new = devices.Tables.read(payload), devices.Tables.read(changed)

    def by_group(pages, tables, reading, data):
        out = {}
        for page in pages:
            keyed = {tag: op for tag, op, _opc, _f in page.bindings}
            for cell, name in zip(page.cells, _names(data, page)):
                out.setdefault(cell.group, set()).add((
                    bytes(cell.picture["raw"][3:6]), name,
                    tables.entered_mode(keyed[cell.key])))
        return out

    before = by_group(old, tables_old, None, payload)
    after = by_group(new, tables_new, None, changed)
    for group in (1, 2, 3):
        assert after[group] == before[group] and len(after[group]) == 1


@pytest.mark.parametrize("name", IMAGES)
def test_only_the_menus_page_records_change_and_the_rest_is_added_after_the_old_end(name):
    payload, changed, _notes = _removed(name, {1})
    found, _ = devices._pages(payload)
    records = {page.record + offset for page in found for offset in range(6)}
    body = len(payload) - 6
    differing = [i for i in range(body) if payload[i] != changed[i]]
    assert set(differing) <= records | set(range(4, 8))
    assert len(changed) > len(payload)
    before, after = ir.read_back(payload), ir.read_back(changed)
    assert before.keys() == after.keys()
    assert all(ir.same_meaning(signal, after[key]) for key, signal in before.items())


def test_a_device_that_is_not_there_changes_nothing():
    payload = _named("Update.EZHex")
    assert devices.remove(payload, set()) == (payload, [])
    assert devices.remove(payload, {9}) == (payload, [])


def test_an_image_whose_menu_is_not_understood_is_left_as_it_is_and_said():
    path = next((p for p in REAL if p.name == "H785-Config.EZHex"), None)
    if path is None:
        pytest.skip("H785-Config.EZHex is not in configs/880/")
    payload = ezhex.read(path.read_bytes()).payload
    changed, notes = devices.remove(payload, {0})
    assert changed == payload
    assert notes and all(note.startswith(labels.WARNING) for note in notes)


def test_taking_two_devices_off_one_after_the_other_equals_taking_both_off_at_once():
    payload = _named("Update.EZHex")
    both, _ = devices.remove(payload, {1, 2})
    assert devices.listed(both) == [[g for g in groups if g not in (1, 2)]
                                    for groups in devices.listed(payload)]


# -- through the backend --------------------------------------------------------------------


class _Context:
    def __init__(self, root: Path):
        self.root, self.lines = root, []

    def log(self, message):
        self.lines.append(message)


def _project():
    payload = _named("Update.EZHex")
    return payload, backend.import_project(payload, remotes.get("harmony-880"), {}).project


def _build(project: dict, tmp_path: Path):
    project = {**project, "settings": {**project["settings"],
                                       "out_file": str(tmp_path / "built.ezhex")}}
    context = _Context(tmp_path)
    result = backend.build_project(project, remotes.get("harmony-880"), context)
    return result, ezhex.read(Path(result.artifact).read_bytes()).payload, context


def test_an_import_says_which_group_each_device_was_read_from():
    _payload, project = _project()
    assert [d["_group"] for d in project["devices"]] == [0, 1, 2, 3]
    assert project["settings"][backend.GROUPS_KEY] == [0, 1, 2, 3]


def test_a_device_deleted_from_the_project_is_off_the_menu_after_a_build(tmp_path):
    payload, project = _project()
    project["devices"] = [d for d in project["devices"] if d["_group"] != 1]
    result, built, context = _build(project, tmp_path)
    tptp.validate(built)
    assert devices.listed(built) == [[g for g in groups if g != 1]
                                     for groups in devices.listed(payload)]
    assert result.changes["warnings"] == []
    assert any("off the Devices menu" in line for line in context.lines)
    assert sum(line.startswith("menu: ") for line in context.lines) == 6


def test_a_rename_and_a_removal_in_one_build_both_happen(tmp_path):
    payload, project = _project()
    project["devices"] = [d for d in project["devices"] if d["_group"] != 1]
    project["devices"][2]["label"] = "Stereo"
    _result, built, _context = _build(project, tmp_path)
    found, _skipped = devices._pages(built)
    texts = {name.strip() for page in found for name in _names(built, page)}
    assert "Stereo" in texts and not any("Digital" in name for name in texts)


def test_a_project_that_removed_nothing_builds_the_image_it_read(tmp_path):
    payload, project = _project()
    _result, built, _context = _build(project, tmp_path)
    assert built == payload


def test_a_device_added_in_the_project_is_said_not_to_be_written(tmp_path):
    payload, project = _project()
    project["devices"].append({"schema": "afterglow-project-device/1", "id": "x",
                               "label": "Brand New", "signals": {}, "commands": []})
    result, built, _context = _build(project, tmp_path)
    assert built == payload
    assert any("'Brand New'" in w and "is new" in w for w in result.changes["warnings"])
