from pathlib import Path

import pytest

from indi_mcp import rig_store

VALID_RIG_YAML = """
id: newtonian-8in
name: 8" Newtonian imaging rig
components:
  - role: mount
    id: mount-1
    device: "Telescope Simulator"
  - role: telescope
    id: main-scope
    apertureMm: 203
    focalLengthMm: 1000
  - role: focuser
    id: focuser-1
    device: "Focuser Simulator"
    minPosition: 0
    maxPosition: 50000
  - role: filterWheel
    id: filter-wheel-1
    device: "Filter Wheel Simulator"
    slots:
      1: Luminance
      2: Red
      3: Green
      4: Blue
  - role: rotator
    id: rotator-1
    device: "Rotator Simulator"
  - role: camera
    id: "SN12345"
    make: ZWO
    model: ASI2600MM Pro
    device: "ZWO CCD ASI2600MM Pro"
    cooled: true
    pixelsX: 6248
    pixelsY: 4176
    pixelSizeMicron: 3.76
    bitDepth: 16
  - role: guideTelescope
    id: guide-scope
    apertureMm: 60
    focalLengthMm: 240
  - role: guideCamera
    id: "SN67890"
    device: "ZWO CCD ASI120MM Mini"
    cooled: false
    pixelsX: 1280
    pixelsY: 960
    pixelSizeMicron: 3.75
    bitDepth: 12
  - role: powerHub
    id: power-hub-1
    device: "Pegasus PPBA"
  - role: observatoryControl
    id: dome-1
    device: "Dome Simulator"
  - role: flatScreen
    id: flat-screen-1
    device: "Flat Panel Simulator"
  - role: dewHeater
    id: dew-heater-a
    device: "Pegasus PPBA:Dew A"
  - role: dewHeater
    id: dew-heater-b
    device: "Pegasus PPBA:Dew B"
"""

MINIMAL_RIG_YAML = """
id: minimal
name: Minimal rig
components:
  - role: mount
    id: mount-1
    device: "Telescope Simulator"
  - role: camera
    id: camera-1
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
        MINIMAL_RIG_YAML
        + '  - role: allSkyCamera\n    id: allsky-1\n    device: "All Sky Simulator"\n'
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
        'id: no-role\nname: "No role"\ncomponents:\n  - id: c1\n    device: "Telescope Simulator"\n'
    )
    (tmp_path / "minimal.yaml").write_text(MINIMAL_RIG_YAML)

    rigs = rig_store.load_rigs(tmp_path)

    assert [rig.id for rig in rigs] == ["minimal"]


def test_load_rigs_skips_files_with_a_component_missing_its_id(tmp_path: Path) -> None:
    (tmp_path / "no-id.yaml").write_text(
        'id: no-id\nname: "No id"\ncomponents:\n'
        '  - role: mount\n    device: "Telescope Simulator"\n'
    )
    (tmp_path / "minimal.yaml").write_text(MINIMAL_RIG_YAML)

    rigs = rig_store.load_rigs(tmp_path)

    assert [rig.id for rig in rigs] == ["minimal"]


def test_load_rigs_skips_files_with_duplicate_component_ids(tmp_path: Path) -> None:
    (tmp_path / "duplicate-ids.yaml").write_text(
        MINIMAL_RIG_YAML + '  - role: dewHeater\n    id: camera-1\n    device: "Dew Heater"\n'
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


def test_suggest_rig_scores_full_match_highest(tmp_path: Path) -> None:
    (tmp_path / "minimal.yaml").write_text(MINIMAL_RIG_YAML)
    rig_store.load_rigs(tmp_path)

    suggestions = rig_store.suggest_rig(["Telescope Simulator", "CCD Simulator"])

    assert suggestions == [
        {
            "kind": "rigSuggestion",
            "rigId": "minimal",
            "rigName": "Minimal rig",
            "score": 1.0,
            "matched": ["mount-1", "camera-1"],
            "missing": [],
        }
    ]


def test_suggest_rig_reports_missing_devices(tmp_path: Path) -> None:
    (tmp_path / "minimal.yaml").write_text(MINIMAL_RIG_YAML)
    rig_store.load_rigs(tmp_path)

    suggestions = rig_store.suggest_rig(["Telescope Simulator"])

    assert suggestions[0]["score"] == 0.5
    assert suggestions[0]["matched"] == ["mount-1"]
    assert suggestions[0]["missing"] == ["camera-1"]


def test_suggest_rig_ignores_components_without_a_device(tmp_path: Path) -> None:
    (tmp_path / "newtonian-8in.yaml").write_text(VALID_RIG_YAML)
    rig_store.load_rigs(tmp_path)

    suggestions = rig_store.suggest_rig([])

    assert "main-scope" not in suggestions[0]["missing"]
    assert "guide-scope" not in suggestions[0]["missing"]


def test_suggest_rig_sorts_best_match_first(tmp_path: Path) -> None:
    (tmp_path / "a-minimal.yaml").write_text(MINIMAL_RIG_YAML)
    (tmp_path / "b-newtonian.yaml").write_text(VALID_RIG_YAML)
    rig_store.load_rigs(tmp_path)

    suggestions = rig_store.suggest_rig(["Telescope Simulator", "CCD Simulator"])

    assert [s["rigId"] for s in suggestions] == ["minimal", "newtonian-8in"]


def test_suggest_rig_scores_rig_with_no_device_components_as_none(tmp_path: Path) -> None:
    (tmp_path / "no-devices.yaml").write_text(
        'id: no-devices\nname: "No devices"\ncomponents:\n'
        "  - role: telescope\n    id: main-scope\n    apertureMm: 203\n"
    )
    rig_store.load_rigs(tmp_path)

    suggestions = rig_store.suggest_rig(["Telescope Simulator"])

    assert suggestions == [
        {
            "kind": "rigSuggestion",
            "rigId": "no-devices",
            "rigName": "No devices",
            "score": None,
            "matched": [],
            "missing": [],
        }
    ]


def test_suggest_rig_sorts_a_real_zero_score_ahead_of_a_none_score(tmp_path: Path) -> None:
    (tmp_path / "a-minimal.yaml").write_text(MINIMAL_RIG_YAML)
    (tmp_path / "b-no-devices.yaml").write_text(
        'id: no-devices\nname: "No devices"\ncomponents:\n'
        "  - role: telescope\n    id: main-scope\n    apertureMm: 203\n"
    )
    rig_store.load_rigs(tmp_path)

    suggestions = rig_store.suggest_rig([])

    assert [s["rigId"] for s in suggestions] == ["minimal", "no-devices"]
    assert suggestions[0]["score"] == 0.0
    assert suggestions[1]["score"] is None
