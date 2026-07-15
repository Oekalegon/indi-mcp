"""Loading and querying imaging rig definitions.

Rig definitions describe the physical imaging setup (mount, telescope optics,
focuser, filter wheel, imaging/guide cameras) that INDI itself has no
protocol representation for. They are YAML documents under a rigs directory
(one file per rig, see `docs/RigSchema.md`), not SQLite rows, since this is
low-volume human-curated configuration rather than write-heavy operational
data. Like the scripting layer, files are parsed with `yaml.safe_load` and
validated against a schema, since they may be authored on the Client
Computer and uploaded.
"""

import logging
import os
from pathlib import Path
from typing import TypedDict

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

logger = logging.getLogger(__name__)

__all__ = [
    "Camera",
    "CameraTrains",
    "Device",
    "Filterwheel",
    "Focuser",
    "Rig",
    "RigSummary",
    "Telescope",
    "TelescopeTrain",
    "get_rig",
    "list_rigs",
    "load_rigs",
]

RIGS_DIR_ENV = "INDI_MCP_RIGS_DIR"
_DEFAULT_RIGS_DIR = Path("rigs")


class _StrictModel(BaseModel):
    """Base for rig schema models: reject unknown fields from hand-edited/uploaded YAML."""

    model_config = ConfigDict(extra="forbid")


class TelescopeTrain(_StrictModel):
    apertureMm: float
    focalLengthMm: float


class Telescope(_StrictModel):
    imaging: TelescopeTrain
    guiding: TelescopeTrain | None = None


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


class CameraTrains(_StrictModel):
    imaging: Camera
    guiding: Camera | None = None


class Rig(_StrictModel):
    """A single imaging rig definition, as declared in one `rigs/*.yaml` file."""

    id: str
    name: str
    mount: Device
    telescope: Telescope
    focuser: Focuser
    filterWheel: Filterwheel
    camera: CameraTrains
    rotator: Device | None = None
    powerHub: Device | None = None
    observatoryControl: Device | None = None
    flatScreen: Device | None = None
    dewHeaters: list[Device] = []


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
