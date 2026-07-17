from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from indi_mcp import observatory_store

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
