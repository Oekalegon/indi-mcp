"""Loading and querying imaging rig definitions.

Rig definitions describe the physical imaging setup — mount, telescope(s),
camera(s), focuser, filter wheel, rotator, and other equipment — that INDI
itself has no protocol representation for. They are YAML documents under a
rigs directory (one file per rig, see `docs/RigSchema.md`), not SQLite rows,
since this is low-volume human-curated configuration rather than
write-heavy operational data. Like the scripting layer, files are parsed
with `yaml.safe_load` and validated against a schema, since they may be
authored on the Client Computer and uploaded.

A rig is a flat list of components rather than a nested structure of
imaging/guiding trains and optical tube assemblies. Real setups can swap
imaging trains between telescopes and telescopes between mounts, so a
faithful model of those relationships would need separate stores for
trains, OTAs, mounts, and observatories, cross-referencing each other. That
is deferred as unnecessary complexity for now; a flat list per rig is
enough to declare "this is what's mounted this session" and to cross-check
it against connected INDI devices (see `suggest_rig`/`check_rig`).
"""

import logging
import os
from pathlib import Path
from typing import Literal, TypedDict

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

logger = logging.getLogger(__name__)

__all__ = [
    "Component",
    "KNOWN_ROLES",
    "Rig",
    "RigSummary",
    "Role",
    "get_rig",
    "list_rigs",
    "load_rigs",
]

RIGS_DIR_ENV = "INDI_MCP_RIGS_DIR"
_DEFAULT_RIGS_DIR = Path("rigs")

KNOWN_ROLES = (
    "mount",
    "telescope",
    "guideTelescope",
    "camera",
    "guideCamera",
    "focuser",
    "filterWheel",
    "rotator",
    "powerHub",
    "observatoryControl",
    "flatScreen",
    "dewHeater",
)
"""Roles this schema's authors have thought of. Kept in sync with `Role` below."""

Role = (
    Literal[
        "mount",
        "telescope",
        "guideTelescope",
        "camera",
        "guideCamera",
        "focuser",
        "filterWheel",
        "rotator",
        "powerHub",
        "observatoryControl",
        "flatScreen",
        "dewHeater",
    ]
    | str
)
"""A component's role: one of `KNOWN_ROLES`, or any other string.

Validating against the `Literal` first gives known roles IDE
autocomplete/typo protection, while the trailing `| str` still accepts a
role this schema has no dedicated name for, so a new component type never
requires a schema change.
"""


class _StrictModel(BaseModel):
    """Base for rig schema models: reject unknown fields from hand-edited/uploaded YAML."""

    model_config = ConfigDict(extra="forbid")


class Component(_StrictModel):
    """One piece of rig equipment.

    All fields besides `role` are optional since which ones are meaningful
    depends on the role: a `"telescope"` has `apertureMm`/`focalLengthMm`
    but no `device` (it isn't a driver); a `"camera"` has `device` plus
    pixel geometry; a `"powerHub"` has just `device`.

    `make`/`model` identify the product (e.g. `"ZWO"`/`"ASI2600MM Pro"`),
    useful once rigs are cross-referenced against a device library rather
    than each repeating full specs. `id` identifies the specific physical
    unit — a serial number, or any label the operator chooses — needed once
    a rig has two components of the same make/model (e.g. two of the same
    camera model) and something downstream needs to tell them apart, such
    as picking the matching master dark for a given camera's frames.
    """

    role: Role
    make: str | None = None
    model: str | None = None
    id: str | None = None
    device: str | None = None
    apertureMm: float | None = None
    focalLengthMm: float | None = None
    cooled: bool | None = None
    pixelsX: int | None = None
    pixelsY: int | None = None
    pixelSizeMicron: float | None = None
    bitDepth: int | None = None
    minPosition: int | None = None
    maxPosition: int | None = None
    slots: dict[int, str] | None = None


class Rig(_StrictModel):
    """A single imaging rig definition, as declared in one `rigs/*.yaml` file."""

    id: str
    name: str
    components: list[Component]


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
