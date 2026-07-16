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
from collections.abc import Iterable
from pathlib import Path
from typing import Literal, TypedDict

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

logger = logging.getLogger(__name__)

__all__ = [
    "Component",
    "DraftDeviceInfo",
    "KNOWN_ROLES",
    "Rig",
    "RigCheck",
    "RigDraft",
    "RigSuggestion",
    "RigSummary",
    "Role",
    "check_rig",
    "draft_rig",
    "get_rig",
    "list_rigs",
    "load_rigs",
    "suggest_rig",
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

    `role` and `id` are the only required fields; the rest are optional
    since which ones are meaningful depends on the role: a `"telescope"`
    has `apertureMm`/`focalLengthMm` but no `device` (it isn't a driver);
    a `"camera"` has `device` plus pixel geometry; a `"powerHub"` has just
    `device`.

    `id` is a stable handle for this specific component within the rig — a
    serial number, or any label the operator chooses — required (and unique
    within the rig, see `Rig`) rather than left to `role` alone, since a rig
    commonly has more than one component sharing a role (e.g. two identical
    guide cameras, or several dew heater channels) and something downstream
    needs a way to tell them apart — e.g. picking the matching master dark
    for a given camera's frames. `make`/`model` identify the product (e.g.
    `"ZWO"`/`"ASI2600MM Pro"`), useful once rigs are cross-referenced
    against a device library rather than each repeating full specs.
    """

    role: Role
    id: str
    make: str | None = None
    model: str | None = None
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

    @model_validator(mode="after")
    def _check_component_ids_are_unique(self) -> "Rig":
        seen: set[str] = set()
        for component in self.components:
            if component.id in seen:
                raise ValueError(f"duplicate component id {component.id!r} within this rig")
            seen.add(component.id)
        return self


class RigSummary(TypedDict):
    """The id/name of a loaded rig, without its full definition."""

    id: str
    name: str


class RigSuggestion(TypedDict):
    """How well a configured rig matches the currently connected INDI devices."""

    kind: str
    rigId: str
    rigName: str
    score: float | None
    matched: list[str]
    missing: list[str]


class RigCheck(TypedDict):
    """Whether a specific rig's devices are currently connected."""

    kind: str
    rigId: str
    ok: bool
    present: list[str]
    missing: list[str]


class DraftDeviceInfo(TypedDict):
    """One connected INDI device, as gathered by the caller for `draft_rig`.

    `draft_rig` itself never talks to INDI or the driver catalog — the
    caller (`server.draft_rig`) resolves `family` via the driver catalog
    and the property values via the messaging layer, so this module stays
    testable with plain data (see `suggest_rig`/`check_rig`).
    """

    name: str
    family: str | None
    ccdInfo: dict[str, str] | None
    filterNames: dict[str, str] | None
    focusRange: tuple[float, float] | None


class RigDraft(TypedDict):
    """A pre-filled rig skeleton for the operator to complete and save.

    Never a finalized, saved `Rig`: `notes` calls out anything `draft_rig`
    could not fill in with confidence — fields INDI has no way to supply,
    or an ambiguous role assignment — that the operator must resolve
    before saving it as a real rig.
    """

    kind: str
    components: list[Component]
    notes: list[str]


_FAMILY_TO_ROLE: dict[str, Role] = {
    "CCDs": "camera",
    "Filter Wheels": "filterWheel",
    "Focusers": "focuser",
    "Telescopes": "mount",
}
"""Driver catalog family names (`DeviceDriver.family`) recognized by `draft_rig`."""

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


def suggest_rig(connected_devices: Iterable[str]) -> list[RigSuggestion]:
    """Propose which loaded rig is likely mounted, by matching connected INDI device names.

    Cross-checks each rig's component `device` fields against
    `connected_devices` (from the INDI messaging layer) and scores how many
    match. This never selects a rig for use — it only proposes candidates,
    sorted best match first, for the operator or client to choose from (see
    "No silent auto-selection" in `docs/RigSchema.md`). Components without a
    `device` field (e.g. `telescope`, `guideTelescope`) have nothing
    INDI-visible to check them against, so they're excluded from the score.
    A rig with no device-bearing components at all has nothing to check
    against, so its `score` is `None` rather than `0.0` — a rig that's been
    checked and found to have nothing connected is a different situation
    from a rig where there was nothing to check.
    """
    connected = set(connected_devices)
    suggestions: list[RigSuggestion] = []
    for rig in _rigs.values():
        matched, missing = _match_devices(rig, connected)
        total = len(matched) + len(missing)
        score = len(matched) / total if total else None
        suggestions.append(
            {
                "kind": "rigSuggestion",
                "rigId": rig.id,
                "rigName": rig.name,
                "score": score,
                "matched": matched,
                "missing": missing,
            }
        )
    suggestions.sort(key=lambda suggestion: _sort_key(suggestion["score"]), reverse=True)
    return suggestions


def check_rig(rig_id: str, connected_devices: Iterable[str]) -> RigCheck:
    """Warn (rather than fail) about a specific rig's devices that aren't currently connected.

    Unlike `suggest_rig`, which scores every loaded rig to help pick one,
    this checks a single already-selected rig's component `device` fields
    against `connected_devices` and reports which are `present`/`missing`
    (see `_match_devices`). It never raises on a missing device: a rig
    might be intentionally used without one of its devices (e.g. imaging
    without a guide camera), and anything that actually needs the missing
    device will fail naturally when it tries to use it.
    """
    rig = get_rig(rig_id)
    present, missing = _match_devices(rig, set(connected_devices))
    return {
        "kind": "rigCheck",
        "rigId": rig.id,
        "ok": not missing,
        "present": present,
        "missing": missing,
    }


def draft_rig(devices: Iterable[DraftDeviceInfo]) -> RigDraft:
    """Pre-fill a draft rig skeleton from connected devices' families and live properties.

    Each `"CCDs"` device becomes a `camera` component (`guideCamera` for all
    of them if more than one camera is connected, since which one is the
    imaging camera isn't something INDI can tell us); `"Filter Wheels"`,
    `"Focusers"`, and `"Telescopes"` devices become `filterWheel`,
    `focuser`, and `mount` components respectively, with whatever `ccdInfo`
    /`filterNames`/`focusRange` data is available filled in. Every drafted
    component's `id` is its device name — a stable, unique placeholder the
    operator is free to rename. Other roles (`telescope`, `powerHub`, ...)
    have no INDI driver family to detect them by, so they're never drafted.

    This never produces a finalized rig, only a starting point: `notes`
    calls out fields it could not fill in (see "Assisting rig creation from
    connected devices" in `docs/Design.md`) for the operator to complete
    and save themselves (see "No silent auto-selection" in
    `docs/RigSchema.md`).
    """
    devices = list(devices)
    cameras = [device for device in devices if device["family"] == "CCDs"]
    single_camera = len(cameras) == 1

    components: list[Component] = []
    notes: list[str] = []
    for device in devices:
        role = _FAMILY_TO_ROLE.get(device["family"])
        if role is None:
            continue
        if role == "camera" and not single_camera:
            role = "guideCamera"
        components.append(_draft_component(role, device))

    if len(cameras) > 1:
        notes.append(
            "More than one camera detected; all were drafted as guideCamera "
            "since which one does the imaging isn't visible to INDI. Change "
            "the imaging camera's role to camera."
        )
    if any(component.role in ("camera", "guideCamera") for component in components):
        notes.append(
            "apertureMm/focalLengthMm have no INDI equivalent; add telescope/"
            "guideTelescope components with those fields before saving."
        )

    return {"kind": "rigDraft", "components": components, "notes": notes}


def _draft_component(role: Role, device: DraftDeviceInfo) -> Component:
    """Build one draft `Component` for `device`, filling in whatever `role`-specific data it has."""
    if role in ("camera", "guideCamera"):
        pixels_x, pixels_y, pixel_size, bit_depth = _ccd_info_fields(device["ccdInfo"])
        return Component(
            role=role,
            id=device["name"],
            device=device["name"],
            pixelsX=pixels_x,
            pixelsY=pixels_y,
            pixelSizeMicron=pixel_size,
            bitDepth=bit_depth,
        )
    if role == "filterWheel":
        slots = _filter_slots(device["filterNames"])
        return Component(role=role, id=device["name"], device=device["name"], slots=slots or None)
    if role == "focuser" and device["focusRange"] is not None:
        min_position, max_position = device["focusRange"]
        return Component(
            role=role,
            id=device["name"],
            device=device["name"],
            minPosition=int(min_position),
            maxPosition=int(max_position),
        )
    return Component(role=role, id=device["name"], device=device["name"])


def _ccd_info_fields(
    ccd_info: dict[str, str] | None,
) -> tuple[int | None, int | None, float | None, int | None]:
    """Map a `CCD_INFO` property's members to (pixelsX, pixelsY, pixelSizeMicron, bitDepth)."""
    if not ccd_info:
        return None, None, None, None
    pixels_x = _parse_number(ccd_info.get("CCD_MAX_X"))
    pixels_y = _parse_number(ccd_info.get("CCD_MAX_Y"))
    pixel_size = _parse_number(ccd_info.get("CCD_PIXEL_SIZE"))
    bit_depth = _parse_number(ccd_info.get("CCD_BITSPERPIXEL"))
    return (
        int(pixels_x) if pixels_x is not None else None,
        int(pixels_y) if pixels_y is not None else None,
        pixel_size,
        int(bit_depth) if bit_depth is not None else None,
    )


def _filter_slots(filter_names: dict[str, str] | None) -> dict[int, str]:
    """Parse a `FILTER_NAME` property's `FILTER_SLOT_NAME_<n>` members into `{slot: name}`."""
    if not filter_names:
        return {}
    slots: dict[int, str] = {}
    for member_name, value in filter_names.items():
        prefix = "FILTER_SLOT_NAME_"
        if not member_name.startswith(prefix):
            continue
        try:
            slot = int(member_name[len(prefix) :])
        except ValueError:
            continue
        slots[slot] = value
    return slots


def _parse_number(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _match_devices(rig: Rig, connected: set[str]) -> tuple[list[str], list[str]]:
    """Split `rig`'s device-bearing components into (present, missing) component ids.

    Components without a `device` field (e.g. `telescope`, `guideTelescope`)
    have nothing INDI-visible to check them against, so they're excluded
    from both lists.
    """
    present = []
    missing = []
    for component in rig.components:
        if component.device is None:
            continue
        if component.device in connected:
            present.append(component.id)
        else:
            missing.append(component.id)
    return present, missing


def _sort_key(score: float | None) -> float:
    """Sort `None` (nothing to check) after every real score, including 0.0."""
    return score if score is not None else -1.0
