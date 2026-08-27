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
equivalent — a saved location is always selected explicitly by `id`, never
auto-detected. It does have a `draft_observatory` (mirroring `rig_store`'s
`draft_rig`): INDI's `GEOGRAPHIC_COORD` standard property (LAT/LONG/ELEV) is
exposed by GPS drivers and often by mount drivers too, so a connected
device's live reading can pre-fill a draft the operator reviews and saves
via `save_observatory` — advisory only, never auto-selected/authoritative,
same as `draft_rig`.
"""

import logging
import os
from collections.abc import Iterable
from pathlib import Path
from typing import TypedDict

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

logger = logging.getLogger(__name__)

__all__ = [
    "DraftLocationDeviceInfo",
    "HorizonPoint",
    "Observatory",
    "ObservatoryDraft",
    "ObservatorySummary",
    "draft_observatory",
    "get_observatory",
    "list_observatories",
    "load_observatories",
    "parse_hzn_profile",
    "save_observatory",
]

OBSERVATORIES_DIR_ENV = "INDI_MCP_OBSERVATORIES_DIR"
_DEFAULT_OBSERVATORIES_DIR = Path("observatories")


class _StrictModel(BaseModel):
    """Base for observatory schema models: reject unknown fields from hand-edited/uploaded YAML."""

    model_config = ConfigDict(extra="forbid")


class HorizonPoint(_StrictModel):
    """One azimuth/altitude sample of a horizon-obstruction profile (INDIMCP-135).

    `azimuthDeg` follows the usual astronomical convention (0=North, 90=East, measured
    clockwise), matching what `visibility.compute_visibility` reads back from astropy's
    `AltAz` frame. `altitudeDeg` is the height above the geometric horizon, in degrees, that a
    real obstruction (a tree, a building, a hill) blocks up to at that azimuth — i.e. the
    minimum altitude a target must clear to be observable in that direction, not the
    obstruction's own physical height.
    """

    azimuthDeg: float = Field(ge=0, lt=360)
    altitudeDeg: float = Field(ge=-90, le=90)


class Observatory(_StrictModel):
    """A single observatory location definition, as declared in one `observatories/*.yaml` file.

    `latitudeDeg`/`longitudeDeg`/`elevationMeters` map directly onto
    astropy's `EarthLocation.from_geodetic(lon, lat, height)`, which is what
    consumers such as `visibility.compute_visibility`'s horizon check construct the observer
    frame from. Latitude/longitude bounds are validated because a value
    outside them is unambiguously a mistake (e.g. unconverted
    degrees/minutes/seconds), not a legitimate location.

    `horizonProfile` is optional and describes real obstructions around this site, beyond the
    geometric horizon — see `HorizonPoint`. When present, `visibility.compute_visibility`
    linearly interpolates between its points (wrapping around 0/360) to find the minimum
    observable altitude at any azimuth, and combines it with its own `min_altitude_deg`
    argument (whichever is higher wins) rather than one silently overriding the other. `None`
    (the default) means no obstruction data is available — the same as an all-zero flat
    horizon, but explicit about the difference between "flat" and "unknown".
    """

    id: str
    name: str
    latitudeDeg: float = Field(ge=-90, le=90)
    longitudeDeg: float = Field(ge=-180, le=180)
    elevationMeters: float = 0
    horizonProfile: list[HorizonPoint] | None = None

    @model_validator(mode="after")
    def _check_horizon_profile_azimuths_are_sorted_and_unique(self) -> "Observatory":
        if self.horizonProfile is None:
            return self
        azimuths = [point.azimuthDeg for point in self.horizonProfile]
        if azimuths != sorted(azimuths):
            raise ValueError("horizonProfile points must be sorted by ascending azimuthDeg")
        if len(azimuths) != len(set(azimuths)):
            raise ValueError("horizonProfile points must have unique azimuthDeg values")
        return self


class ObservatorySummary(TypedDict):
    """The id/name of a loaded observatory, without its full definition."""

    id: str
    name: str


class DraftLocationDeviceInfo(TypedDict):
    """One connected INDI device's live `GEOGRAPHIC_COORD` reading, as gathered by the caller
    for `draft_observatory`.

    `draft_observatory` itself never talks to INDI — the caller
    (`server.draft_observatory`) resolves property values via the messaging
    layer, so this module stays testable with plain data (the same split
    `rig_store.DraftDeviceInfo` uses for `draft_rig`).
    """

    name: str
    geographicCoord: dict[str, str] | None
    state: str | None


class ObservatoryDraft(TypedDict):
    """A pre-filled observatory location skeleton for the operator to review and save.

    Never a finalized, saved `Observatory`: `id`/`name` have no INDI
    equivalent and are left `None` for the operator to fill in, same as
    `draft_rig` leaves `apertureMm`/`focalLengthMm` for the operator.
    `notes` calls out anything that needs a second look before saving —
    most importantly a GPS fix that's missing, not yet valid, or reporting
    the common all-zero "no fix yet" default.
    """

    kind: str
    id: str | None
    name: str | None
    latitudeDeg: float | None
    longitudeDeg: float | None
    elevationMeters: float | None
    sourceDevice: str | None
    notes: list[str]


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


def draft_observatory(devices: Iterable[DraftLocationDeviceInfo]) -> ObservatoryDraft:
    """Pre-fill a draft observatory location from a connected device's live `GEOGRAPHIC_COORD`.

    INDI's `GEOGRAPHIC_COORD` standard property (LAT/LONG/ELEV) is exposed
    by GPS drivers and often by mount drivers too, so it's read the same way
    `rig_store.draft_rig` reads `CCD_INFO`/`FILTER_NAME` from whichever
    connected devices report it. `LONG` is converted from INDI's 0-360
    East-positive convention to this schema's -180..180
    (`Observatory.longitudeDeg`, astropy's `EarthLocation.from_geodetic`
    convention).

    This is always advisory, never authoritative — like `draft_rig`'s
    output, the result is a starting point for the operator to review and
    save themselves via `save_observatory`, never auto-selected. `id`/`name`
    have no INDI equivalent and are left `None`. `notes` flags anything that
    needs a second look: no connected device currently reports a fix, more
    than one disagrees (the first, in device order, is used), the reading is
    the common all-zero "no fix yet" default, or the property's vector
    `state` isn't `"Ok"` (still settling, or in error).
    """
    candidates: list[tuple[str, float, float, float]] = []
    notes: list[str] = []
    for device in devices:
        coord = device["geographicCoord"]
        if coord is None:
            continue
        lat = _parse_coord(coord.get("LAT"))
        lon = _parse_coord(coord.get("LONG"))
        elev = _parse_coord(coord.get("ELEV"))
        if lat is None or lon is None or elev is None:
            continue
        if lon > 180:
            lon -= 360
        candidates.append((device["name"], lat, lon, elev))
        if device["state"] not in (None, "Ok"):
            notes.append(
                f"{device['name']}'s GEOGRAPHIC_COORD state is {device['state']!r}, not "
                "'Ok' — the reading may not be a valid fix yet."
            )
        if lat == 0 and lon == 0 and elev == 0:
            notes.append(
                f"{device['name']}'s GEOGRAPHIC_COORD reads back as 0/0/0, the common "
                "default before a GPS fix — confirm this is a real location, not a missing fix."
            )

    if not candidates:
        notes.append(
            "No connected device currently reports GEOGRAPHIC_COORD; fill in latitudeDeg/"
            "longitudeDeg/elevationMeters by hand before saving."
        )
        return {
            "kind": "observatoryDraft",
            "id": None,
            "name": None,
            "latitudeDeg": None,
            "longitudeDeg": None,
            "elevationMeters": None,
            "sourceDevice": None,
            "notes": notes,
        }

    if len(candidates) > 1:
        notes.append(
            "More than one connected device reports GEOGRAPHIC_COORD "
            f"({', '.join(name for name, *_ in candidates)}); used {candidates[0][0]}. "
            "Confirm this is the right source before saving."
        )

    source, lat, lon, elev = candidates[0]
    return {
        "kind": "observatoryDraft",
        "id": None,
        "name": None,
        "latitudeDeg": lat,
        "longitudeDeg": lon,
        "elevationMeters": elev,
        "sourceDevice": source,
        "notes": notes,
    }


def _parse_coord(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_hzn_profile(text: str) -> list[HorizonPoint]:
    """Parse a `.hzn` horizon-profile file's content into a list of `HorizonPoint` values, for
    assigning to `Observatory.horizonProfile`.

    `.hzn` is the plain-CSV format used by Stellarium/N.I.N.A. and similar planetarium/
    acquisition tools: one `azimuthDeg,altitudeDeg` pair per line, conventionally (but not
    required by this parser) one line per integer azimuth degree 0..359. Blank lines (including
    a common trailing one) are skipped; line order is preserved.

    Raises `ValueError` naming the offending line number if a non-blank line isn't a parseable
    `azimuth,altitude` pair, or if a value is out of `HorizonPoint`'s valid range (azimuth
    0..<360, altitude -90..90) — the same validation `Observatory.horizonProfile` itself
    enforces, just attributed to a specific source line rather than the point's position in the
    list. Does not itself check that azimuths come out sorted/unique across the whole file —
    that's `Observatory.horizonProfile`'s own model validator's job, once this result is
    assigned to it.
    """
    points: list[HorizonPoint] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(",")
        if len(parts) != 2:
            raise ValueError(f"line {line_number}: expected 'azimuth,altitude', got {line!r}")
        try:
            azimuth_deg, altitude_deg = float(parts[0]), float(parts[1])
        except ValueError as exc:
            raise ValueError(
                f"line {line_number}: expected 'azimuth,altitude', got {line!r}"
            ) from exc
        try:
            points.append(HorizonPoint(azimuthDeg=azimuth_deg, altitudeDeg=altitude_deg))
        except ValidationError as exc:
            raise ValueError(f"line {line_number}: {exc}") from exc
    return points
