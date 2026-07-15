"""Loading and querying imaging rig definitions.

Rig definitions describe the physical imaging setup (mount, imaging train,
optional guiding train) that INDI itself has no protocol representation for.
They are YAML documents under a rigs directory (one file per rig, see
`docs/RigSchema.md`), not SQLite rows, since this is low-volume
human-curated configuration rather than write-heavy operational data. Like
the scripting layer, files are parsed with `yaml.safe_load` and validated
against a schema, since they may be authored on the Client Computer and
uploaded.
"""

import logging
import os
from pathlib import Path
from typing import TypedDict

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

logger = logging.getLogger(__name__)

__all__ = [
    "AuxiliaryDevice",
    "Camera",
    "Device",
    "Filterwheel",
    "Focuser",
    "ImagingTrain",
    "OffAxisGuider",
    "OpticalTrain",
    "Rig",
    "RigSummary",
    "TelescopeOptics",
    "get_rig",
    "list_rigs",
    "load_rigs",
]

RIGS_DIR_ENV = "INDI_MCP_RIGS_DIR"
_DEFAULT_RIGS_DIR = Path("rigs")


class _StrictModel(BaseModel):
    """Base for rig schema models: reject unknown fields from hand-edited/uploaded YAML."""

    model_config = ConfigDict(extra="forbid")


class TelescopeOptics(_StrictModel):
    """Aperture/focal length of one optical train. Not a driver, so no `device` field."""

    apertureMm: float
    focalLengthMm: float


class Device(_StrictModel):
    """A rig component that is just an INDI device name, with no extra config."""

    device: str


class Focuser(_StrictModel):
    device: str
    minPosition: int
    maxPosition: int


class Filterwheel(_StrictModel):
    device: str
    slots: dict[int, str] = {}


class Camera(_StrictModel):
    device: str
    cooled: bool = False
    pixelsX: int
    pixelsY: int
    pixelSizeMicron: float
    bitDepth: int


class OffAxisGuider(_StrictModel):
    """A guide camera picked off the imaging train's own light path via a prism.

    An alternative to a separate `guidingTrain`: rather than a second scope
    with its own optics, the guide camera shares the imaging train's
    telescope. Mutually exclusive with `guidingTrain` (see `Rig`).
    """

    camera: Camera


class OpticalTrain(_StrictModel):
    """One optical path from telescope to camera: the imaging train, or a separate guiding train.

    `focuser`/`filterWheel`/`rotator` are optional here because a guiding
    train usually only has a camera (most guide scopes are manual-focus,
    with no filter wheel or field rotator), but any of them can occur on
    either train.
    """

    telescope: TelescopeOptics
    camera: Camera
    focuser: Focuser | None = None
    filterWheel: Filterwheel | None = None
    rotator: Device | None = None


class ImagingTrain(OpticalTrain):
    """The main imaging train, which may also carry an off-axis guider."""

    offAxisGuider: OffAxisGuider | None = None


class AuxiliaryDevice(_StrictModel):
    """An additional INDI device that doesn't need config beyond its name and role.

    `role` is a free-form label (e.g. `"powerHub"`, `"observatoryControl"`,
    `"flatScreen"`, `"dewHeater"`) rather than a fixed enum, so a rig can
    declare a device type this schema doesn't have a dedicated field for yet
    without requiring a schema change. Roles that don't need per-role config
    (unlike e.g. `focuser`'s position range) fit here; give a component its
    own typed field instead once it needs more than a device name.
    """

    role: str
    device: str


class Rig(_StrictModel):
    """A single imaging rig definition, as declared in one `rigs/*.yaml` file."""

    id: str
    name: str
    mount: Device
    imagingTrain: ImagingTrain
    guidingTrain: OpticalTrain | None = None
    devices: list[AuxiliaryDevice] = []

    @model_validator(mode="after")
    def _check_guiding_method_is_unambiguous(self) -> "Rig":
        if self.guidingTrain is not None and self.imagingTrain.offAxisGuider is not None:
            raise ValueError(
                "a rig can't declare both `guidingTrain` and `imagingTrain.offAxisGuider` "
                "— guiding via a separate scope and via an off-axis guider are mutually exclusive"
            )
        return self


class RigSummary(TypedDict):
    """The id/name of a loaded rig, without its full definition."""

    id: str
    name: str


_rigs: dict[str, Rig] = {}


def _rigs_dir() -> Path:
    return Path(os.environ.get(RIGS_DIR_ENV, _DEFAULT_RIGS_DIR))


def load_rigs(directory: Path | None = None) -> list[Rig]:
    """Load every `*.yaml` rig definition from `directory` into memory.

    Defaults to `$INDI_MCP_RIGS_DIR`, falling back to `./rigs`. Files that
    fail to parse or don't match the rig schema are logged and skipped
    rather than aborting the whole load, since rig YAML may be hand-edited
    or uploaded by a client. A duplicate `id` across files keeps whichever
    file was loaded first (files are loaded in sorted filename order).
    """
    global _rigs
    directory = directory if directory is not None else _rigs_dir()
    rigs: dict[str, Rig] = {}
    if not directory.is_dir():
        logger.info("Rigs directory does not exist, no rigs loaded: %s", directory)
        _rigs = rigs
        return []
    for path in sorted(directory.glob("*.yaml")):
        try:
            raw = yaml.safe_load(path.read_text())
            rig = Rig.model_validate(raw)
        except (yaml.YAMLError, ValidationError) as exc:
            logger.warning("Skipping invalid rig file %s: %s", path, exc)
            continue
        if rig.id in rigs:
            logger.warning("Duplicate rig id %r in %s, keeping first definition", rig.id, path)
            continue
        rigs[rig.id] = rig
    _rigs = rigs
    logger.info("Loaded %d rig(s) from %s", len(rigs), directory)
    return list(rigs.values())


def list_rigs() -> list[RigSummary]:
    """List the id/name of every currently loaded rig."""
    return [{"id": rig.id, "name": rig.name} for rig in _rigs.values()]


def get_rig(rig_id: str) -> Rig:
    """Return the full definition of the rig identified by `rig_id`."""
    rig = _rigs.get(rig_id)
    if rig is None:
        raise ValueError(f"Unknown rig: {rig_id!r}")
    return rig
