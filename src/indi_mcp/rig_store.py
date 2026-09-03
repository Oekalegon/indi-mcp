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
optical tube assemblies, mounts, and observatories. Real setups can swap
whole OTAs between mounts, so a faithful model of those relationships would
need separate stores for OTAs, mounts, and observatories, cross-referencing
each other. That is deferred as unnecessary complexity for now; a flat list
per rig is enough to declare "this is what's mounted this session" and to
cross-check it against connected INDI devices (see
`suggest_rig`/`check_rig`).

Within that flat list, a component's optional `trainId` groups it with the
other components mounted on the same imaging train (e.g. filter wheel,
camera, and rotator sharing one OTA) — see `Component.trainId`.
"""

import logging
import os
import threading
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
    "filter_slots",
    "get_rig",
    "list_rigs",
    "load_rigs",
    "save_rig",
    "suggest_rig",
    "update_component_slots",
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

_MULTI_INSTANCE_TRAIN_ROLES = frozenset(
    {"powerHub", "observatoryControl", "flatScreen", "dewHeater"}
)
"""Known roles exempt from the one-per-train check (see `_SINGLE_INSTANCE_TRAIN_ROLES`).

These commonly have more than one instance on the same physical train (e.g.
two independently-controlled dew heater channels on one OTA).
"""

_SINGLE_INSTANCE_TRAIN_ROLES = frozenset(KNOWN_ROLES) - _MULTI_INSTANCE_TRAIN_ROLES
"""Roles a train may have at most one of (see `Rig._check_train_roles_are_unique`).

Derived from `KNOWN_ROLES` minus `_MULTI_INSTANCE_TRAIN_ROLES` so a newly
added known role defaults to "singular per train" unless explicitly
exempted, rather than the two lists silently drifting apart. Any role this
schema has no dedicated name for (the `Role` Literal's `| str` escape
hatch) is also exempt, since it can't appear in `KNOWN_ROLES` at all.
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

    `trainId` is an optional, arbitrary tag (e.g. `"ota1"`) grouping
    components that move together as one imaging train — a filter wheel,
    camera, and rotator on the same OTA, distinct from a second OTA's
    focuser mounted on the same rig. It carries no ordering; it just says
    "these belong together". A train may have at most one component of a
    given role for roles where that's the natural expectation (e.g. one
    `"camera"`, one `"focuser"`) — see `_SINGLE_INSTANCE_TRAIN_ROLES` and
    `Rig._check_train_roles_are_unique` — but roles that commonly repeat
    (e.g. `"dewHeater"`) are exempt and may appear any number of times in
    the same train. Components without a `trainId` aren't part of any
    train — accessory roles like `"powerHub"` typically stay ungrouped.
    """

    role: Role
    id: str
    trainId: str | None = None
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

    @model_validator(mode="after")
    def _check_train_roles_are_unique(self) -> "Rig":
        roles_by_train: dict[str, set[Role]] = {}
        for component in self.components:
            if component.trainId is None or component.role not in _SINGLE_INSTANCE_TRAIN_ROLES:
                continue
            roles = roles_by_train.setdefault(component.trainId, set())
            if component.role in roles:
                raise ValueError(
                    f"train {component.trainId!r} has more than one component with "
                    f"role {component.role!r}"
                )
            roles.add(component.role)
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
_rigs_lock = threading.RLock()
"""Guards every read or write of `_rigs`, and serializes `save_rig`'s write-then-reload.

`save_rig` runs off the asyncio event loop via `asyncio.to_thread`
(`server.save_rig`), so two saves triggered close together genuinely run in
different OS threads rather than merely interleaved coroutines. Without this
lock, two concurrent calls each do their own full directory glob+parse and
then wholesale-replace the shared `_rigs` dict (`load_rigs`); whichever
reload happens to finish last wins and silently discards the other call's
just-written rig from memory even though its file is on disk, and that
call's own trailing `get_rig` can then raise `Unknown rig` despite having
successfully written it.

Deliberately a single lock rather than one per `directory`: `_rigs` is one
shared, directory-agnostic cache (whatever was most recently loaded), not a
per-directory store, so splitting the lock by `directory` would let saves to
two different directories race on that same shared `_rigs` dict again —
reintroducing the exact bug this lock exists to fix. In production there is
only ever one rigs directory; `directory` is a parameter mainly so tests can
isolate `tmp_path` fixtures from each other, not a sign of real per-directory
independent state.

