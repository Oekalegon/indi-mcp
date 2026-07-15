from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from indi_mcp import rig_store

VALID_RIG_YAML = """
id: newtonian-8in
name: 8" Newtonian imaging rig
mount:
  device: "Telescope Simulator"
imagingTrain:
  telescope:
    apertureMm: 203
    focalLengthMm: 1000
  focuser:
    device: "Focuser Simulator"
    minPosition: 0
    maxPosition: 50000
  filterWheel:
    device: "Filter Wheel Simulator"
    slots:
      1: Luminance
      2: Red
      3: Green
      4: Blue
  rotator:
    device: "Rotator Simulator"
  camera:
    device: "ZWO CCD ASI2600MM Pro"
    cooled: true
    pixelsX: 6248
    pixelsY: 4176
    pixelSizeMicron: 3.76
    bitDepth: 16
guidingTrain:
  telescope:
    apertureMm: 60
    focalLengthMm: 240
  camera:
    device: "ZWO CCD ASI120MM Mini"
    cooled: false
    pixelsX: 1280
    pixelsY: 960
    pixelSizeMicron: 3.75
    bitDepth: 12
devices:
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
name: Minimal rig (no guide train)
mount:
  device: "Telescope Simulator"
imagingTrain:
  telescope:
    apertureMm: 100
    focalLengthMm: 500
  camera:
    device: "CCD Simulator"
    pixelsX: 1000
    pixelsY: 1000
    pixelSizeMicron: 5.0
    bitDepth: 16
"""

OAG_RIG_YAML = """
id: oag-rig
name: Off-axis guided rig
mount:
  device: "Telescope Simulator"
imagingTrain:
  telescope:
    apertureMm: 200
    focalLengthMm: 800
  camera:
    device: "CCD Simulator"
    pixelsX: 4000
    pixelsY: 3000
    pixelSizeMicron: 3.8
    bitDepth: 16
  offAxisGuider:
    camera:
      device: "Guide Camera Simulator"
      pixelsX: 1280
      pixelsY: 960
      pixelSizeMicron: 3.75
      bitDepth: 12
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
    assert rig.mount.device == "Telescope Simulator"
    assert rig.imagingTrain.telescope.apertureMm == 203
    assert rig.imagingTrain.focuser is not None
    assert rig.imagingTrain.focuser.maxPosition == 50000
    assert rig.imagingTrain.filterWheel is not None
    assert rig.imagingTrain.filterWheel.slots[2] == "Red"
    assert rig.imagingTrain.rotator is not None
    assert rig.imagingTrain.rotator.device == "Rotator Simulator"
    assert rig.imagingTrain.camera.device == "ZWO CCD ASI2600MM Pro"
    assert rig.imagingTrain.offAxisGuider is None
    assert rig.guidingTrain is not None
    assert rig.guidingTrain.telescope.focalLengthMm == 240
    assert rig.guidingTrain.camera.bitDepth == 12
    assert rig.guidingTrain.focuser is None
    assert [(d.role, d.device) for d in rig.devices] == [
        ("powerHub", "Pegasus PPBA"),
        ("observatoryControl", "Dome Simulator"),
        ("flatScreen", "Flat Panel Simulator"),
        ("dewHeater", "Pegasus PPBA:Dew A"),
        ("dewHeater", "Pegasus PPBA:Dew B"),
    ]


def test_load_rigs_allows_omitting_optional_fields(tmp_path: Path) -> None:
    (tmp_path / "minimal.yaml").write_text(MINIMAL_RIG_YAML)

    rigs = rig_store.load_rigs(tmp_path)

    assert len(rigs) == 1
    rig = rigs[0]
    assert rig.imagingTrain.focuser is None
    assert rig.imagingTrain.filterWheel is None
    assert rig.imagingTrain.rotator is None
    assert rig.imagingTrain.offAxisGuider is None
    assert rig.guidingTrain is None
    assert rig.devices == []


def test_load_rigs_parses_off_axis_guider(tmp_path: Path) -> None:
    (tmp_path / "oag-rig.yaml").write_text(OAG_RIG_YAML)

    rigs = rig_store.load_rigs(tmp_path)

    assert len(rigs) == 1
    rig = rigs[0]
    assert rig.imagingTrain.offAxisGuider is not None
    assert rig.imagingTrain.offAxisGuider.camera.device == "Guide Camera Simulator"
    assert rig.guidingTrain is None


def test_rig_rejects_both_off_axis_guider_and_guiding_train(tmp_path: Path) -> None:
    combined = OAG_RIG_YAML + (
        "guidingTrain:\n"
        "  telescope:\n"
        "    apertureMm: 60\n"
        "    focalLengthMm: 240\n"
        "  camera:\n"
        '    device: "ZWO CCD ASI120MM Mini"\n'
        "    pixelsX: 1280\n"
        "    pixelsY: 960\n"
        "    pixelSizeMicron: 3.75\n"
        "    bitDepth: 12\n"
    )

    with pytest.raises(ValidationError, match="mutually exclusive"):
        rig_store.Rig.model_validate(yaml.safe_load(combined))


def test_load_rigs_accepts_unanticipated_device_roles(tmp_path: Path) -> None:
    (tmp_path / "minimal.yaml").write_text(
        MINIMAL_RIG_YAML + '\ndevices:\n  - role: allSkyCamera\n    device: "All Sky Simulator"\n'
    )

    rigs = rig_store.load_rigs(tmp_path)

    assert len(rigs) == 1
    assert rigs[0].devices == [
        rig_store.AuxiliaryDevice(role="allSkyCamera", device="All Sky Simulator")
    ]


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


def test_load_rigs_skips_files_with_both_guiding_train_and_off_axis_guider(
    tmp_path: Path,
) -> None:
    combined = OAG_RIG_YAML + (
        "guidingTrain:\n"
        "  telescope:\n"
        "    apertureMm: 60\n"
        "    focalLengthMm: 240\n"
        "  camera:\n"
        '    device: "ZWO CCD ASI120MM Mini"\n'
        "    pixelsX: 1280\n"
        "    pixelsY: 960\n"
        "    pixelSizeMicron: 3.75\n"
        "    bitDepth: 12\n"
    )
    (tmp_path / "combined.yaml").write_text(combined)
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
    assert rigs[0].name == "Minimal rig (no guide train)"


def test_list_rigs_reports_id_and_name_only(tmp_path: Path) -> None:
    (tmp_path / "minimal.yaml").write_text(MINIMAL_RIG_YAML)
    rig_store.load_rigs(tmp_path)

    assert rig_store.list_rigs() == [{"id": "minimal", "name": "Minimal rig (no guide train)"}]


def test_get_rig_returns_loaded_rig(tmp_path: Path) -> None:
    (tmp_path / "minimal.yaml").write_text(MINIMAL_RIG_YAML)
    rig_store.load_rigs(tmp_path)

    rig = rig_store.get_rig("minimal")

    assert rig.id == "minimal"


def test_get_rig_rejects_unknown_id(tmp_path: Path) -> None:
    rig_store.load_rigs(tmp_path)

    with pytest.raises(ValueError, match="Unknown rig"):
        rig_store.get_rig("does-not-exist")
