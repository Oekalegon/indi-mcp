from pathlib import Path

import pytest
import yaml

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


def test_load_rigs_skips_a_yaml_named_directory(tmp_path: Path) -> None:
    (tmp_path / "not-a-file.yaml").mkdir()
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


def test_check_rig_reports_ok_when_all_devices_connected(tmp_path: Path) -> None:
    (tmp_path / "minimal.yaml").write_text(MINIMAL_RIG_YAML)
    rig_store.load_rigs(tmp_path)

    result = rig_store.check_rig("minimal", ["Telescope Simulator", "CCD Simulator"])

    assert result == {
        "kind": "rigCheck",
        "rigId": "minimal",
        "ok": True,
        "present": ["mount-1", "camera-1"],
        "missing": [],
    }


def test_check_rig_warns_rather_than_fails_on_missing_devices(tmp_path: Path) -> None:
    (tmp_path / "minimal.yaml").write_text(MINIMAL_RIG_YAML)
    rig_store.load_rigs(tmp_path)

    result = rig_store.check_rig("minimal", ["Telescope Simulator"])

    assert result["ok"] is False
    assert result["present"] == ["mount-1"]
    assert result["missing"] == ["camera-1"]


def test_check_rig_ignores_components_without_a_device(tmp_path: Path) -> None:
    (tmp_path / "newtonian-8in.yaml").write_text(VALID_RIG_YAML)
    rig_store.load_rigs(tmp_path)

    result = rig_store.check_rig("newtonian-8in", [])

    assert "main-scope" not in result["missing"]
    assert "guide-scope" not in result["missing"]


def test_check_rig_reports_ok_when_rig_has_no_device_components(tmp_path: Path) -> None:
    (tmp_path / "no-devices.yaml").write_text(
        'id: no-devices\nname: "No devices"\ncomponents:\n'
        "  - role: telescope\n    id: main-scope\n    apertureMm: 203\n"
    )
    rig_store.load_rigs(tmp_path)

    result = rig_store.check_rig("no-devices", [])

    assert result == {
        "kind": "rigCheck",
        "rigId": "no-devices",
        "ok": True,
        "present": [],
        "missing": [],
    }


def test_check_rig_rejects_unknown_id(tmp_path: Path) -> None:
    rig_store.load_rigs(tmp_path)

    with pytest.raises(ValueError, match="Unknown rig"):
        rig_store.check_rig("does-not-exist", [])


def _device(
    name: str,
    family: str | None,
    *,
    ccd_info: dict[str, str] | None = None,
    filter_names: dict[str, str] | None = None,
    focus_range: tuple[float, float] | None = None,
) -> rig_store.DraftDeviceInfo:
    return {
        "name": name,
        "family": family,
        "ccdInfo": ccd_info,
        "filterNames": filter_names,
        "focusRange": focus_range,
    }


def test_draft_rig_drafts_a_single_camera_from_ccd_info() -> None:
    draft = rig_store.draft_rig(
        [
            _device(
                "ZWO CCD ASI2600MM Pro",
                "CCDs",
                ccd_info={
                    "CCD_MAX_X": "6248",
                    "CCD_MAX_Y": "4176",
                    "CCD_PIXEL_SIZE": "3.76",
                    "CCD_BITSPERPIXEL": "16",
                },
            )
        ]
    )

    assert draft["components"] == [
        rig_store.Component(
            role="camera",
            id="ZWO CCD ASI2600MM Pro",
            device="ZWO CCD ASI2600MM Pro",
            pixelsX=6248,
            pixelsY=4176,
            pixelSizeMicron=3.76,
            bitDepth=16,
        )
    ]
    assert any("apertureMm" in note for note in draft["notes"])


def test_draft_rig_drafts_multiple_cameras_as_guide_cameras_with_a_note() -> None:
    draft = rig_store.draft_rig(
        [
            _device("ZWO CCD ASI2600MM Pro", "CCDs"),
            _device("ZWO CCD ASI120MM Mini", "CCDs"),
        ]
    )

    assert [c.role for c in draft["components"]] == ["guideCamera", "guideCamera"]
    assert any("more than one camera" in note.lower() for note in draft["notes"])


def test_draft_rig_drafts_a_camera_with_no_ccd_info_yet() -> None:
    draft = rig_store.draft_rig([_device("CCD Simulator", "CCDs", ccd_info=None)])

    assert draft["components"] == [
        rig_store.Component(role="camera", id="CCD Simulator", device="CCD Simulator")
    ]


def test_draft_rig_drafts_a_filter_wheel_with_slots_from_filter_name() -> None:
    draft = rig_store.draft_rig(
        [
            _device(
                "Filter Wheel Simulator",
                "Filter Wheels",
                filter_names={
                    "FILTER_SLOT_NAME_1": "Luminance",
                    "FILTER_SLOT_NAME_2": "Red",
                },
            )
        ]
    )

    assert draft["components"] == [
        rig_store.Component(
            role="filterWheel",
            id="Filter Wheel Simulator",
            device="Filter Wheel Simulator",
            slots={1: "Luminance", 2: "Red"},
        )
    ]
    assert draft["notes"] == []


def test_draft_rig_drafts_a_filter_wheel_with_no_filter_name_yet() -> None:
    draft = rig_store.draft_rig(
        [_device("Filter Wheel Simulator", "Filter Wheels", filter_names=None)]
    )

    assert draft["components"] == [
        rig_store.Component(
            role="filterWheel", id="Filter Wheel Simulator", device="Filter Wheel Simulator"
        )
    ]
    assert draft["notes"] == []


