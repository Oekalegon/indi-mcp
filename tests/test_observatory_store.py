from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from indi_mcp import observatory_store, visibility

_HORIZON_PROFILE_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "horizon_profiles"

VALID_OBSERVATORY_YAML = """
id: home-backyard
name: Home backyard observatory
latitudeDeg: 52.3676
longitudeDeg: 4.9041
elevationMeters: 4
"""

MINIMAL_OBSERVATORY_YAML = """
id: minimal
name: Minimal site
latitudeDeg: 0
longitudeDeg: 0
"""


@pytest.fixture(autouse=True)
def _reset_loaded_observatories() -> None:
    observatory_store._observatories = {}


def test_load_observatories_returns_empty_list_when_directory_missing(tmp_path: Path) -> None:
    observatories = observatory_store.load_observatories(tmp_path / "does-not-exist")

    assert observatories == []
    assert observatory_store.list_observatories() == []


def test_load_observatories_parses_valid_observatory_file(tmp_path: Path) -> None:
    (tmp_path / "home-backyard.yaml").write_text(VALID_OBSERVATORY_YAML)

    observatories = observatory_store.load_observatories(tmp_path)

    assert len(observatories) == 1
    observatory = observatories[0]
    assert observatory.id == "home-backyard"
    assert observatory.name == "Home backyard observatory"
    assert observatory.latitudeDeg == 52.3676
    assert observatory.longitudeDeg == 4.9041
    assert observatory.elevationMeters == 4


def test_load_observatories_defaults_elevation_to_zero(tmp_path: Path) -> None:
    (tmp_path / "minimal.yaml").write_text(MINIMAL_OBSERVATORY_YAML)

    observatories = observatory_store.load_observatories(tmp_path)

    assert observatories[0].elevationMeters == 0


def test_load_observatories_skips_files_with_invalid_yaml(tmp_path: Path) -> None:
    (tmp_path / "broken.yaml").write_text("id: [unterminated")
    (tmp_path / "minimal.yaml").write_text(MINIMAL_OBSERVATORY_YAML)

    observatories = observatory_store.load_observatories(tmp_path)

    assert [o.id for o in observatories] == ["minimal"]


def test_load_observatories_skips_a_yaml_named_directory(tmp_path: Path) -> None:
    (tmp_path / "not-a-file.yaml").mkdir()
    (tmp_path / "minimal.yaml").write_text(MINIMAL_OBSERVATORY_YAML)

    observatories = observatory_store.load_observatories(tmp_path)

    assert [o.id for o in observatories] == ["minimal"]


def test_load_observatories_skips_files_missing_required_fields(tmp_path: Path) -> None:
    (tmp_path / "incomplete.yaml").write_text("id: incomplete\nname: Incomplete site\n")
    (tmp_path / "minimal.yaml").write_text(MINIMAL_OBSERVATORY_YAML)

    observatories = observatory_store.load_observatories(tmp_path)

    assert [o.id for o in observatories] == ["minimal"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("latitudeDeg", 90.1),
        ("latitudeDeg", -90.1),
        ("longitudeDeg", 180.1),
        ("longitudeDeg", -180.1),
    ],
)
def test_load_observatories_skips_files_with_out_of_range_coordinates(
    tmp_path: Path, field: str, value: float
) -> None:
    (tmp_path / "out-of-range.yaml").write_text(
        f'id: out-of-range\nname: "Out of range"\n'
        f"latitudeDeg: 0\nlongitudeDeg: 0\n{field}: {value}\n"
    )
    (tmp_path / "minimal.yaml").write_text(MINIMAL_OBSERVATORY_YAML)

    observatories = observatory_store.load_observatories(tmp_path)

    assert [o.id for o in observatories] == ["minimal"]


def test_load_observatories_rejects_unknown_fields(tmp_path: Path) -> None:
    (tmp_path / "extra-field.yaml").write_text(MINIMAL_OBSERVATORY_YAML + "\nunknownField: true\n")

    observatories = observatory_store.load_observatories(tmp_path)

    assert observatories == []


def test_load_observatories_keeps_first_definition_on_duplicate_id(tmp_path: Path) -> None:
    (tmp_path / "a-first.yaml").write_text(MINIMAL_OBSERVATORY_YAML)
    (tmp_path / "b-second.yaml").write_text(
        MINIMAL_OBSERVATORY_YAML.replace("Minimal site", "Duplicate")
    )

    observatories = observatory_store.load_observatories(tmp_path)

    assert len(observatories) == 1
    assert observatories[0].name == "Minimal site"


def test_list_observatories_reports_id_and_name_only(tmp_path: Path) -> None:
    (tmp_path / "minimal.yaml").write_text(MINIMAL_OBSERVATORY_YAML)
    observatory_store.load_observatories(tmp_path)

    assert observatory_store.list_observatories() == [{"id": "minimal", "name": "Minimal site"}]