`RLock` rather than `Lock` because `save_rig` calls `get_rig` (and
`load_rigs`) while already holding the lock; a plain `Lock` would deadlock on
that reentrant acquisition.
"""


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
        with _rigs_lock:
            _rigs = rigs
        return []
    for path in sorted(directory.glob("*.yaml")):
        if not path.is_file():
            logger.warning("Skipping non-file rig path %s", path)
            continue
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
    with _rigs_lock:
        _rigs = rigs
    logger.info("Loaded %d rig(s) from %s", len(rigs), directory)
    return list(rigs.values())


def list_rigs() -> list[RigSummary]:
    """List the id/name of every currently loaded rig."""
    with _rigs_lock:
        return [{"id": rig.id, "name": rig.name} for rig in _rigs.values()]


def get_rig(rig_id: str) -> Rig:
    """Return the full definition of the rig identified by `rig_id`."""
    with _rigs_lock:
        rig = _rigs.get(rig_id)
        if rig is None:
            raise ValueError(f"Unknown rig: {rig_id!r}")
        return rig


def save_rig(rig: Rig, *, overwrite: bool = False, directory: Path | None = None) -> Rig:
    """Write `rig` to `<directory>/<rig.id>.yaml` and reload it into memory.

    `rig` is already a validated `Rig` (pydantic validation happens when
    it's constructed, whether hand-assembled from a `draft_rig` result or
    supplied directly by the caller), so this only needs to worry about the
    filesystem: it refuses to replace an existing `<rig.id>.yaml` unless
    `overwrite` is set, since reusing an `id` could otherwise silently
    destroy a previously saved rig with no warning — this is the operator's
    explicit save action, never something the server does on its own (see
    "No silent auto-selection" in `docs/RigSchema.md`). The existence check
    and the write happen as one atomic file-open (exclusive-create unless
    `overwrite`), so two concurrent saves of the same new `id` can't both
    slip past the check. Reloads every rig in `directory` afterwards (see
    `load_rigs`) so the saved rig is immediately available by `id` to
    `get_rig`/`suggest_rig`/`check_rig`; the write and that reload are
    serialized against other `save_rig` calls (and against readers) by
    `_rigs_lock`, since two unlocked concurrent reloads could otherwise race
    (see `_rigs_lock`).
    """
    if not rig.id or rig.id in (".", "..") or "/" in rig.id or "\\" in rig.id:
        raise ValueError(f"Invalid rig id for a filename: {rig.id!r}")
    directory = directory if directory is not None else _rigs_dir()
    with _rigs_lock:
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except NotADirectoryError as exc:
            raise ValueError(f"Cannot create rigs directory {directory}: {exc}") from exc
        path = directory / f"{rig.id}.yaml"
        if path.is_dir():
            raise ValueError(f"Cannot save rig {rig.id!r}: {path} is a directory, not a file")
        content = yaml.safe_dump(rig.model_dump(exclude_none=True), sort_keys=False)
        try:
            with path.open("w" if overwrite else "x", encoding="utf-8") as f:
                f.write(content)
        except FileExistsError as exc:
            raise ValueError(
                f"A rig file already exists for id {rig.id!r} ({path}); "
                "pass overwrite=True to replace it."
            ) from exc
        logger.info("Saved rig %r to %s", rig.id, path)
        load_rigs(directory)
        return get_rig(rig.id)


def update_component_slots(rig_id: str, role: str, slots: dict[int, str]) -> Rig:
    """Persist `slots` onto rig `rig_id`'s `role` component and reload it (INDIMCP-64).

    Used both when `select_filter` auto-adopts the driver's `FILTER_NAME` onto a filter wheel
    component that has no `slots` configured at all yet
    (`script_engine._reconcile_filter_config_with_driver`), and when
    `script_engine.adopt_filter_names_from_driver` deliberately overwrites an *existing*
    `slots` map instead — in both cases so the rig's YAML file (not just the in-memory `Rig`)
    is updated, since it's the durable source of truth (`docs/RigSchema.md`). `overwrite=True`
    here is an update to an existing, already-owned rig file (adding/replacing data on one of
    its own components), not the "reusing an id could silently destroy someone else's rig"
    case `save_rig`'s `overwrite` guard exists to prevent.

    Raises `ValueError` if `role` doesn't resolve to exactly one component — a rig's `role` is
    explicitly allowed to be shared by more than one component (`Component.role`'s own
    docstring), so silently updating every matching component (or an arbitrary one) could
    apply one physical device's filter names to a rig entry that actually describes a
    different device.
    """
    rig = get_rig(rig_id)
    matches = [component for component in rig.components if component.role == role]
    if len(matches) != 1:
        raise ValueError(
            f"rig {rig_id!r} has {len(matches)} component(s) for role {role!r}; "
            "expected exactly one"
        )
    updated_components = [
        component.model_copy(update={"slots": slots}) if component.role == role else component
        for component in rig.components
    ]
    return save_rig(rig.model_copy(update={"components": updated_components}), overwrite=True)


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
    with _rigs_lock:
        rigs = list(_rigs.values())
    for rig in rigs:
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
        slots = filter_slots(device["filterNames"])
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


def filter_slots(filter_names: dict[str, str] | None) -> dict[int, str]:
    """Parse a `FILTER_NAME` property's `FILTER_SLOT_NAME_<n>` members into `{slot: name}`.

    Public (INDIMCP-64): `script_engine._reconcile_filter_config_with_driver` and
    `script_engine.sync_filter_names` both call this too, to parse a filter wheel driver's own
    live `FILTER_NAME` into the exact same shape as a rig component's configured `slots` map,
    so the two can be compared directly.
    """
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
