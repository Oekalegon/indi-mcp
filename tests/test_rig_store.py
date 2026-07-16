from pathlib import Path

import pytest

from indi_mcp import rig_store

VALID_RIG_YAML = """
id: newtonian-8in
name: 8" Newtonian imaging rig
components:
  - role: mount
    device: "Telescope Simulator"
  - role: telescope
    apertureMm: 203
    focalLengthMm: 1000
  - role: focuser
    device: "Focuser Simulator"
    minPosition: 0
    maxPosition: 50000
  - role: filterWheel
    device: "Filter Wheel Simulator"
    slots:
      1: Luminance
      2: Red
      3: Green
      4: Blue
  - role: rotator
    device: "Rotator Simulator"
  - role: camera
    make: ZWO
    model: ASI2600MM Pro
    id: "SN12345"
    device: "ZWO CCD ASI2600MM Pro"
    cooled: true
    pixelsX: 6248
    pixelsY: 4176
    pixelSizeMicron: 3.76
    bitDepth: 16
  - role: guideTelescope
    apertureMm: 60
    focalLengthMm: 240
  - role: guideCamera
    device: "ZWO CCD ASI120MM Mini"
    cooled: false
    pixelsX: 1280
    pixelsY: 960
    pixelSizeMicron: 3.75
    bitDepth: 12
  - role: powerHub
    device: "Pegasus PPBA"
  - role: observatoryControl
    device: "Dome Simulator"
  - role: flatScreen
    device: "Flat Panel Simulator"
  - role: dewHeater
    device: "Pegasus PPBA:Dew A"
  - role: dewHeater
    device: "Pegasus PPBA:Dew B"
"""

MINIMAL_RIG_YAML = """
id: minimal
name: Minimal rig
components:
  - role: mount
    device: "Telescope Simulator"
  - role: camera
    device: "CCD Simulator"
    pixelsX: 1000
    pixelsY: 1000
    pixelSizeMicron: 5.0
    bitDepth: 16
"""


@pytest.fixture(autouse=True)
def _reset_loaded_rigs() -> None:
    rig_store._rigs = {}


def test_load_rigs_returns_empty_list_when_directory_missing(tmp_path: Path) -> None:
    rigs = rig_store.load_rigs(tmp_path / "does-not-exist")

    assert rigs == []
    assert rig_store.list_rigs() == []


def test_load_rigs_parses_valid_rig_file(tmp_path: Path) -> None:
    (tmp_path / "newtonian-8in.yaml").write_text(VALID_RIG_YAML)

    rigs = rig_store.load_rigs(tmp_path)

    assert len(rigs) == 1
    rig = rigs[0]
    assert rig.id == "newtonian-8in"

    by_role = {c.role: c for c in rig.components if c.role != "dewHeater"}
    assert by_role["mount"].device == "Telescope Simulator"
    assert by_role["telescope"].apertureMm == 203
    assert by_role["telescope"].device is None
    assert by_role["focuser"].maxPosition == 50000
    assert by_role["filterWheel"].slots == {1: "Luminance", 2: "Red", 3: "Green", 4: "Blue"}
    assert by_role["rotator"].device == "Rotator Simulator"
    assert by_role["camera"].device == "ZWO CCD ASI2600MM Pro"
    assert by_role["camera"].bitDepth == 16
    assert by_role["camera"].make == "ZWO"
    assert by_role["camera"].model == "ASI2600MM Pro"
    assert by_role["camera"].id == "SN12345"
    assert by_role["guideTelescope"].focalLengthMm == 240
    assert by_role["guideCamera"].bitDepth == 12
    assert by_role["powerHub"].device == "Pegasus PPBA"
    assert by_role["observatoryControl"].device == "Dome Simulator"
    assert by_role["flatScreen"].device == "Flat Panel Simulator"

    dew_heaters = [c.device for c in rig.components if c.role == "dewHeater"]
    assert dew_heaters == ["Pegasus PPBA:Dew A", "Pegasus PPBA:Dew B"]


def test_load_rigs_allows_a_minimal_component_list(tmp_path: Path) -> None:
    (tmp_path / "minimal.yaml").write_text(MINIMAL_RIG_YAML)

    rigs = rig_store.load_rigs(tmp_path)

    assert len(rigs) == 1
    assert [c.role for c in rigs[0].components] == ["mount", "camera"]