def test_get_observatory_returns_loaded_observatory(tmp_path: Path) -> None:
    (tmp_path / "minimal.yaml").write_text(MINIMAL_OBSERVATORY_YAML)
    observatory_store.load_observatories(tmp_path)

    observatory = observatory_store.get_observatory("minimal")

    assert observatory.id == "minimal"


def test_get_observatory_rejects_unknown_id(tmp_path: Path) -> None:
    observatory_store.load_observatories(tmp_path)

    with pytest.raises(ValueError, match="Unknown observatory location"):
        observatory_store.get_observatory("does-not-exist")


def test_observatory_model_rejects_out_of_range_latitude() -> None:
    with pytest.raises(ValidationError):
        observatory_store.Observatory(id="bad", name="Bad", latitudeDeg=90.1, longitudeDeg=0)


def test_observatory_model_rejects_out_of_range_longitude() -> None:
    with pytest.raises(ValidationError):
        observatory_store.Observatory(id="bad", name="Bad", latitudeDeg=0, longitudeDeg=-180.1)


def _minimal_observatory(observatory_id: str = "minimal") -> observatory_store.Observatory:
    return observatory_store.Observatory(
        id=observatory_id, name="Minimal site", latitudeDeg=0, longitudeDeg=0
    )


def test_save_observatory_writes_a_yaml_file_and_reloads_it(tmp_path: Path) -> None:
    observatory = _minimal_observatory()

    saved = observatory_store.save_observatory(observatory, directory=tmp_path)

    assert saved == observatory
    assert (tmp_path / "minimal.yaml").is_file()
    assert observatory_store.get_observatory("minimal") == observatory


def test_save_observatory_roundtrips_through_yaml(tmp_path: Path) -> None:
    observatory = observatory_store.Observatory(
        id="home-backyard",
        name="Home backyard observatory",
        latitudeDeg=52.3676,
        longitudeDeg=4.9041,
        elevationMeters=4,
    )

    observatory_store.save_observatory(observatory, directory=tmp_path)

    reloaded = observatory_store.Observatory.model_validate(
        yaml.safe_load((tmp_path / "home-backyard.yaml").read_text())
    )
    assert reloaded == observatory


def test_save_observatory_rejects_overwriting_an_existing_file_by_default(tmp_path: Path) -> None:
    observatory_store.save_observatory(_minimal_observatory(), directory=tmp_path)

    with pytest.raises(ValueError, match="already exists"):
        observatory_store.save_observatory(_minimal_observatory(), directory=tmp_path)


def test_save_observatory_allows_overwrite_when_explicitly_requested(tmp_path: Path) -> None:
    observatory_store.save_observatory(_minimal_observatory(), directory=tmp_path)
    updated = observatory_store.Observatory(
        id="minimal", name="Renamed site", latitudeDeg=1, longitudeDeg=1
    )

    saved = observatory_store.save_observatory(updated, overwrite=True, directory=tmp_path)

    assert saved.name == "Renamed site"
    assert observatory_store.get_observatory("minimal").name == "Renamed site"


def test_save_observatory_creates_the_observatories_directory_if_missing(tmp_path: Path) -> None:
    missing_dir = tmp_path / "does-not-exist-yet"

    observatory_store.save_observatory(_minimal_observatory(), directory=missing_dir)

    assert (missing_dir / "minimal.yaml").is_file()