def test_draft_rig_drafts_a_focuser_with_its_position_range() -> None:
    draft = rig_store.draft_rig(
        [_device("Focuser Simulator", "Focusers", focus_range=(0.0, 50000.0))]
    )

    assert draft["components"] == [
        rig_store.Component(
            role="focuser",
            id="Focuser Simulator",
            device="Focuser Simulator",
            minPosition=0,
            maxPosition=50000,
        )
    ]
    assert draft["notes"] == []


def test_draft_rig_drafts_a_mount() -> None:
    draft = rig_store.draft_rig([_device("Telescope Simulator", "Telescopes")])

    assert draft["components"] == [
        rig_store.Component(role="mount", id="Telescope Simulator", device="Telescope Simulator")
    ]
    assert draft["notes"] == []


def test_draft_rig_ignores_devices_with_no_recognized_family() -> None:
    draft = rig_store.draft_rig([_device("Pegasus PPBA", "Power"), _device("Unknown Widget", None)])

    assert draft["components"] == []
    assert draft["notes"] == []


def test_draft_rig_with_no_devices_returns_an_empty_draft() -> None:
    draft = rig_store.draft_rig([])

    assert draft == {"kind": "rigDraft", "components": [], "notes": []}


def _minimal_rig(rig_id: str = "minimal") -> rig_store.Rig:
    return rig_store.Rig(
        id=rig_id,
        name="Minimal rig",
        components=[
            rig_store.Component(role="mount", id="mount-1", device="Telescope Simulator"),
            rig_store.Component(
                role="camera",
                id="camera-1",
                device="CCD Simulator",
                pixelsX=1000,
                pixelsY=1000,
                pixelSizeMicron=5.0,
                bitDepth=16,
            ),
        ],
    )


def test_save_rig_writes_a_yaml_file_and_reloads_it(tmp_path: Path) -> None:
    rig = _minimal_rig()

    saved = rig_store.save_rig(rig, directory=tmp_path)

    assert saved == rig
    assert (tmp_path / "minimal.yaml").is_file()
    assert rig_store.get_rig("minimal") == rig


def test_save_rig_roundtrips_through_yaml(tmp_path: Path) -> None:
    rig = rig_store.Rig(
        id="newtonian-8in",
        name="8in Newtonian",
        components=[
            rig_store.Component(
                role="filterWheel",
                id="filter-wheel-1",
                device="Filter Wheel Simulator",
                slots={1: "Luminance", 2: "Red"},
            ),
            rig_store.Component(role="telescope", id="main-scope", apertureMm=203.0),
        ],
    )

    rig_store.save_rig(rig, directory=tmp_path)

    reloaded = rig_store.Rig.model_validate(
        yaml.safe_load((tmp_path / "newtonian-8in.yaml").read_text())
    )
    assert reloaded == rig


def test_save_rig_rejects_overwriting_an_existing_file_by_default(tmp_path: Path) -> None:
    rig_store.save_rig(_minimal_rig(), directory=tmp_path)

    with pytest.raises(ValueError, match="already exists"):
        rig_store.save_rig(_minimal_rig(), directory=tmp_path)


def test_save_rig_allows_overwrite_when_explicitly_requested(tmp_path: Path) -> None:
    rig_store.save_rig(_minimal_rig(), directory=tmp_path)
    updated = rig_store.Rig(id="minimal", name="Renamed rig", components=[])

    saved = rig_store.save_rig(updated, overwrite=True, directory=tmp_path)

    assert saved.name == "Renamed rig"
    assert rig_store.get_rig("minimal").name == "Renamed rig"


def test_save_rig_creates_the_rigs_directory_if_missing(tmp_path: Path) -> None:
    missing_dir = tmp_path / "does-not-exist-yet"

    rig_store.save_rig(_minimal_rig(), directory=missing_dir)

    assert (missing_dir / "minimal.yaml").is_file()


@pytest.mark.parametrize("bad_id", ["", ".", "..", "a/b", "a\\b", "../escape"])
def test_save_rig_rejects_ids_that_are_not_safe_filenames(tmp_path: Path, bad_id: str) -> None:
    rig = rig_store.Rig(id=bad_id, name="Bad id", components=[])

    with pytest.raises(ValueError, match="Invalid rig id"):
        rig_store.save_rig(rig, directory=tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_save_rig_uses_the_default_rigs_directory_when_none_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(rig_store.RIGS_DIR_ENV, str(tmp_path))

    rig_store.save_rig(_minimal_rig())

    assert (tmp_path / "minimal.yaml").is_file()


def test_save_rig_succeeds_despite_other_invalid_rig_files_in_the_directory(
    tmp_path: Path,
) -> None:
    (tmp_path / "broken.yaml").write_text("id: [unterminated")

    saved = rig_store.save_rig(_minimal_rig(), directory=tmp_path)

    assert saved == rig_store.get_rig("minimal")


def test_save_rig_rejects_an_id_whose_file_path_is_already_a_directory(tmp_path: Path) -> None:
    (tmp_path / "minimal.yaml").mkdir()

    with pytest.raises(ValueError, match="is a directory"):
        rig_store.save_rig(_minimal_rig(), directory=tmp_path)
