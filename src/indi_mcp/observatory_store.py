"""Loading and querying observatory location definitions.

An observatory location describes where the equipment is physically set up
— latitude, longitude, and elevation — plus a name/id. INDI has no protocol
representation for this at all (unlike a rig's camera pixel geometry, which
a device can at least partially report), so it is pure operator knowledge,
needed by astronomical calculations that depend on the observer's position
on Earth (e.g. INDIMCP-29's object-above-horizon check). See
`docs/ObservatorySchema.md` for the full schema reference and rationale.

Locations are YAML documents, not SQLite rows, and are kept in their own
store rather than folded into the rig store: a rig describes *what* is
mounted, a location describes *where*, and the same rig can be used from
more than one site. This module mirrors `rig_store`'s loading/saving
discipline (`yaml.safe_load`, skip-and-log invalid files, exclusive-create
unless overwriting), but has no `suggest_location`/`check_location`
equivalent — there is no INDI-visible signal to cross-check a location
against, so it is always selected explicitly by `id`.
"""

import logging
import os
from pathlib import Path
from typing import TypedDict

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)

__all__ = [
    "Observatory",
    "ObservatorySummary",
    "get_observatory",
    "list_observatories",
    "load_observatories",
    "save_observatory",
]

OBSERVATORIES_DIR_ENV = "INDI_MCP_OBSERVATORIES_DIR"
_DEFAULT_OBSERVATORIES_DIR = Path("observatories")


class _StrictModel(BaseModel):
    """Base for observatory schema models: reject unknown fields from hand-edited/uploaded YAML."""

    model_config = ConfigDict(extra="forbid")


class Observatory(_StrictModel):
    """A single observatory location definition, as declared in one `observatories/*.yaml` file.

    `latitudeDeg`/`longitudeDeg`/`elevationMeters` map directly onto
    astropy's `EarthLocation.from_geodetic(lon, lat, height)`, which is what
    consumers such as INDIMCP-29's horizon check construct the observer
    frame from. Latitude/longitude bounds are validated because a value
    outside them is unambiguously a mistake (e.g. unconverted
    degrees/minutes/seconds), not a legitimate location.
    """

    id: str
    name: str
    latitudeDeg: float = Field(ge=-90, le=90)
    longitudeDeg: float = Field(ge=-180, le=180)
    elevationMeters: float = 0


class ObservatorySummary(TypedDict):
    """The id/name of a loaded observatory, without its full definition."""

    id: str
    name: str


_observatories: dict[str, Observatory] = {}


def _observatories_dir() -> Path:
    return Path(os.environ.get(OBSERVATORIES_DIR_ENV, _DEFAULT_OBSERVATORIES_DIR))


def load_observatories(directory: Path | None = None) -> list[Observatory]:
    """Load every `*.yaml` observatory location definition from `directory` into memory.

    Defaults to `$INDI_MCP_OBSERVATORIES_DIR`, falling back to
    `./observatories`. Files that fail to parse or don't match the schema
    are logged and skipped rather than aborting the whole load, since this
    config may be hand-edited or uploaded by a client. A duplicate `id`
    across files keeps whichever file was loaded first (files are loaded in
    sorted filename order).
    """
    global _observatories
    directory = directory if directory is not None else _observatories_dir()
    observatories: dict[str, Observatory] = {}
    if not directory.is_dir():
        logger.info(
            "Observatories directory does not exist, no observatories loaded: %s", directory
        )
        _observatories = observatories
        return []
    for path in sorted(directory.glob("*.yaml")):
        if not path.is_file():
            logger.warning("Skipping non-file observatory path %s", path)
            continue
        try:
            raw = yaml.safe_load(path.read_text())
            observatory = Observatory.model_validate(raw)
        except (yaml.YAMLError, ValidationError) as exc:
            logger.warning("Skipping invalid observatory file %s: %s", path, exc)
            continue
        if observatory.id in observatories:
            logger.warning(
                "Duplicate observatory id %r in %s, keeping first definition", observatory.id, path
            )
            continue
        observatories[observatory.id] = observatory
    _observatories = observatories
    logger.info("Loaded %d observatory location(s) from %s", len(observatories), directory)
    return list(observatories.values())


def list_observatories() -> list[ObservatorySummary]:
    """List the id/name of every currently loaded observatory location."""
    return [
        {"id": observatory.id, "name": observatory.name} for observatory in _observatories.values()
    ]


def get_observatory(observatory_id: str) -> Observatory:
    """Return the full definition of the observatory location identified by `observatory_id`."""
    observatory = _observatories.get(observatory_id)
    if observatory is None:
        raise ValueError(f"Unknown observatory location: {observatory_id!r}")
    return observatory


def save_observatory(
    observatory: Observatory, *, overwrite: bool = False, directory: Path | None = None
) -> Observatory:
    """Write `observatory` to `<directory>/<observatory.id>.yaml` and reload it into memory.

    `observatory` is already a validated `Observatory` (pydantic validation
    happens when it's constructed), so this only needs to worry about the
    filesystem: it refuses to replace an existing `<observatory.id>.yaml`
    unless `overwrite` is set, since reusing an `id` could otherwise
    silently destroy a previously saved location with no warning. The
    existence check and the write happen as one atomic file-open
    (exclusive-create unless `overwrite`), so two concurrent saves of the
    same new `id` can't both slip past the check. Reloads every observatory
    in `directory` afterwards (see `load_observatories`) so the saved
    location is immediately available by `id` to `get_observatory`.
    """
    if (
        not observatory.id
        or observatory.id in (".", "..")
        or "/" in observatory.id
        or "\\" in observatory.id
    ):
        raise ValueError(f"Invalid observatory id for a filename: {observatory.id!r}")
    directory = directory if directory is not None else _observatories_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except NotADirectoryError as exc:
        raise ValueError(f"Cannot create observatories directory {directory}: {exc}") from exc
    path = directory / f"{observatory.id}.yaml"
    if path.is_dir():
        raise ValueError(
            f"Cannot save observatory {observatory.id!r}: {path} is a directory, not a file"
        )
    content = yaml.safe_dump(observatory.model_dump(), sort_keys=False)
    try:
        with path.open("w" if overwrite else "x", encoding="utf-8") as f:
            f.write(content)
    except FileExistsError as exc:
        raise ValueError(
            f"An observatory file already exists for id {observatory.id!r} ({path}); "
            "pass overwrite=True to replace it."
        ) from exc
    logger.info("Saved observatory location %r to %s", observatory.id, path)
    load_observatories(directory)
    return get_observatory(observatory.id)