@pytest.mark.parametrize("bad_id", ["", ".", "..", "a/b", "a\\b", "../escape"])
def test_save_observatory_rejects_ids_that_are_not_safe_filenames(
    tmp_path: Path, bad_id: str
) -> None:
    observatory = observatory_store.Observatory(
        id=bad_id, name="Bad id", latitudeDeg=0, longitudeDeg=0
    )

    with pytest.raises(ValueError, match="Invalid observatory id"):
        observatory_store.save_observatory(observatory, directory=tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_save_observatory_uses_the_default_directory_when_none_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(observatory_store.OBSERVATORIES_DIR_ENV, str(tmp_path))

    observatory_store.save_observatory(_minimal_observatory())

    assert (tmp_path / "minimal.yaml").is_file()


def test_save_observatory_succeeds_despite_other_invalid_observatory_files_in_the_directory(
    tmp_path: Path,
) -> None:
    (tmp_path / "broken.yaml").write_text("id: [unterminated")

    saved = observatory_store.save_observatory(_minimal_observatory(), directory=tmp_path)

    assert saved == observatory_store.get_observatory("minimal")


def test_save_observatory_rejects_an_id_whose_file_path_is_already_a_directory(
    tmp_path: Path,
) -> None:
    (tmp_path / "minimal.yaml").mkdir()

    with pytest.raises(ValueError, match="is a directory"):
        observatory_store.save_observatory(_minimal_observatory(), directory=tmp_path)


def _device(
    name: str,
    *,
    lat: str = "52.3676",
    lon: str = "4.9041",
    elev: str = "4",
    state: str | None = "Ok",
    no_property: bool = False,
) -> observatory_store.DraftLocationDeviceInfo:
    coord = None if no_property else {"LAT": lat, "LONG": lon, "ELEV": elev}
    return {"name": name, "geographicCoord": coord, "state": None if no_property else state}


def test_draft_observatory_drafts_from_a_single_devices_geographic_coord() -> None:
    draft = observatory_store.draft_observatory([_device("Telescope Simulator")])

    assert draft["kind"] == "observatoryDraft"
    assert draft["id"] is None
    assert draft["name"] is None
    assert draft["latitudeDeg"] == 52.3676
    assert draft["longitudeDeg"] == 4.9041
    assert draft["elevationMeters"] == 4
    assert draft["sourceDevice"] == "Telescope Simulator"
    assert draft["notes"] == []


def test_draft_observatory_converts_long_from_indis_0_360_convention() -> None:
    draft = observatory_store.draft_observatory([_device("Telescope Simulator", lon="350")])

    assert draft["longitudeDeg"] == -10


def test_draft_observatory_ignores_devices_with_no_geographic_coord() -> None:
    draft = observatory_store.draft_observatory(
        [_device("CCD Simulator", no_property=True), _device("Telescope Simulator")]
    )

    assert draft["sourceDevice"] == "Telescope Simulator"


def test_draft_observatory_with_no_devices_returns_an_empty_draft_with_a_note() -> None:
    draft = observatory_store.draft_observatory([])

    assert draft == {
        "kind": "observatoryDraft",
        "id": None,
        "name": None,
        "latitudeDeg": None,
        "longitudeDeg": None,
        "elevationMeters": None,
        "sourceDevice": None,
        "notes": [
            "No connected device currently reports GEOGRAPHIC_COORD; fill in latitudeDeg/"
            "longitudeDeg/elevationMeters by hand before saving."
        ],
    }


def test_draft_observatory_flags_the_all_zero_default_as_a_missing_fix() -> None:
    draft = observatory_store.draft_observatory(
        [_device("GPS Simulator", lat="0", lon="0", elev="0")]
    )

    assert draft["latitudeDeg"] == 0
    assert any("0/0/0" in note for note in draft["notes"])


def test_draft_observatory_flags_a_non_ok_state() -> None:
    draft = observatory_store.draft_observatory([_device("GPS Simulator", state="Busy")])

    assert any("'Busy'" in note for note in draft["notes"])


def test_draft_observatory_uses_first_device_and_notes_others_on_multiple_fixes() -> None:
    draft = observatory_store.draft_observatory(
        [
            _device("Telescope Simulator", lat="52.3676", lon="4.9041"),
            _device("GPS Simulator", lat="1", lon="1"),
        ]
    )

    assert draft["sourceDevice"] == "Telescope Simulator"
    assert any("Telescope Simulator" in note and "GPS Simulator" in note for note in draft["notes"])


def test_observatory_horizon_profile_defaults_to_none() -> None:
    observatory = observatory_store.Observatory(
        id="no-profile", name="No profile", latitudeDeg=0, longitudeDeg=0
    )

    assert observatory.horizonProfile is None


def test_observatory_accepts_a_valid_horizon_profile() -> None:
    observatory = observatory_store.Observatory(
        id="with-profile",
        name="With profile",
        latitudeDeg=0,
        longitudeDeg=0,
        horizonProfile=[
            {"azimuthDeg": 0, "altitudeDeg": 5},
            {"azimuthDeg": 90, "altitudeDeg": 12},
            {"azimuthDeg": 180, "altitudeDeg": 8},
        ],
    )

    assert observatory.horizonProfile is not None
    assert [p.azimuthDeg for p in observatory.horizonProfile] == [0, 90, 180]
    assert [p.altitudeDeg for p in observatory.horizonProfile] == [5, 12, 8]


def test_observatory_rejects_horizon_profile_with_azimuths_out_of_order() -> None:
    with pytest.raises(ValidationError, match="sorted"):
        observatory_store.Observatory(
            id="unsorted",
            name="Unsorted",
            latitudeDeg=0,
            longitudeDeg=0,
            horizonProfile=[
                {"azimuthDeg": 90, "altitudeDeg": 5},
                {"azimuthDeg": 10, "altitudeDeg": 5},
            ],
        )


def test_observatory_rejects_horizon_profile_with_duplicate_azimuths() -> None:
    with pytest.raises(ValidationError, match="unique"):
        observatory_store.Observatory(
            id="duplicate",
            name="Duplicate",
            latitudeDeg=0,
            longitudeDeg=0,
            horizonProfile=[
                {"azimuthDeg": 10, "altitudeDeg": 5},
                {"azimuthDeg": 10, "altitudeDeg": 7},
            ],
        )


def test_horizon_point_rejects_azimuth_out_of_range() -> None:
    with pytest.raises(ValidationError):
        observatory_store.HorizonPoint(azimuthDeg=360, altitudeDeg=5)


def test_horizon_point_rejects_altitude_out_of_range() -> None:
    with pytest.raises(ValidationError):
        observatory_store.HorizonPoint(azimuthDeg=10, altitudeDeg=91)


def test_save_and_reload_observatory_round_trips_horizon_profile(tmp_path: Path) -> None:
    observatory = observatory_store.Observatory(
        id="round-trip",
        name="Round trip",
        latitudeDeg=52.3676,
        longitudeDeg=4.9041,
        horizonProfile=[
            {"azimuthDeg": 0, "altitudeDeg": 5},
            {"azimuthDeg": 180, "altitudeDeg": 10},
        ],
    )

    observatory_store.save_observatory(observatory, directory=tmp_path)
    reloaded = observatory_store.get_observatory("round-trip")

    assert reloaded.horizonProfile is not None
    assert [p.model_dump() for p in reloaded.horizonProfile] == [
        {"azimuthDeg": 0, "altitudeDeg": 5},
        {"azimuthDeg": 180, "altitudeDeg": 10},
    ]


def test_parse_hzn_profile_parses_azimuth_altitude_pairs_and_skips_blank_lines() -> None:
    text = "0,5.23\n1,6.03\n\n359,4.61\n"

    points = observatory_store.parse_hzn_profile(text)

    assert [(p.azimuthDeg, p.altitudeDeg) for p in points] == [
        (0.0, 5.23),
        (1.0, 6.03),
        (359.0, 4.61),
    ]


def test_parse_hzn_profile_raises_with_line_number_on_malformed_line() -> None:
    text = "0,5.23\nnot-a-pair\n2,6.0\n"

    with pytest.raises(ValueError, match="line 2"):
        observatory_store.parse_hzn_profile(text)


def test_parse_hzn_profile_raises_with_line_number_on_out_of_range_value() -> None:
    text = "0,5.23\n1,95.0\n"

    with pytest.raises(ValueError, match="line 2"):
        observatory_store.parse_hzn_profile(text)


def test_parse_hzn_profile_result_is_accepted_by_observatory_horizon_profile() -> None:
    text = "0,5.23\n90,12.0\n180,8.5\n270,6.0\n"

    points = observatory_store.parse_hzn_profile(text)
    observatory = observatory_store.Observatory(
        id="from-hzn", name="From hzn", latitudeDeg=0, longitudeDeg=0, horizonProfile=points
    )

    assert observatory.horizonProfile == points


# --- Real .hzn fixtures (tests/fixtures/horizon_profiles/) -------------------------------


@pytest.mark.parametrize(
    "filename",
    ["center.hzn", "north.hzn", "north_east.hzn", "north_west.hzn", "south_west.hzn", "west.hzn"],
)
def test_parse_hzn_profile_parses_every_real_fixture_into_a_valid_observatory(
    filename: str,
) -> None:
    text = (_HORIZON_PROFILE_FIXTURES_DIR / filename).read_text()

    points = observatory_store.parse_hzn_profile(text)
    observatory = observatory_store.Observatory(
        id="fixture", name="Fixture", latitudeDeg=0, longitudeDeg=0, horizonProfile=points
    )

    assert len(points) == 360
    assert [p.azimuthDeg for p in points] == list(range(360))
    assert observatory.horizonProfile == points


def test_parse_hzn_profile_handles_a_flat_plateau_segment_from_a_real_fixture() -> None:
    """north_west.hzn has a flat 40.09 degree plateau from azimuth 199 through 225 (a building
    edge, dropping to 31.94 at azimuth 226) — a real-world case for the flat-segment
    (zero-slope) branch of the interpolator."""
    text = (_HORIZON_PROFILE_FIXTURES_DIR / "north_west.hzn").read_text()
    points = observatory_store.parse_hzn_profile(text)

    for azimuth_deg in (199, 210, 225):
        assert visibility._horizon_altitude_at(points, azimuth_deg) == pytest.approx(40.09)
    # The very next point drops off the plateau — should not still read the plateau's value.
    assert visibility._horizon_altitude_at(points, 226) == pytest.approx(31.94)