def test_load_rigs_distinguishes_two_identical_cameras_by_id(tmp_path: Path) -> None:
    (tmp_path / "two-cameras.yaml").write_text(
        MINIMAL_RIG_YAML + "  - role: guideCamera\n"
        "    make: ZWO\n"
        "    model: ASI120MM Mini\n"
        '    id: "SN-A"\n'
        '    device: "ZWO CCD ASI120MM Mini #1"\n'
        "  - role: guideCamera\n"
        "    make: ZWO\n"
        "    model: ASI120MM Mini\n"
        '    id: "SN-B"\n'
        '    device: "ZWO CCD ASI120MM Mini #2"\n'
    )

    rigs = rig_store.load_rigs(tmp_path)

    guide_cameras = [c for c in rigs[0].components if c.role == "guideCamera"]
    assert [c.id for c in guide_cameras] == ["SN-A", "SN-B"]
    assert {c.model for c in guide_cameras} == {"ASI120MM Mini"}


def test_load_rigs_allows_an_empty_component_list(tmp_path: Path) -> None:
    (tmp_path / "empty.yaml").write_text('id: empty\nname: "Empty rig"\ncomponents: []\n')

    rigs = rig_store.load_rigs(tmp_path)

    assert len(rigs) == 1
    assert rigs[0].components == []


def test_load_rigs_accepts_unanticipated_component_roles(tmp_path: Path) -> None:
    (tmp_path / "minimal.yaml").write_text(
        MINIMAL_RIG_YAML + '  - role: allSkyCamera\n    device: "All Sky Simulator"\n'
    )

    rigs = rig_store.load_rigs(tmp_path)

    assert len(rigs) == 1
    roles = [c.role for c in rigs[0].components]
    assert "allSkyCamera" in roles


def test_known_roles_covers_the_documented_important_roles() -> None:
    assert set(rig_store.KNOWN_ROLES) >= {
        "mount",
        "camera",
        "guideCamera",
        "focuser",
        "filterWheel",
        "rotator",
    }


def test_load_rigs_skips_files_with_invalid_yaml(tmp_path: Path) -> None:
    (tmp_path / "broken.yaml").write_text("id: [unterminated")
    (tmp_path / "minimal.yaml").write_text(MINIMAL_RIG_YAML)

    rigs = rig_store.load_rigs(tmp_path)

    assert [rig.id for rig in rigs] == ["minimal"]


def test_load_rigs_skips_files_that_fail_schema_validation(tmp_path: Path) -> None:
    (tmp_path / "missing-fields.yaml").write_text("id: incomplete\nname: Incomplete rig\n")
    (tmp_path / "minimal.yaml").write_text(MINIMAL_RIG_YAML)

    rigs = rig_store.load_rigs(tmp_path)

    assert [rig.id for rig in rigs] == ["minimal"]


def test_load_rigs_skips_files_with_a_component_missing_its_role(tmp_path: Path) -> None:
    (tmp_path / "no-role.yaml").write_text(
        'id: no-role\nname: "No role"\ncomponents:\n  - device: "Telescope Simulator"\n'
    )
    (tmp_path / "minimal.yaml").write_text(MINIMAL_RIG_YAML)

    rigs = rig_store.load_rigs(tmp_path)

    assert [rig.id for rig in rigs] == ["minimal"]


def test_load_rigs_rejects_unknown_fields(tmp_path: Path) -> None:
    (tmp_path / "extra-field.yaml").write_text(MINIMAL_RIG_YAML + "\nunknownField: true\n")

    rigs = rig_store.load_rigs(tmp_path)

    assert rigs == []


def test_load_rigs_keeps_first_definition_on_duplicate_id(tmp_path: Path) -> None:
    (tmp_path / "a-first.yaml").write_text(MINIMAL_RIG_YAML)
    (tmp_path / "b-second.yaml").write_text(MINIMAL_RIG_YAML.replace("Minimal rig", "Duplicate"))

    rigs = rig_store.load_rigs(tmp_path)

    assert len(rigs) == 1
    assert rigs[0].name == "Minimal rig"


def test_list_rigs_reports_id_and_name_only(tmp_path: Path) -> None:
    (tmp_path / "minimal.yaml").write_text(MINIMAL_RIG_YAML)
    rig_store.load_rigs(tmp_path)

    assert rig_store.list_rigs() == [{"id": "minimal", "name": "Minimal rig"}]


def test_get_rig_returns_loaded_rig(tmp_path: Path) -> None:
    (tmp_path / "minimal.yaml").write_text(MINIMAL_RIG_YAML)
    rig_store.load_rigs(tmp_path)

    rig = rig_store.get_rig("minimal")

    assert rig.id == "minimal"


def test_get_rig_rejects_unknown_id(tmp_path: Path) -> None:
    rig_store.load_rigs(tmp_path)

    with pytest.raises(ValueError, match="Unknown rig"):
        rig_store.get_rig("does-not-exist")
