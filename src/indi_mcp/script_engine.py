"""Executing a loaded `script_store.Script` against a resolved rig.

This is the internal "given a script, a rig, and parameters, run it" engine
(INDIMCP-7) — it sits below the MCP-facing layer. `run_script`/
`get_script_status`/`cancel_script`/etc. as `@mcp.tool()`s, `runId`
bookkeeping, and the `indi://scripts` event stream are INDIMCP-13/14,
separate tickets that wrap `execute_script` below.

One thing is deliberately incomplete here, noted inline where it matters:
`slew` is implemented for a `raDec` target (INDIMCP-38); its `objectName`
target still raises `ScriptExecutionError` pending astropy-based name
resolution (INDIMCP-29).

Pause/cancel are supported as plain hooks (`asyncio.Event`s) an eventual
caller passes in — this engine has no `runId`/task-tracking concept of its
own; that's INDIMCP-13's job. `run_id`, however, *is* threaded through (as
a plain optional string, not a task-tracking concept) purely so
`capture_frame` can tag the frames it saves with the run that produced
them — see `execute_script`'s `run_id` parameter.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TypedDict

from indi_mcp import (
    fits_headers,
    frame_store,
    indi_messaging,
    observatory_store,
    rig_store,
    script_store,
)
from indi_mcp.observatory_store import Observatory
from indi_mcp.script_store import (
    CaptureFrameStep,
    Condition,
    ConditionOperator,
    CoolCameraStep,
    IfStep,
    RepeatStep,
    RunScriptStep,
    Script,
    SelectFilterStep,
    SetFocusPositionStep,
    SetPropertyStep,
    SlewStep,
    Step,
    WaitForStep,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ScriptCancelled",
    "ScriptExecutionError",
    "ScriptPreconditionError",
    "ScriptProgress",
    "ScriptResult",
    "ScriptValidationError",
    "execute_script",
]

_WAIT_POLL_INTERVAL_SECONDS = 0.2
_PAUSE_POLL_INTERVAL_SECONDS = 0.1
_SLEW_TIMEOUT_SECONDS = 120.0
"""How long a `slew` step waits for the mount's `EQUATORIAL_EOD_COORD` to reach `Ok`.

Not a schema field (`docs/ScriptSchema.md`'s `slew` step has no
`timeoutSeconds` of its own, unlike `wait_for`) — a slew's duration
depends on the mount and how far it's moving, not something a script
author tunes per call, so this is a generous fixed engine default rather
than something exposed in the YAML.
"""

_CAPTURE_READOUT_BUFFER_SECONDS = 30.0
"""Extra time `capture_frame` allows, beyond the exposure length itself, for `CCD_EXPOSURE`
to reach `Ok` and for the resulting BLOB to actually arrive on `_CCD_BLOB_VECTOR`.

Covers sensor readout and image download/transfer time, which varies by
camera/driver and isn't something a script author tunes per capture (same
reasoning as `_SLEW_TIMEOUT_SECONDS` not being a schema field).
"""

_CCD_BLOB_VECTOR = "CCD1"
"""The INDI BLOB vector name a camera driver publishes captured image data on.

Standard INDI CCD convention, same as `CCD_EXPOSURE`/`CCD_FRAME_TYPE`/
`CCD_BINNING` below — not configurable per rig component today (every
camera driver this project has been tested against uses it), hardcoded
here the same way `slew` hardcodes `EQUATORIAL_EOD_COORD`.
"""

_FRAME_TYPE_ELEMENTS = {
    "Light": "FRAME_LIGHT",
    "Dark": "FRAME_DARK",
    "Flat": "FRAME_FLAT",
    "Bias": "FRAME_BIAS",
}
"""`CaptureFrameStep.frameType` value -> `CCD_FRAME_TYPE` switch element name (standard INDI)."""

_FILTER_RELEVANT_FRAME_TYPES = frozenset({"Light", "Flat"})
"""`frameType` values for which `FILTER` is written to the FITS header (INDIMCP-60).

A Flat is taken *through* a specific filter, same as a Light — the whole point of a Flat is
to calibrate that filter's illumination/vignetting pattern, so its filter matters just as
much. A Dark/Bias is filter-independent (taken with the sensor read out the same way
regardless of what's in the optical path, typically capped) — recording a filter name on one
would be misleading, implying a dependency that doesn't exist.
"""


class ScriptValidationError(Exception):
    """Raised before execution starts: the script/rig/parameters themselves are invalid.

    A role with no matching component, an unknown script/rig id, a
    missing required parameter — problems inherent to *this* (script,
    rig, parameters) combination that no amount of waiting or connecting
    hardware would fix; the script would need to change, or a different
    rig/id supplied.
    """


class ScriptPreconditionError(Exception):
    """Raised before (or at the very start of) a step: the script is valid, but the physical
    rig isn't currently in a state this run requires — a device isn't connected, a mount is
    parked, etc.

    Distinct from `ScriptValidationError` (nothing about the script/rig/
    parameters is wrong) and `ScriptExecutionError` (no step has actually
    failed while running) — this is specifically "try again once the
    hardware is ready," not "fix the script" or "something went wrong
    mid-step."
    """


class ScriptExecutionError(Exception):
    """Raised when a step actively fails while running (`wait_for` timeout, `maxIterations`
    exceeded, ...) — the script and the rig were both fine to start; something about carrying
    out a specific step's own work didn't succeed.
    """


class ScriptCancelled(Exception):
    """Raised when `cancel_event` is set while a script run is in progress."""


class ScriptProgress(TypedDict):
    """Reported via `on_progress` before each step executes.

    `totalSteps` is `None` whenever it can't be known exactly rather than a
    number that only looks exact — see `_count_total_steps`. `message` is
    the step's own `description` verbatim, so it's honestly `None` when the
    script author didn't write one; the engine doesn't synthesize a
    fallback (e.g. the step's class name) into what's meant to be
    human-authored text — a caller wanting a fallback supplies its own.
    """

    scriptId: str
    stepsExecuted: int
    totalSteps: int | None
    message: str | None


class ScriptResult(TypedDict):
    """The outcome of a completed `execute_script` call.

    `framesCaptured` counts every `capture_frame` step that completed
    during this run (including inside nested `run_script` calls and
    `repeat` iterations, matching `stepsExecuted`'s own whole-run scope) —
    not a full `frames` list of per-frame metadata (`docs/Design.md`'s
    illustrative `scriptCompleted` example shows one): a caller wanting
    that can already query `frame_store.list_frames(run_id=...)` once
    `run_id` is threaded through (see `execute_script`), without this
    result needing to duplicate that same data.
    """

    scriptId: str
    stepsExecuted: int
    framesCaptured: int


@dataclass
class _ExecutionContext:
    """State shared, unchanged, across an entire run — including into nested `run_script` calls.

    `scripts` is a snapshot of every script reachable from the top-level
    script via `run_script`, taken once at the start of the run — nested
    `run_script` steps resolve their callee from this dict, never by
    calling back into the live `script_store` module state. Without this,
    a `run_script` step executing minutes into a long sequence could
    resolve against a script library that's since been reloaded (e.g. a
    concurrent `load_scripts()`/future `save_script()` call), running a
    different version of a sub-script than the one that was validated
    (role resolution, `totalSteps`, cycle/argument checks) at the start of
    this same run — or finding it gone entirely.

    `role_to_slots` is each resolved role's rig-component `slots` map
    (empty if it has none) — only consulted by `select_filter`'s
    `filterName` resolution (`_resolve_filter_slot`); every other step
    ignores it.

    `role_to_focus_range` is each resolved role's rig-component
    `(minPosition, maxPosition)` pair, absent entirely for a role whose
    component doesn't declare one of them — only consulted by
    `set_focus_position`'s range check (`_check_focus_position_in_range`);
    every other step ignores it.

    `role_to_component` is every strictly-resolved role's full rig `Component` (the same
    resolution `role_to_device`/`role_to_slots`/`role_to_focus_range` are each derived a
    slice of) — only consulted by `capture_frame`'s FITS header enrichment, for data a step's
    own resolved role already carries that none of those narrower views expose (e.g. the
    camera's `pixelSizeMicron`, for `SCALE`).

    `observatory`/`optional_role_components` are also only consulted by `capture_frame`'s FITS
    header enrichment (`_add_fits_header_fields`, INDIMCP-60). `observatory` is `None`
    whenever the run wasn't given a `location_id`. `optional_role_components` holds
    `"mount"`/`"filterWheel"`/`"telescope"`/`"focuser"`, each resolved once here best-effort
    (see `_resolve_optional_role_component`) — absent from the dict entirely if the rig
    doesn't declare that role at all — rather than via `role_to_device`/`role_to_component`,
    since a script whose only step is `capture_frame` never otherwise needs any of them
    resolved: without this, telescope-position/filter-name/focuser/telescope-optics headers
    would only ever be available to scripts that happen to also reference those roles for
    some other reason (e.g. a `slew`/`select_filter`/`set_focus_position` step earlier in the
    same run). `"telescope"` in particular has no `device` of its own at all (it isn't a
    driver — see `docs/RigSchema.md`), so it could never appear in `role_to_device` regardless.
    """

    role_to_device: dict[str, str]
    cancel_event: asyncio.Event | None
    pause_event: asyncio.Event | None
    on_progress: Callable[[ScriptProgress], None] | None
    total_steps: int | None
    scripts: dict[str, Script]
    run_id: str | None
    steps_executed: int = field(default=0)
    frames_captured: int = field(default=0)
    role_to_slots: dict[str, dict[int, str]] = field(default_factory=dict)
    role_to_focus_range: dict[str, tuple[int, int]] = field(default_factory=dict)
    role_to_component: dict[str, rig_store.Component] = field(default_factory=dict)
    observatory: Observatory | None = None
    optional_role_components: dict[str, rig_store.Component] = field(default_factory=dict)


async def execute_script(
    script_id: str,
    rig_id: str,
    parameters: dict[str, Any],
    *,
    location_id: str | None = None,
    cancel_event: asyncio.Event | None = None,
    pause_event: asyncio.Event | None = None,
    on_progress: Callable[[ScriptProgress], None] | None = None,
    run_id: str | None = None,
) -> ScriptResult:
    """Run the script identified by `script_id` against the rig identified by `rig_id`.

    `location_id`, if given, identifies an `Observatory` (`docs/ObservatorySchema.md`) an
    unknown id raises `ScriptValidationError`, matching `rig_id`'s own behavior. Omitted
    (the default), no observatory-dependent enrichment happens at all — currently only
    `capture_frame`'s celestial-context FITS headers (INDIMCP-60) consume it, and that's
    itself best-effort even when a location *is* given (see `_ExecutionContext`).

    `run_id` is purely a label passed through to `capture_frame` steps
    (which tag each frame they save with it via `frame_store.save_frame`)
    — `None` for a run with no run identity of its own (this engine has no
    concept of one; `script_runs.start_script` supplies its own `uuid4` for
    every real run it starts). A frame saved with `run_id=None` is
    indistinguishable from one captured entirely outside any script run,
    per `frame_store`'s own "`None` for a frame captured ad hoc" convention.

    Resolves every rig-component role referenced anywhere in the call tree
    (this script, and every script it transitively calls via `run_script`)
    to a device up front — a role with no matching component, a matching
    component with no `device`, or a role matching more than one
    device-bearing component all raise `ScriptValidationError` before any
    step runs (see `docs/ScriptSchema.md#resolving-roles-to-devices`). A
    step's `role` may itself be a `"{{ paramName }}"` parameter reference,
    not just a literal — resolution threads each script invocation's own
    concrete parameter values (this run's `parameters`, and, for a
    `run_script` call, its arguments substituted against the *caller's*
    parameters) through the whole call tree before any step runs, so a bad
    parameterized role fails just as fast as a bad literal one (see
    `_collect_role_usage`). Also warns (logs) about any resolved device not
    currently connected to `indiserver` at all, mirroring `check_rig`'s
    "warn rather than fail" behavior for the *whole rig* (a rig might
    intentionally be used without one of its components), and separately,
    strictly, checks that every device this specific run actually needs has
    `CONNECTION.CONNECT = On` — raising `ScriptPreconditionError` if not
    (see `_check_devices_connected`), so a run against a device that's
    present but not yet connected fails clearly up front rather than with a
    raw, confusing error partway through a step.

    `cancel_event`/`pause_event` are checked between steps throughout the
    whole run, including inside nested `run_script` calls (cancellation
    cascades) and `repeat` iterations; `pause_event` is only honored while
    the currently-executing (sub-)script's `pausable` is true (dynamic
    pausability — see `docs/Design.md#composing-scripts`).
    """
    script = _get_script(script_id)
    rig = _get_rig(rig_id)
    observatory = _get_observatory(location_id) if location_id is not None else None
    scripts = _collect_reachable_scripts(script)
    resolved_params = _resolve_parameters(script, parameters)
    usage = _collect_role_usage(script, resolved_params, scripts)
    role_to_component = _resolve_role_to_component(rig, usage.roles)
    # `_resolve_role_to_component` only ever matches components with `device is not None`
    # (see its docstring), so this is always a `str`, never `None`, despite `Component.device`'s
    # own `str | None` type.
    role_to_device = {
        role: component.device
        for role, component in role_to_component.items()
        if component.device is not None
    }
    known_devices = set(indi_messaging.list_devices())
    _warn_on_missing_devices(rig_id, known_devices)
    _check_devices_connected(role_to_device, known_devices, usage.connection_managed_roles)

    optional_role_components: dict[str, rig_store.Component] = {}
    for optional_role in _OPTIONAL_METADATA_ROLES:
        component = _resolve_optional_role_component(rig, optional_role)
        if component is not None:
            optional_role_components[optional_role] = component

    ctx = _ExecutionContext(
        role_to_device=role_to_device,
        cancel_event=cancel_event,
        pause_event=pause_event,
        on_progress=on_progress,
        scripts=scripts,
        run_id=run_id,
        total_steps=_count_total_steps(script, scripts),
        role_to_slots={
            role: component.slots or {} for role, component in role_to_component.items()
        },
        role_to_focus_range={
            role: (component.minPosition, component.maxPosition)
            for role, component in role_to_component.items()
            if component.minPosition is not None and component.maxPosition is not None
        },
        role_to_component=role_to_component,
        observatory=observatory,
        optional_role_components=optional_role_components,
    )
    await _execute_steps(script.steps, ctx, resolved_params, script.id, script.pausable)
    return {
        "scriptId": script.id,
        "stepsExecuted": ctx.steps_executed,
        "framesCaptured": ctx.frames_captured,
    }


def _get_script(script_id: str) -> Script:
    """`script_store.get_script`, wrapped so an unknown id raises `ScriptValidationError`.

    Every lookup this module does before/during a run — the top-level
    script here, and each `run_script` callee walked by
    `_collect_reachable_scripts` — goes through this, so a caller relying
    on this module's documented exception contract (`ScriptValidationError`/
    `ScriptPreconditionError`/`ScriptExecutionError`/`ScriptCancelled`,
    never anything else) never sees a bare `ValueError` leak from
    `script_store` instead.
    """
    try:
        return script_store.get_script(script_id)
    except ValueError as exc:
        raise ScriptValidationError(str(exc)) from exc


def _get_rig(rig_id: str) -> rig_store.Rig:
    """`rig_store.get_rig`, wrapped so an unknown id raises `ScriptValidationError`.

    See `_get_script` for why.
    """
    try:
        return rig_store.get_rig(rig_id)
    except ValueError as exc:
        raise ScriptValidationError(str(exc)) from exc


def _get_observatory(location_id: str) -> Observatory:
    """`observatory_store.get_observatory`, wrapped so an unknown id raises
    `ScriptValidationError`. See `_get_script` for why.
    """
    try:
        return observatory_store.get_observatory(location_id)
    except ValueError as exc:
        raise ScriptValidationError(str(exc)) from exc


_OPTIONAL_METADATA_ROLES = ("mount", "filterWheel", "telescope", "focuser")
"""Roles `execute_script` best-effort resolves into `_ExecutionContext.optional_role_components`
for `capture_frame`'s FITS header enrichment (INDIMCP-60) — see that field's docstring."""


def _resolve_optional_role_component(rig: rig_store.Rig, role: str) -> rig_store.Component | None:
    """`role` resolved to a rig component, or `None` if it isn't resolvable.

    Best-effort, unlike `_resolve_role_to_component`'s normal strict behavior — a rig with no
    matching component for `role` (or an ambiguous one) simply means the FITS-header fields
    that depend on it (telescope optics, filter name, focuser position, ... — see
    `_add_fits_header_fields`) aren't available for this run, not that the run itself should
    fail; nothing about `capture_frame` otherwise requires any of `_OPTIONAL_METADATA_ROLES`
    to exist.

    Deliberately **not** built on `_resolve_role_to_component`, unlike other best-effort
    lookups in this module: that helper requires `component.device is not None` (see its own
    docstring), which is correct for a role that must resolve to a live INDI device to send
    commands to — but a `"telescope"` component has no `device` of its own at all (it isn't a
    driver — `docs/RigSchema.md`), so that filter would make a `"telescope"` role
    unresolvable here even when the rig declares one perfectly validly. This only reads
    already-known rig config, so it doesn't need a device to exist.
    """
    matches = [component for component in rig.components if component.role == role]
    if len(matches) != 1:
        return None
    return matches[0]


def _collect_reachable_scripts(
    script: Script, _collected: dict[str, Script] | None = None
) -> dict[str, Script]:
    """Every script reachable from `script` via `run_script` (including itself), keyed by `id`.

    Read from `script_store` exactly once per run, up front — see
    `_ExecutionContext.scripts` for why the rest of the run must resolve
    `run_script` callees from this snapshot rather than calling back into
    the live store. Safe to recurse without a separate cycle guard: a
    script already in `_collected` just returns immediately, and
    `script_store.load_scripts` already rejects any `run_script` call
    cycle at load time, so this always terminates.
    """
    collected = _collected if _collected is not None else {}
    if script.id in collected:
        return collected
    collected[script.id] = script
    for call in _run_script_calls(script.steps):
        callee = _get_script(call.script)
        _collect_reachable_scripts(callee, collected)
    return collected


def _run_script_calls(steps: list[Step]) -> list[RunScriptStep]:
    calls: list[RunScriptStep] = []
    for step in steps:
        if isinstance(step, RunScriptStep):
            calls.append(step)
        elif isinstance(step, RepeatStep):
            calls.extend(_run_script_calls(step.steps))
        elif isinstance(step, IfStep):
            calls.extend(_run_script_calls(step.then))
            calls.extend(_run_script_calls(step.else_))
    return calls


def _substituted_role(role: str, params: dict[str, Any]) -> str:
    """Resolve a step's `role` field against `params`, same as any other substitutable value.

    A `role` may be a literal (`"mount"`) or a `"{{ paramName }}"`
    reference — `script_store` already validates that any such reference
    names a parameter the script itself declares (it walks *every* string
    field, not just the ones the engine happens to substitute today, see
    `script_store._iter_string_fields`), so `params[name]` below can't
    `KeyError` for a script that loaded successfully. Raises
    `ScriptValidationError` if the resolved value isn't a string — a role
    parameter declared with a non-`"string"` type, or one whose supplied
    value isn't a string, can't name a rig-component role.
    """
    resolved = _substitute(role, params)
    if not isinstance(resolved, str):
        raise ScriptValidationError(
            f"role {role!r} resolved to {resolved!r}, which isn't a string; "
            "a role parameter must be declared type: string"
        )
    return resolved


def _step_role(step: Step, params: dict[str, Any]) -> str | None:
    """The concrete role `step` targets, substituted against `params`; `None` if it has none."""
    if isinstance(
        step,
        SetPropertyStep
        | CaptureFrameStep
        | SlewStep
        | CoolCameraStep
        | SelectFilterStep
        | SetFocusPositionStep,
    ):
        return _substituted_role(step.role, params)
    if isinstance(step, WaitForStep | IfStep):
        return _substituted_role(step.condition.role, params)
    if isinstance(step, RepeatStep) and step.until is not None:
        return _substituted_role(step.until.role, params)
    return None


@dataclass
class _RoleUsage:
    """The concrete roles a run needs, and which of those it manages `CONNECTION` for itself.

    `connection_managed_roles` is position-aware, not tree-global: a role X
    is exempt from `_check_devices_connected`'s "must already be connected"
    requirement only if the *first* use of X, in execution order (walking
    `run_script` calls inline at their real position — see
    `_collect_role_usage`), is itself a step that sets/checks `CONNECTION`
    for X. A role used for something else first (even if some later step
    manages `CONNECTION` for it) is not exempt — that earlier use still
    needs the device already connected, and gets the clean
    `ScriptPreconditionError` `_check_devices_connected` guarantees, rather
    than a raw error mid-step. This is what makes a composed sequence
    (INDIMCP-49) that mixes a `connect` call with other steps against the
    same role safe: the connect call only grants the exemption if it
    genuinely comes first for that role (INDIMCP-53).

    Still deliberately coarse across `if` branches: `then`/`else` are
    walked as if sequential (`then` first), even though only one runs at
    execution time, so a role connected in one branch reads as already
    exempt by the time the other branch is walked, regardless of which
    branch a real run takes. Correctly scoping this would need per-branch,
    path-sensitive tracking; not worth the complexity for scripts that
    don't exist yet (no shipped script uses `if` at all today).
    """

    roles: set[str] = field(default_factory=set)
    connection_managed_roles: set[str] = field(default_factory=set)


def _params_cache_key(params: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
    """A hashable key for a resolved-parameter dict — every `Parameter.type` is a scalar
    (string/integer/number/boolean), so every value is hashable and this can't raise."""
    return tuple(sorted(params.items()))


def _collect_role_usage(
    script: Script,
    params: dict[str, Any],
    scripts: dict[str, Script],
    usage: _RoleUsage | None = None,
    _visited: set[tuple[str, tuple[tuple[str, Any], ...]]] | None = None,
) -> _RoleUsage:
    """Walk the whole call tree rooted at `script` (run with `params`), resolving every role.

    Unlike a purely structural walk (the engine's earlier approach), this
    threads each invocation's own concrete parameter values through
    `run_script` calls — a callee's arguments are substituted against the
    *caller's* current `params`, then validated/defaulted against the
    callee's own declared parameters (`_resolve_parameters`, the same
    rules already applied to the top-level call in `execute_script`) —
    so a role written as `"{{ paramName }}"` resolves to the real
    rig-component role this specific run will use, and a bad one still
    fails before any step runs, matching the guarantee for literal roles.

    Walks steps in true execution order (see `_walk_role_usage`), inlining
    `run_script` calls at their real position rather than visiting a
    script's own steps and its callees' steps as two separate passes — this
    is what makes `connection_managed_roles` position-aware (see
    `_RoleUsage`) instead of tree-global.

    `_visited` memoizes by `(script.id, resolved params)`, not just
    `script.id` — walking a purely structural call tree could dedupe by id
    alone (`_collect_reachable_scripts` does), but here the same script
    called twice with *different* parameters is a different role-usage
    result each time, so only an exact (script, params) repeat is safe to
    skip. This still bounds the walk for a script that calls the same
    sub-script from several call sites with the same arguments (e.g. a
    composed sequence connecting several roles by repeatedly calling
    `connect` — each distinct role is its own cache entry, but a role
    connected more than once in one run is only walked once).
    """
    if usage is None:
        usage = _RoleUsage()
    if _visited is None:
        _visited = set()
    cache_key = (script.id, _params_cache_key(params))
    if cache_key in _visited:
        return usage
    _visited.add(cache_key)

    _walk_role_usage(script.steps, params, scripts, usage, _visited)
    return usage


def _walk_role_usage(
    steps: list[Step],
    params: dict[str, Any],
    scripts: dict[str, Script],
    usage: _RoleUsage,
    _visited: set[tuple[str, tuple[tuple[str, Any], ...]]],
) -> None:
    """Record `steps`' role usage in execution order, inlining `run_script` calls in place.

    A role's *first* recorded use (across this whole walk, including into
    callees, in true execution order) determines whether it lands in
    `connection_managed_roles` — see `_RoleUsage`. A `run_script` step
    recurses via `_collect_role_usage` itself (not a separate
    call-collection pass, unlike the structural walk `_run_script_calls`
    does for `_collect_reachable_scripts`) so its callee's steps are
    visited at exactly the point the caller would actually run them, and so
    the top-level (script.id, params) memoization in `_collect_role_usage`
    still applies to nested calls.
    """
    for step in steps:
        if isinstance(step, RunScriptStep):
            callee = scripts[step.script]
            call_args = {
                name: _substitute(value, params) for name, value in step.parameters.items()
            }
            callee_params = _resolve_parameters(callee, call_args)
            _collect_role_usage(callee, callee_params, scripts, usage, _visited)
            continue

        role = _step_role(step, params)
        if role is not None:
            first_use = role not in usage.roles
            usage.roles.add(role)
            sets_connection = isinstance(step, SetPropertyStep) and step.property == "CONNECTION"
            waits_on_connection = (
                isinstance(step, WaitForStep) and step.condition.property == "CONNECTION"
            )
            if first_use and (sets_connection or waits_on_connection):
                usage.connection_managed_roles.add(role)

        if isinstance(step, RepeatStep):
            _walk_role_usage(step.steps, params, scripts, usage, _visited)
        elif isinstance(step, IfStep):
            _walk_role_usage(step.then, params, scripts, usage, _visited)
            _walk_role_usage(step.else_, params, scripts, usage, _visited)


def _count_total_steps(script: Script, scripts: dict[str, Script]) -> int | None:
    """The exact number of steps a run of `script` will dispatch, or `None` if that isn't knowable.

    Walks the whole call tree (this script, plus every script it calls via
    `run_script`, transitively, resolved from `scripts` — the run's own
    snapshot, see `_collect_reachable_scripts` — never the live
    `script_store` module state) counting one per dispatched step —
    matching `stepsExecuted`'s own accounting, including container steps
    like `repeat`/`run_script` counting themselves. No cycle-tracking is
    needed here (unlike a "visited" set): a script called twice (two
    separate `run_script` steps naming the same callee) must be counted
    twice, not skipped the second time, and `script_store.load_scripts`
    already guarantees the call graph has no cycle to recurse into forever.

    This is only ever exact or `None`, never an estimate presented as if it
    were exact:

    * a `repeat.until` loop's iteration count depends on live INDI state,
      so any script containing one anywhere in reach makes the whole total
      unknown (`None`) — reporting `maxIterations` as if it were the real
      total would look like a progress bar that never finishes, since a
      script normally satisfies `until` well before the cap.
    * an `if` step's `then`/`else` branches are only chosen at runtime; if
      they have different step counts, the total is likewise unknown. If
      they happen to match, the count is unambiguous regardless of which
      branch actually runs.
    """
    return _count_steps_list(script.steps, scripts)


def _count_steps_list(steps: list[Step], scripts: dict[str, Script]) -> int | None:
    total = 0
    for step in steps:
        count = _count_one_step(step, scripts)
        if count is None:
            return None
        total += count
    return total


def _count_one_step(step: Step, scripts: dict[str, Script]) -> int | None:
    if isinstance(step, RepeatStep):
        if step.until is not None:
            return None
        if step.count is None:  # pragma: no cover - schema validation guarantees one is set
            raise ScriptExecutionError(
                f"repeat step {step!r} has neither count nor until; "
                "schema validation should have rejected this"
            )
        body = _count_steps_list(step.steps, scripts)
        return None if body is None else 1 + body * step.count
    if isinstance(step, IfStep):
        then_count = _count_steps_list(step.then, scripts)
        else_count = _count_steps_list(step.else_, scripts)
        if then_count is None or else_count is None or then_count != else_count:
            return None
        return 1 + then_count
    if isinstance(step, RunScriptStep):
        callee_total = _count_total_steps(scripts[step.script], scripts)
        return None if callee_total is None else 1 + callee_total
    return 1


def _resolve_role_to_component(
    rig: rig_store.Rig, roles: set[str]
) -> dict[str, rig_store.Component]:
    """Resolve every role in `roles` to exactly one device-bearing rig component.

    A role with no matching component, or matching only components with no
    `device` (e.g. a `telescope`, which has no INDI device of its own), is a
    validation error, per `docs/ScriptSchema.md#resolving-roles-to-devices`.
    A role matching *more than one* device-bearing component is also
    treated as an error here — the schema doc only disambiguates same-role
    rig components by `id`, not by a script's generic role reference, so
    resolving to more than one device is ambiguous rather than a case to
    silently pick one from.

    The single source of truth for "which component does this role mean" —
    `execute_script` derives both `role_to_device` (every step's `role` →
    `device`) and `role_to_slots` (`select_filter`'s `filterName` lookup)
    from this same result, so the two can never silently disagree about
    which component a role resolved to.
    """
    role_to_component: dict[str, rig_store.Component] = {}
    for role in roles:
        matches = [
            component
            for component in rig.components
            if component.role == role and component.device is not None
        ]
        if not matches:
            raise ScriptValidationError(
                f"role {role!r} has no INDI device in rig {rig.id!r} "
                "(no matching component, or the matching component has no device)"
            )
        if len(matches) > 1:
            ids = ", ".join(component.id for component in matches)
            raise ScriptValidationError(
                f"role {role!r} is ambiguous in rig {rig.id!r}: matches components {ids}"
            )
        role_to_component[role] = matches[0]
    return role_to_component


def _warn_on_missing_devices(rig_id: str, known_devices: set[str]) -> None:
    check = rig_store.check_rig(rig_id, known_devices)
    if not check["ok"]:
        logger.warning(
            "Rig %r has missing device(s) before running script: %s", rig_id, check["missing"]
        )


def _check_devices_connected(
    role_to_device: dict[str, str], known_devices: set[str], exempt_roles: set[str]
) -> None:
    """Raise `ScriptPreconditionError` for any resolved device that isn't confirmed connected.

    Checks every distinct (role, device) this run actually needs (not the
    whole rig — `_warn_on_missing_devices` already covers that, as a
    warning, separately). Discovered via manual testing: sending a command
    to a device that's known to `indi_messaging` (e.g. present in
    `list_devices()`) but not yet `CONNECTION.CONNECT = On` previously
    raised a raw `ValueError` from deep inside `send_property` instead of
    one of this module's documented exception types — this catches that
    case up front instead.

    Distinguishes two different problems that would otherwise both look
    like "not connected": a device entirely absent from `known_devices`
    (never plugged in, its driver isn't running — `_warn_on_missing_devices`
    already flags this as a warning for the whole rig; this raises for it
    specifically when the *run* needs it) gets its own message, since
    there's no `CONNECTION` property to set `On` for a device `indiserver`
    has never heard of — the fix is checking the physical connection or
    starting the driver, not "connecting" anything. A device `indiserver`
    does know about but hasn't reported `CONNECTION.CONNECT = On` for gets
    the "connect it" message `CONNECTION` actually supports fixing.

    Unlike `_check_not_parked`/`_ensure_track_on_slew` (which treat an
    undefined property as "not applicable, skip" because `TELESCOPE_PARK`/
    `ON_COORD_SET` are genuinely optional on some mount drivers),
    `CONNECTION` is part of INDI's base `DefaultDevice` class — every
    known device defines it. So here, a known device with an undefined
    `CONNECTION` means its properties haven't been received yet (a startup
    race, not "doesn't apply"), and is treated the same as "confirmed not
    connected": both fail loudly before any step runs, rather than risking
    a raw error leaking out mid-script.

    Deliberately does not auto-connect the device. Connecting isn't
    guaranteed side-effect-free across every driver (some focusers home on
    connect, some filter wheels calibrate to a reference slot, some mounts
    do a brief init move) — silently connecting could move hardware in a
    way the script never asked for, the same reasoning `slew` doesn't
    auto-unpark. Left to the script (an explicit `connect` step, see
    INDIMCP-52) or the operator.

    `exempt_roles` (see `_collect_role_usage`'s `connection_managed_roles`) skips the "must
    already be `CONNECT = On`" half of this check for roles whose *first*
    use in the run sets/checks `CONNECTION` — otherwise a `connect_*`/
    `disconnect_*` script could never run against a not-yet-connected
    device, since it would require the very state it exists to create.
    This exemption is position-aware, not whole-run — see
    `_RoleUsage.connection_managed_roles`'s docstring for what that means
    for a composed script (INDIMCP-53). The "must be known to indiserver at
    all" half stays unconditional for every device regardless of exemption:
    no script can connect a device whose driver was never started.
    """
    checked: set[str] = set()
    for role, device in role_to_device.items():
        if device in checked:
            continue
        checked.add(device)
        if device not in known_devices:
            raise ScriptPreconditionError(
                f"device {device!r} (role {role!r}) is not known to indiserver "
                "(not plugged in, or its driver isn't running)"
            )

    checked = set()
    for role, device in role_to_device.items():
        if role in exempt_roles or device in checked:
            continue
        checked.add(device)
        values = indi_messaging.get_property_values(device, "CONNECTION")
        if values is None or values.get("CONNECT") != "On":
            raise ScriptPreconditionError(
                f"device {device!r} (role {role!r}) is not connected; "
                "connect it before running this script"
            )


def _resolve_parameters(script: Script, supplied: dict[str, Any]) -> dict[str, Any]:
    """Fill `script`'s declared parameters from `supplied`: apply defaults, check required."""
    unknown = set(supplied) - set(script.parameters)
    if unknown:
        raise ScriptValidationError(
            f"script {script.id!r} was called with undeclared parameter(s) {sorted(unknown)}"
        )
    resolved: dict[str, Any] = {}
    for name, parameter in script.parameters.items():
        if name in supplied:
            resolved[name] = supplied[name]
        elif parameter.required:
            raise ScriptValidationError(
                f"script {script.id!r} is missing required parameter {name!r}"
            )
        else:
            resolved[name] = parameter.default
    return resolved


def _substitute(value: Any, params: dict[str, Any]) -> Any:
    """Replace a `"{{ name }}"` field value with `params[name]`; anything else is unchanged."""
    if isinstance(value, str):
        match = script_store.PARAMETER_REFERENCE.match(value)
        if match:
            return params[match.group(1)]
    return value


def _resolve_device(role: str, ctx: _ExecutionContext) -> str:
    device = ctx.role_to_device.get(role)
    if device is None:  # pragma: no cover - roles are pre-resolved in execute_script
        raise ScriptValidationError(f"role {role!r} has no resolved device for this run")
    return device


async def _check_cancelled(ctx: _ExecutionContext) -> None:
    if ctx.cancel_event is not None and ctx.cancel_event.is_set():
        raise ScriptCancelled("script run was cancelled")


async def _wait_while_paused(ctx: _ExecutionContext, pausable: bool) -> None:
    """Block while `pause_event` is set, but only for a (sub-)script that declares `pausable`.

    A non-pausable sub-script (e.g. mid-slew) ignores a pending pause
    request until control returns to a pausable one — dynamic pausability,
    see `docs/Design.md#composing-scripts`.
    """
    if not pausable or ctx.pause_event is None:
        return
    while ctx.pause_event.is_set():
        await _check_cancelled(ctx)
        await asyncio.sleep(_PAUSE_POLL_INTERVAL_SECONDS)


def _report_progress(ctx: _ExecutionContext, script_id: str, step: Step) -> None:
    if ctx.on_progress is None:
        return
    ctx.on_progress(
        {
            "scriptId": script_id,
            "stepsExecuted": ctx.steps_executed,
            "totalSteps": ctx.total_steps,
            "message": step.description,
        }
    )


async def _execute_steps(
    steps: list[Step],
    ctx: _ExecutionContext,
    params: dict[str, Any],
    script_id: str,
    pausable: bool,
) -> None:
    for step in steps:
        await _run_one_step(step, ctx, params, script_id, pausable)


StepHandler = Callable[[Any, "_ExecutionContext", dict[str, Any], str, bool], Awaitable[None]]
"""A step handler's uniform signature: `(step, ctx, params, script_id, pausable)`.

Every handler takes the same five arguments even though most ignore
`script_id`/`pausable` (only `repeat`/`if` recurse with them, and
`run_script` uses the *callee's* own id/pausable instead) — a uniform
signature is what makes `STEP_HANDLERS` below a single flat registry
rather than needing per-arity special-casing.
"""


async def _run_one_step(
    step: Step,
    ctx: _ExecutionContext,
    params: dict[str, Any],
    script_id: str,
    pausable: bool,
) -> None:
    await _check_cancelled(ctx)
    await _wait_while_paused(ctx, pausable)
    ctx.steps_executed += 1
    _report_progress(ctx, script_id, step)

    handler = STEP_HANDLERS.get(type(step))
    if handler is None:
        raise ScriptValidationError(
            f"no handler registered for step type {type(step).__name__!r} "
            f"(step={step.description or step!r}); this should be unreachable for a "
            "script that loaded successfully, since script_store only produces the "
            "closed set of step types registered here"
        )
    await handler(step, ctx, params, script_id, pausable)


async def _execute_set_property(
    step: SetPropertyStep,
    ctx: _ExecutionContext,
    params: dict[str, Any],
    script_id: str,
    pausable: bool,
) -> None:
    device = _resolve_device(_substituted_role(step.role, params), ctx)
    elements = {name: str(_substitute(value, params)) for name, value in step.elements.items()}
    await indi_messaging.send_property(device, step.property, elements)


async def _execute_wait_for(
    step: WaitForStep,
    ctx: _ExecutionContext,
    params: dict[str, Any],
    script_id: str,
    pausable: bool,
) -> None:
    """Poll `step.condition` until it's true, or time out.

    Fails immediately (rather than waiting out the full timeout) if the
    condition's own property vector reports `Alert` — same reasoning as
    `_wait_for_property_state`'s fast-fail: a driver-reported hardware
    fault isn't something more polling will resolve. The condition is
    always evaluated first, so a script that's deliberately waiting for
    an `Alert` state itself (e.g. a diagnostic `wait_for` checking
    `property: MOUNT_PARK, operator: equals, value: "Alert"`) still
    succeeds normally — this only fires when the condition is *not* met
    and the property has faulted, not on every transient non-matching
    state (`Busy` while genuinely still in progress is fine).

    `_evaluate_condition` fetches the vector state as part of evaluating the
    condition either way, so this reuses that same result for the Alert
    check rather than fetching it again.
    """
    condition = step.condition
    timeout = float(_substitute(step.timeoutSeconds, params))
    deadline = asyncio.get_running_loop().time() + timeout
    device = _resolve_device(_substituted_role(condition.role, params), ctx)
    while True:
        await _check_cancelled(ctx)
        matched, vector_state = _evaluate_condition(condition, ctx, params)
        if matched:
            return
        if vector_state == indi_messaging.PropertyState.ALERT:
            raise ScriptExecutionError(
                f"{condition.property} on {device} went to Alert while polling a wait_for condition"
            )
        if asyncio.get_running_loop().time() >= deadline:
            raise ScriptExecutionError(
                f"wait_for timed out after {timeout}s waiting on {condition.property}"
            )
        await asyncio.sleep(_WAIT_POLL_INTERVAL_SECONDS)


async def _wait_for_property_state(
    ctx: _ExecutionContext,
    device: str,
    property_name: str,
    target_state: indi_messaging.PropertyState,
    timeout_seconds: float,
) -> None:
    """Poll `device`'s `property_name` vector until it reaches `target_state`, or time out.

    A lower-level cousin of `_execute_wait_for`: that one evaluates an
    arbitrary script-authored `Condition` (any property/element/operator);
    this one is for engine-implemented primitives (`slew`, `capture_frame`)
    that need to wait for their own specific `Busy`->`Ok` transition, with
    no `Condition` for a script author to write.

    Fails immediately (rather than waiting out the full timeout) if the
    driver reports `Alert` instead of `target_state` — a driver-reported
    hardware fault (aborted exposure, mount fault, disconnected device)
    isn't something more polling will resolve, so there's no reason to
    keep a caller waiting on it, unlike a genuine "still working" `Busy`.
    """
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        await _check_cancelled(ctx)
        state = indi_messaging.get_property_state(device, property_name)
        if state == target_state:
            return
        if state == indi_messaging.PropertyState.ALERT and target_state != (
            indi_messaging.PropertyState.ALERT
        ):
            raise ScriptExecutionError(f"{property_name} on {device} went to Alert")
        if asyncio.get_running_loop().time() >= deadline:
            raise ScriptExecutionError(
                f"{property_name} on {device} did not reach {target_state} within "
                f"{timeout_seconds}s (last state: {state})"
            )
        await asyncio.sleep(_WAIT_POLL_INTERVAL_SECONDS)


async def _execute_run_script(
    step: RunScriptStep,
    ctx: _ExecutionContext,
    params: dict[str, Any],
    script_id: str,
    pausable: bool,
) -> None:
    callee = ctx.scripts[step.script]  # the run's own snapshot, not the live script_store
    call_args = {name: _substitute(value, params) for name, value in step.parameters.items()}
    resolved_params = _resolve_parameters(callee, call_args)
    await _execute_steps(callee.steps, ctx, resolved_params, callee.id, callee.pausable)


async def _execute_repeat(
    step: RepeatStep,
    ctx: _ExecutionContext,
    params: dict[str, Any],
    script_id: str,
    pausable: bool,
) -> None:
    if step.count is not None:
        for iteration in range(1, step.count + 1):
            await _run_repeat_iteration(step.steps, ctx, params, script_id, pausable, iteration)
        return

    if step.until is None or step.maxIterations is None:  # pragma: no cover - see above
        raise ScriptExecutionError(
            f"repeat step {step!r} has neither count nor until, or is missing maxIterations; "
            "schema validation should have rejected this"
        )
    for iteration in range(1, step.maxIterations + 1):
        await _run_repeat_iteration(step.steps, ctx, params, script_id, pausable, iteration)
        matched, _ = _evaluate_condition(step.until, ctx, params)
        if matched:
            return
    raise ScriptExecutionError(
        f"repeat exceeded maxIterations ({step.maxIterations}) without meeting its until condition"
    )


async def _run_repeat_iteration(
    steps: list[Step],
    ctx: _ExecutionContext,
    params: dict[str, Any],
    script_id: str,
    pausable: bool,
    iteration: int,
) -> None:
    for step in steps:
        if step.every is not None and iteration % step.every != 0:
            continue
        await _run_one_step(step, ctx, params, script_id, pausable)


async def _execute_if(
    step: IfStep,
    ctx: _ExecutionContext,
    params: dict[str, Any],
    script_id: str,
    pausable: bool,
) -> None:
    matched, _ = _evaluate_condition(step.condition, ctx, params)
    branch = step.then if matched else step.else_
    await _execute_steps(branch, ctx, params, script_id, pausable)


async def _execute_capture_frame(
    step: CaptureFrameStep,
    ctx: _ExecutionContext,
    params: dict[str, Any],
    script_id: str,
    pausable: bool,
) -> None:
    """Capture a frame: set frame type/binning/gain/offset/sub-frame (best-effort), expose,
    drain the BLOB, and store it.

    Sequence: set `CCD_FRAME_TYPE`/`CCD_BINNING`/`CCD_GAIN`/`CCD_OFFSET`/`CCD_FRAME` if the
    device defines them (skipped, not an error, if undefined — not every driver reports
    frame type, binning, gain, offset, or sub-frame support, mirroring
    `_check_not_parked`/`_ensure_track_on_slew`'s "optional property" handling) — then set
    `CCD_EXPOSURE`, wait through its `Busy`->`Ok` transition (`_wait_for_property_state`,
    the same primitive `slew` uses for `EQUATORIAL_EOD_COORD`), and drain whatever BLOB
    most recently arrived on `_CCD_BLOB_VECTOR` *after* the exposure command was sent
    (`_wait_for_blob` guards against draining one left over from an earlier, unrelated
    capture of the same device). The drained bytes are saved via `frame_store.save_frame`
    — synchronous/blocking, so wrapped in `asyncio.to_thread` per that module's own
    contract — tagged with this run's `run_id` so a captured frame can be traced back to
    the script run that produced it.

    `gain`/`offset` are skipped entirely (not even a "device defines it?" check) when
    `None` — that's "leave the device's current setting alone", not "set to some default".
    `frameX`/`frameY`/`frameWidth`/`frameHeight` are resolved together via
    `_resolve_frame_roi`, which raises `ScriptExecutionError` for a partial specification
    rather than silently capturing the wrong region.

    Once the BLOB is drained, `_add_fits_header_fields` best-effort enriches it with capture/
    telescope-optics/focuser/filter/telescope-position/Sun-Moon-elongation FITS headers
    (INDIMCP-60) before it's saved — see that function and `fits_headers.py` for what's
    written and why each part is best-effort rather than required.
    """
    role = _substituted_role(step.role, params)
    device = _resolve_device(role, ctx)
    exposure = float(_substitute(step.exposureSeconds, params))
    frame_type = _substitute(step.frameType, params)
    binning_x = _substitute(step.binningX, params)
    binning_y = _substitute(step.binningY, params)
    gain = _substitute(step.gain, params) if step.gain is not None else None
    offset = _substitute(step.offset, params) if step.offset is not None else None
    gain = float(gain) if gain is not None else None
    offset = float(offset) if offset is not None else None
    object_name = _substitute(step.objectName, params) if step.objectName is not None else None
    frame_x = _substitute(step.frameX, params) if step.frameX is not None else None
    frame_y = _substitute(step.frameY, params) if step.frameY is not None else None
    frame_width = _substitute(step.frameWidth, params) if step.frameWidth is not None else None
    frame_height = _substitute(step.frameHeight, params) if step.frameHeight is not None else None
    roi = _resolve_frame_roi(frame_x, frame_y, frame_width, frame_height)

    await _set_frame_type(device, frame_type)
    await _set_binning(device, binning_x, binning_y)
    await _set_gain(device, gain)
    await _set_offset(device, offset)
    await _set_frame_roi(device, roi)

    since = datetime.now(tz=UTC)
    deadline = asyncio.get_running_loop().time() + exposure + _CAPTURE_READOUT_BUFFER_SECONDS
    await indi_messaging.send_property(
        device, "CCD_EXPOSURE", {"CCD_EXPOSURE_VALUE": str(exposure)}
    )
    await _wait_for_property_state(
        ctx,
        device,
        "CCD_EXPOSURE",
        indi_messaging.PropertyState.OK,
        deadline - asyncio.get_running_loop().time(),
    )
    data, extension = await _wait_for_blob(
        ctx, device, _CCD_BLOB_VECTOR, since, deadline - asyncio.get_running_loop().time()
    )
    data = await _add_fits_header_fields(
        ctx, data, device, role, frame_type, gain, offset, object_name, since
    )

    metadata = await asyncio.to_thread(
        frame_store.save_frame, data, device=device, extension=extension, run_id=ctx.run_id
    )
    ctx.frames_captured += 1
    logger.info(
        "capture_frame: device=%s exposureSeconds=%s frameType=%s -> frame %s (%d bytes)",
        device,
        exposure,
        frame_type,
        metadata["frameId"],
        metadata["sizeBytes"],
    )


async def _add_fits_header_fields(
    ctx: _ExecutionContext,
    data: bytes,
    device: str,
    camera_role: str,
    frame_type: str,
    gain: float | None,
    offset: float | None,
    object_name: str | None,
    at: datetime,
) -> bytes:
    """Best-effort: enrich `data`'s FITS header with capture metadata, or return it unmodified.

    Four tiers, per `docs/FitsHeaders.md`:

    - **Every frame type** (`DATE-OBS`, `INSTRUME`, `GAIN`/`OFFSET` if set, `FOCALLEN`/
      `APTDIA`/`TELESCOP`/`SCALE` from the rig's `"telescope"` component, `FOCUSPOS`/
      `FOCUSTEM` from a resolvable `"focuser"`, `SITELAT`/`SITELONG` if a `location_id` was
      given): metadata about the capture/setup itself, meaningful for a calibration frame
      exactly as much as a Light frame (a Dark's gain/offset needs to match the Lights it
      calibrates; the telescope/site didn't change because this frame happens to be a Flat).
    - **`Light`/`Flat` frames** (`FILTER`, if resolvable — `_FILTER_RELEVANT_FRAME_TYPES`): a
      Flat is taken *through* a specific filter, same as a Light — calibrating that filter's
      illumination pattern is the whole point of it. A Dark/Bias is filter-independent
      (typically capped, sensor readout the same regardless of the optical path), so
      recording a filter on one would imply a dependency that doesn't exist.
    - **`Light` frames only, no location needed** (`OBJCTRA`/`OBJCTDEC`/`RA`/`DEC`/`EQUINOX`
      — target position converted to J2000, matching Ekos's convention exactly, see
      `fits_headers.compute_target_position` — `PIERSIDE` if the mount reports one, `OBJECT`
      if the caller supplied `objectName`): a Dark/Flat/Bias frame isn't captured "of"
      anything at the mount's current pointing in any meaningful sense — the mount can be
      tracking, parked, or capped during a calibration sequence — so telescope position would
      be misleading rather than useful, not just unnecessary work.
    - **`Light` frames, additionally needing a `location_id`** (`OBJCTALT`/`OBJCTAZ`/
      `AIRMASS`/`SUNALT`/`MOONSEP`/`MOONPHSE`/`ELONGAT`): needs an observer location to
      compute an Alt-Az frame, unlike the raw EOD/J2000 target position above.

    Each field is independently best-effort: an unresolvable role, an undefined/unparseable
    device property, or `data` not being a FITS file at all (`fits_headers.write_fits_headers`
    returns `None`) all just mean that specific field (or the whole enrichment) is skipped —
    never a failed capture.

    The celestial-geometry computation and the FITS header rewrite are synchronous,
    non-trivial CPU work (coordinate frame transforms; reading/writing a potentially
    multi-megabyte file in memory) — wrapped in `asyncio.to_thread`, same as
    `frame_store.save_frame`, so a capture on this single-core-constrained device doesn't
    block the event loop while it runs.
    """
    fields: fits_headers.FitsHeaderFields = {
        "DATE-OBS": (_format_fits_datetime(at), "UTC date/time of exposure start"),
        "INSTRUME": (device, "Camera (INDI device name)"),
    }
    if gain is not None:
        fields["GAIN"] = (gain, "Camera gain")
    if offset is not None:
        fields["OFFSET"] = (offset, "Camera offset")
    _add_telescope_optics_fields(ctx, camera_role, fields)
    _add_focuser_fields(ctx, fields)
    if ctx.observatory is not None:
        fields["SITELAT"] = (ctx.observatory.latitudeDeg, "[deg] Observatory latitude")
        fields["SITELONG"] = (ctx.observatory.longitudeDeg, "[deg] Observatory longitude")

    if frame_type in _FILTER_RELEVANT_FRAME_TYPES:
        _add_filter_field(ctx, fields)

    if frame_type == "Light":
        if object_name is not None:
            fields["OBJECT"] = (object_name, "Object")
        await _add_mount_derived_fields(ctx, fields, at)

    updated = await asyncio.to_thread(fits_headers.write_fits_headers, data, fields)
    return updated if updated is not None else data


def _format_fits_datetime(at: datetime) -> str:
    """`at` (must be UTC) as a FITS-standard `DATE-OBS` string: no timezone suffix, since FITS
    `DATE-OBS` is implicitly UTC — a literal `+00:00`/`Z` isn't valid in that field."""
    return at.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


def _add_telescope_optics_fields(
    ctx: _ExecutionContext, camera_role: str, fields: fits_headers.FitsHeaderFields
) -> None:
    """`FOCALLEN`/`APTDIA`/`TELESCOP` from the rig's `"telescope"` component, plus `SCALE`
    (plate scale) if the camera's own resolved component also declares `pixelSizeMicron`.

    `"telescope"` components have no INDI `device` of their own (see
    `_resolve_optional_role_component`'s docstring) — everything here comes from rig
    config, not a live property read.
    """
    telescope = ctx.optional_role_components.get("telescope")
    if telescope is None:
        return
    if telescope.focalLengthMm is not None:
        fields["FOCALLEN"] = (telescope.focalLengthMm, "[mm] Telescope focal length")
    if telescope.apertureMm is not None:
        fields["APTDIA"] = (telescope.apertureMm, "[mm] Telescope aperture")
    name = " ".join(part for part in (telescope.make, telescope.model) if part)
    if name:
        fields["TELESCOP"] = (name, "Telescope")

    camera = ctx.role_to_component.get(camera_role)
    if (
        telescope.focalLengthMm is not None
        and camera is not None
        and camera.pixelSizeMicron is not None
    ):
        # Plate scale: arcsec/pixel = 206265 * pixel_size_um / (focal_length_mm * 1000).
        scale = 206.265 * camera.pixelSizeMicron / telescope.focalLengthMm
        fields["SCALE"] = (round(scale, 5), "[arcsec/pixel] Plate scale")


def _add_focuser_fields(ctx: _ExecutionContext, fields: fits_headers.FitsHeaderFields) -> None:
    """`FOCUSPOS`/`FOCUSTEM` from a resolvable `"focuser"`'s `ABS_FOCUS_POSITION`/
    `FOCUS_TEMPERATURE` — independently best-effort (a focuser reporting a position but not a
    temperature, or vice versa, is common; not every focuser has a temperature probe)."""
    focuser = ctx.optional_role_components.get("focuser")
    if focuser is None or focuser.device is None:
        return
    position_values = indi_messaging.get_property_values(focuser.device, "ABS_FOCUS_POSITION")
    if position_values is not None:
        with contextlib.suppress(KeyError, TypeError, ValueError):
            # INDI reports this as a float-formatted string ("12345.0"); truncate to whole
            # steps rather than round, matching FOCUS_ABSOLUTE_POSITION's own integer meaning.
            fields["FOCUSPOS"] = (
                int(float(position_values["FOCUS_ABSOLUTE_POSITION"])),
                "Focuser position in steps",
            )
    temperature_values = indi_messaging.get_property_values(focuser.device, "FOCUS_TEMPERATURE")
    if temperature_values is not None:
        with contextlib.suppress(KeyError, TypeError, ValueError):
            fields["FOCUSTEM"] = (
                round(float(temperature_values["TEMPERATURE"]), 2),
                "[C] Focuser temperature",
            )


def _add_filter_field(ctx: _ExecutionContext, fields: fits_headers.FitsHeaderFields) -> None:
    """`FILTER` from a resolvable `"filterWheel"`'s current `FILTER_SLOT`, resolved to a name
    via the rig component's own `slots` map — `None` (skipped) if the wheel isn't resolvable,
    `FILTER_SLOT` is undefined/unparseable, or the current slot isn't in that map."""
    filter_wheel = ctx.optional_role_components.get("filterWheel")
    if filter_wheel is None or filter_wheel.device is None:
        return
    values = indi_messaging.get_property_values(filter_wheel.device, "FILTER_SLOT")
    if values is None:
        return
    try:
        slot = int(values["FILTER_SLOT_VALUE"])
    except (KeyError, TypeError, ValueError):
        return
    filter_name = (filter_wheel.slots or {}).get(slot)
    if filter_name is not None:
        fields["FILTER"] = (filter_name, "Filter name")


async def _add_mount_derived_fields(
    ctx: _ExecutionContext, fields: fits_headers.FitsHeaderFields, at: datetime
) -> None:
    """`PIERSIDE` and, if the mount is resolvable and reporting a parseable
    `EQUATORIAL_EOD_COORD`, target position (J2000, always) and celestial context
    (additionally, only with a `location_id` — see `_add_fits_header_fields`)."""
    mount = ctx.optional_role_components.get("mount")
    if mount is None or mount.device is None:
        return

    pier_side_values = indi_messaging.get_property_values(mount.device, "TELESCOPE_PIER_SIDE")
    if pier_side_values is not None:
        if pier_side_values.get("PIER_WEST") == "On":
            fields["PIERSIDE"] = ("WEST", "Mount pier side")
        elif pier_side_values.get("PIER_EAST") == "On":
            fields["PIERSIDE"] = ("EAST", "Mount pier side")

    coords = indi_messaging.get_property_values(mount.device, "EQUATORIAL_EOD_COORD")
    if coords is None:
        return
    try:
        ra_hours, dec_deg = float(coords["RA"]), float(coords["DEC"])
    except (KeyError, TypeError, ValueError):
        logger.warning(
            "Mount %s reported an unusable EQUATORIAL_EOD_COORD %r; skipping "
            "telescope-position/celestial-context FITS headers",
            mount.device,
            coords,
        )
        return

    position = await asyncio.to_thread(
        fits_headers.compute_target_position, ra_hours=ra_hours, dec_deg=dec_deg, at=at
    )
    fields.update(fits_headers.target_position_fields(position))

    if ctx.observatory is not None:
        context = await asyncio.to_thread(
            fits_headers.compute_celestial_context,
            ra_hours=ra_hours,
            dec_deg=dec_deg,
            observatory=ctx.observatory,
            at=at,
        )
        fields.update(fits_headers.celestial_context_fields(context))


async def _set_frame_type(device: str, frame_type: str) -> None:
    """Set `CCD_FRAME_TYPE` to `frame_type`; skipped (not an error) if undefined on this device.

    `frame_type` is only pydantic-validated against `FrameType`'s four literals when
    `CaptureFrameStep.frameType` is itself a literal in the YAML — a `"{{ paramName }}"`
    reference is schema-valid at load time regardless of what the run eventually
    substitutes for it (see `FrameTypeOrReference`), so a bad runtime value (e.g. a typo'd
    `frameType` script parameter) reaches here unvalidated and must still be caught before
    it becomes a nonsense `CCD_FRAME_TYPE` command.
    """
    values = indi_messaging.get_property_values(device, "CCD_FRAME_TYPE")
    if values is None:
        return
    element = _FRAME_TYPE_ELEMENTS.get(frame_type)
    if element is None:
        raise ScriptValidationError(f"unknown frameType {frame_type!r}")
    await indi_messaging.send_property(device, "CCD_FRAME_TYPE", {element: "On"})


async def _set_binning(device: str, binning_x: int, binning_y: int) -> None:
    """Set `CCD_BINNING`'s `HOR_BIN`/`VER_BIN`; skipped (not an error) if undefined on this device.

    Sent unconditionally (even for the default 1x1) when the property
    exists, so a capture's binning is deterministic regardless of whatever
    a previous session last left the camera set to — same reasoning as
    `_ensure_track_on_slew` always setting `ON_COORD_SET` rather than
    trusting leftover state.
    """
    values = indi_messaging.get_property_values(device, "CCD_BINNING")
    if values is None:
        return
    await indi_messaging.send_property(
        device, "CCD_BINNING", {"HOR_BIN": str(binning_x), "VER_BIN": str(binning_y)}
    )


async def _set_gain(device: str, gain: float | None) -> None:
    """Set `CCD_GAIN`'s `GAIN` element; skipped if `gain` is `None` or the device has no
    `CCD_GAIN` property (not every camera exposes adjustable gain)."""
    if gain is None:
        return
    values = indi_messaging.get_property_values(device, "CCD_GAIN")
    if values is None:
        return
    await indi_messaging.send_property(device, "CCD_GAIN", {"GAIN": str(gain)})


async def _set_offset(device: str, offset: float | None) -> None:
    """Set `CCD_OFFSET`'s `OFFSET` element; skipped if `offset` is `None` or the device has
    no `CCD_OFFSET` property (not every camera exposes adjustable offset)."""
    if offset is None:
        return
    values = indi_messaging.get_property_values(device, "CCD_OFFSET")
    if values is None:
        return
    await indi_messaging.send_property(device, "CCD_OFFSET", {"OFFSET": str(offset)})


def _resolve_frame_roi(
    frame_x: int | None, frame_y: int | None, frame_width: int | None, frame_height: int | None
) -> tuple[int, int, int, int] | None:
    """`None` if no sub-frame was requested (capture the full sensor); the four resolved
    values if all were set.

    Raises `ScriptExecutionError` for a partial specification — e.g. only `frameWidth` set
    — since that doesn't map to a valid `CCD_FRAME` command, and silently ignoring the
    other three would capture the wrong region with no indication why. Checked here, after
    substitution, rather than as a pydantic `model_validator` on `CaptureFrameStep`, because
    any of the four fields may be a `"{{ paramName }}"` reference whose resolved `None`-ness
    isn't known until execution (see `CaptureFrameStep`'s docstring). Also raises
    `ScriptExecutionError` (not a bare `ValueError`) for a resolved value that isn't a valid
    integer — e.g. a `frameWidth` reference resolving to a non-numeric string — matching this
    module's documented exception contract instead of falling through to
    `script_runs._run_and_record`'s generic "internal error" safety net.
    """
    if frame_x is None and frame_y is None and frame_width is None and frame_height is None:
        return None
    if frame_x is None or frame_y is None or frame_width is None or frame_height is None:
        raise ScriptExecutionError(
            "capture_frame's frameX/frameY/frameWidth/frameHeight must be set together or "
            f"not at all (got frameX={frame_x!r}, frameY={frame_y!r}, "
            f"frameWidth={frame_width!r}, frameHeight={frame_height!r})"
        )
    try:
        return (int(frame_x), int(frame_y), int(frame_width), int(frame_height))
    except (TypeError, ValueError) as exc:
        raise ScriptExecutionError(
            "capture_frame's frameX/frameY/frameWidth/frameHeight must all be valid integers "
            f"(got frameX={frame_x!r}, frameY={frame_y!r}, frameWidth={frame_width!r}, "
            f"frameHeight={frame_height!r})"
        ) from exc


async def _set_frame_roi(device: str, roi: tuple[int, int, int, int] | None) -> None:
    """Set `CCD_FRAME`'s `X`/`Y`/`WIDTH`/`HEIGHT` elements to `roi`; skipped if `roi` is
    `None` (full-sensor capture, see `_resolve_frame_roi`) or the device has no `CCD_FRAME`
    property."""
    if roi is None:
        return
    values = indi_messaging.get_property_values(device, "CCD_FRAME")
    if values is None:
        return
    x, y, width, height = roi
    await indi_messaging.send_property(
        device,
        "CCD_FRAME",
        {"X": str(x), "Y": str(y), "WIDTH": str(width), "HEIGHT": str(height)},
    )


async def _wait_for_blob(
    ctx: _ExecutionContext,
    device: str,
    vector_name: str,
    since: datetime,
    timeout_seconds: float,
) -> tuple[bytes, str]:
    """Poll for a BLOB on `device`'s `vector_name` newer than `since`, or time out.

    `since` (this capture's own "command just sent" timestamp) guards
    against draining a stale BLOB already cached from an earlier, unrelated
    capture of the same device/vector — `indi_messaging.get_latest_blob`
    only ever holds the single most recent update, so without this check a
    capture could return someone else's frame instead of timing out
    honestly. Raises `ScriptExecutionError` if the vector doesn't report
    exactly one member (an ambiguous shape this project has no convention
    for) or on timeout, matching every other engine wait's exception
    contract. Returns `(bytes, extension)`, `extension` taken from the
    BLOB's own reported format rather than guessed (see
    `frame_store.save_frame`'s `extension` parameter), normalized to always
    include a leading dot.
    """
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        await _check_cancelled(ctx)
        snapshot = indi_messaging.get_latest_blob(device, vector_name)
        if snapshot is not None and snapshot["timestamp"] > since:
            if len(snapshot["values"]) != 1:
                raise ScriptExecutionError(
                    f"expected exactly one BLOB member on {device}.{vector_name}, "
                    f"got {sorted(snapshot['values'])}"
                )
            (member,) = snapshot["values"]
            data = snapshot["values"][member]
            _, fmt = snapshot["sizeformat"][member]
            extension = fmt if fmt.startswith(".") else f".{fmt}"
            return data, extension
        if asyncio.get_running_loop().time() >= deadline:
            raise ScriptExecutionError(
                f"no BLOB received on {device}.{vector_name} within {timeout_seconds}s"
            )
        await asyncio.sleep(_WAIT_POLL_INTERVAL_SECONDS)


async def _execute_slew(
    step: SlewStep,
    ctx: _ExecutionContext,
    params: dict[str, Any],
    script_id: str,
    pausable: bool,
) -> None:
    """Slew the mount to `step.target` and wait through the `Busy`->`Ok` transition.

    Fails fast with `ScriptPreconditionError` if the mount is currently
    parked (see `_check_not_parked`) — never unparks it automatically.
    Sets `ON_COORD_SET` to `TRACK` before sending the target coordinate
    (see `_ensure_track_on_slew`), so the mount deterministically ends up
    tracking afterward regardless of whatever mode a previous session left
    it in.

    Only `target.raDec` is implemented: sets `EQUATORIAL_EOD_COORD`'s `RA`/
    `DEC` elements directly. `target.objectName` still needs astropy-based
    name resolution (INDIMCP-29, not built yet) to turn a name like `"M101"`
    into RA/Dec, so it raises `ScriptExecutionError` for now rather than
    silently doing nothing — consistent with this module's exception
    contract (`ScriptValidationError`/`ScriptPreconditionError`/
    `ScriptExecutionError`/`ScriptCancelled` only, never a bare
    `NotImplementedError` leaking out).

    **No horizon/altitude awareness yet.** Neither the target nor the path
    to it is checked against the horizon — two above-horizon endpoints
    don't guarantee an above-horizon path (a GEM mount's axes typically
    move independently, so a slew crossing the meridian can dip well below
    either endpoint's altitude mid-motion). Tracked as INDIMCP-39 (simulate
    the path, reroute around or reject a dip) and INDIMCP-40 (a continuous
    watchdog that aborts motion if the mount is ever observed below
    horizon, independent of how it got there).
    """
    device = _resolve_device(_substituted_role(step.role, params), ctx)
    _check_not_parked(device)
    if step.target.raDec is None:
        raise ScriptExecutionError(
            f"slew to objectName {step.target.objectName!r} is not yet supported "
            "(needs astropy-based name resolution, see INDIMCP-29); use target.raDec instead"
        )
    ra = float(_substitute(step.target.raDec.ra, params))
    dec = float(_substitute(step.target.raDec.dec, params))
    await _ensure_track_on_slew(device)
    await indi_messaging.send_property(
        device, "EQUATORIAL_EOD_COORD", {"RA": str(ra), "DEC": str(dec)}
    )
    await _wait_for_property_state(
        ctx, device, "EQUATORIAL_EOD_COORD", indi_messaging.PropertyState.OK, _SLEW_TIMEOUT_SECONDS
    )


def _check_not_parked(device: str) -> None:
    """Raise `ScriptPreconditionError` if `device`'s mount is currently parked.

    Most mount drivers reject (or simply ignore) a slew command while
    parked, so without this check a slew against a parked mount would just
    time out waiting for `EQUATORIAL_EOD_COORD`'s `Busy`->`Ok` transition,
    with no indication of why. Checked *before* validating the target
    (`raDec`/`objectName`), since being parked is a reason to fail
    regardless of where the script wanted to slew to.

    Not every mount driver exposes `TELESCOPE_PARK` (parking support is
    optional) — a device that doesn't define it is treated as "not
    parked" rather than an error. Unparking is left to the script (an
    explicit `set_property` step against `TELESCOPE_PARK`, or a dedicated
    `unpark` script called first) rather than done automatically here:
    unparking moves the mount, so a script should ask for that explicitly
    rather than get it as a side effect of `slew`.
    """
    values = indi_messaging.get_property_values(device, "TELESCOPE_PARK")
    if values is not None and values.get("PARK") == "On":
        raise ScriptPreconditionError(
            f"mount {device!r} is parked; unpark it (TELESCOPE_PARK) before slewing"
        )


async def _ensure_track_on_slew(device: str) -> None:
    """Set `ON_COORD_SET` to `TRACK`, so `slew` deterministically leaves the mount tracking.

    `ON_COORD_SET` (`SLEW`/`TRACK`/`SYNC`) controls what a new
    `EQUATORIAL_EOD_COORD` command *means* to the driver — without setting
    it explicitly here, whether the mount ends up tracking after a slew
    would depend on whatever mode it was last left in (verified against a
    real `indi_simulator_telescope`: leaving `ON_COORD_SET` alone after a
    previous session set it to `SLEW` would move the mount to the target
    and then silently leave it *not* tracking — star-trailing risk for any
    imaging sequence built on top of `slew`). Unlike `_check_not_parked`,
    this isn't withheld as "an action the script should ask for
    explicitly": engaging tracking is intrinsic to what `slew` means (the
    schema's own wording is "set target coordinates, wait for the mount's
    Busy->Ok transition" — a slew that doesn't end up tracking isn't a
    completed slew for imaging purposes), not a separate hardware action
    like unparking.

    `ON_COORD_SET` is part of INDI's base `Telescope` class and
    near-universal, but not every driver is guaranteed to expose it —
    skipped, not an error, if undefined, matching `_check_not_parked`'s
    handling of `TELESCOPE_PARK`.
    """
    values = indi_messaging.get_property_values(device, "ON_COORD_SET")
    if values is None:
        return
    await indi_messaging.send_property(device, "ON_COORD_SET", {"TRACK": "On"})


async def _execute_cool_camera(
    step: CoolCameraStep,
    ctx: _ExecutionContext,
    params: dict[str, Any],
    script_id: str,
    pausable: bool,
) -> None:
    """Cool the camera to `step.targetTempC` and wait through the `Busy`->`Ok` stabilization.

    Turns `CCD_COOLER` on first, best-effort (skipped, not an error, if the
    device doesn't define it — not every camera has active cooling, mirroring
    `_ensure_track_on_slew`/`_set_frame_type`'s "optional property" handling).
    This best-effort step is exactly why `cool_camera` needs a dedicated step
    type rather than a pure `set_property`/`wait_for` composition like
    `park`/`connect`: a declarative script has no way to skip a property that
    doesn't exist on a given device.

    Then sets `CCD_TEMPERATURE_VALUE` to the target and waits for
    `CCD_TEMPERATURE` to reach `Ok` (the driver's own stabilization signal —
    standard INDI CCD drivers hold the vector at `Busy` until the sensor
    settles at or near the setpoint), using `_wait_for_property_state`, the
    same primitive `slew`/`capture_frame` use for their own `Busy`->`Ok`
    waits.
    """
    device = _resolve_device(_substituted_role(step.role, params), ctx)
    target_temp = float(_substitute(step.targetTempC, params))
    timeout = float(_substitute(step.timeoutSeconds, params))

    await _ensure_cooler_on(device)
    await indi_messaging.send_property(
        device, "CCD_TEMPERATURE", {"CCD_TEMPERATURE_VALUE": str(target_temp)}
    )
    await _wait_for_property_state(
        ctx, device, "CCD_TEMPERATURE", indi_messaging.PropertyState.OK, timeout
    )


async def _ensure_cooler_on(device: str) -> None:
    """Set `CCD_COOLER`'s `COOLER_ON` element; skipped (not an error) if undefined on this device.

    `CCD_COOLER` is a standard but optional INDI CCD property — not every
    camera has active cooling — so an undefined vector is treated as "not
    applicable, skip", matching `_check_not_parked`/`_ensure_track_on_slew`'s
    handling of `TELESCOPE_PARK`/`ON_COORD_SET`.
    """
    values = indi_messaging.get_property_values(device, "CCD_COOLER")
    if values is None:
        return
    await indi_messaging.send_property(device, "CCD_COOLER", {"COOLER_ON": "On"})


async def _execute_select_filter(
    step: SelectFilterStep,
    ctx: _ExecutionContext,
    params: dict[str, Any],
    script_id: str,
    pausable: bool,
) -> None:
    """Select a filter wheel slot and wait through the `Busy`->`Ok` transition.

    `step.slot` (a literal or substituted numeric slot) is used directly if
    set; otherwise `step.filterName` is resolved to a slot number via the
    rig component's own `slots` map (`_resolve_filter_slot`) — a lookup only
    the execution engine can do, since it needs the rig's own configuration,
    not just this step's fields (see `SelectFilterStep`'s docstring for why
    this makes `select_filter` an engine-implemented primitive rather than a
    plain `set_property`/`wait_for` composition).
    """
    role = _substituted_role(step.role, params)
    device = _resolve_device(role, ctx)
    slot = _resolve_filter_slot(step, ctx, role, params)
    timeout = float(_substitute(step.timeoutSeconds, params))
    await indi_messaging.send_property(device, "FILTER_SLOT", {"FILTER_SLOT_VALUE": str(slot)})
    await _wait_for_property_state(
        ctx, device, "FILTER_SLOT", indi_messaging.PropertyState.OK, timeout
    )


def _resolve_filter_slot(
    step: SelectFilterStep, ctx: _ExecutionContext, role: str, params: dict[str, Any]
) -> int:
    """The numeric `FILTER_SLOT_VALUE` `step` targets: `step.slot` directly, or `step.filterName`
    looked up against `role`'s rig-component `slots` map.

    Raises `ScriptExecutionError` for an unknown filter name — resolved
    lazily here (not pre-validated up front the way role-to-device
    resolution is), matching `slew`'s `objectName` resolution, which is the
    other case of a step field that depends on more than its own value and
    fails at execution time rather than validation time. Also raises if
    `filterName` matches more than one slot — `docs/RigSchema.md`'s `slots`
    map doesn't enforce name uniqueness, so a misconfigured rig could
    otherwise have this silently resolve to whichever slot happens to come
    first in iteration order, rotating the physical wheel to the wrong slot
    with no error at all. Matches `_resolve_role_to_component`'s own
    precedent of treating an ambiguous match as a hard error rather than
    silently picking one.
    """
    if step.slot is not None:
        return int(_substitute(step.slot, params))
    filter_name = _substitute(step.filterName, params)
    slots = ctx.role_to_slots.get(role, {})
    matches = [slot_number for slot_number, name in slots.items() if name == filter_name]
    if not matches:
        raise ScriptExecutionError(
            f"role {role!r}'s filter wheel has no slot named {filter_name!r} (known slots: {slots})"
        )
    if len(matches) > 1:
        raise ScriptExecutionError(
            f"role {role!r}'s filter wheel has more than one slot named {filter_name!r}: "
            f"{sorted(matches)} — fix the rig's slots map"
        )
    return matches[0]


async def _execute_set_focus_position(
    step: SetFocusPositionStep,
    ctx: _ExecutionContext,
    params: dict[str, Any],
    script_id: str,
    pausable: bool,
) -> None:
    """Move the focuser to `step.position` and wait through the `Busy`->`Ok` transition.

    Validates `position` against the rig component's own `minPosition`/
    `maxPosition` (`_check_focus_position_in_range`) before sending anything to
    the device — a lookup only the execution engine can do, since it needs the
    rig's own configuration, not just this step's fields (see
    `SetFocusPositionStep`'s docstring for why this makes `set_focus_position`
    an engine-implemented primitive rather than a plain `set_property`/
    `wait_for` composition).
    """
    role = _substituted_role(step.role, params)
    device = _resolve_device(role, ctx)
    position = int(_substitute(step.position, params))
    _check_focus_position_in_range(ctx, role, position)
    timeout = float(_substitute(step.timeoutSeconds, params))
    await indi_messaging.send_property(
        device, "ABS_FOCUS_POSITION", {"FOCUS_ABSOLUTE_POSITION": str(position)}
    )
    await _wait_for_property_state(
        ctx, device, "ABS_FOCUS_POSITION", indi_messaging.PropertyState.OK, timeout
    )


def _check_focus_position_in_range(ctx: _ExecutionContext, role: str, position: int) -> None:
    """Raise `ScriptExecutionError` if `position` is outside `role`'s declared focus range.

    `role_to_focus_range` only has an entry for a role whose rig component
    declares *both* `minPosition`/`maxPosition` (`docs/RigSchema.md`) — both
    are optional, so a component that omits either is treated as having no
    known range, and this is skipped rather than an error, matching
    `_check_not_parked`/`_ensure_cooler_on`'s handling of other optional
    rig/device state.
    """
    range_ = ctx.role_to_focus_range.get(role)
    if range_ is None:
        return
    min_position, max_position = range_
    if not (min_position <= position <= max_position):
        raise ScriptExecutionError(
            f"role {role!r}'s focuser position {position} is outside its declared range "
            f"[{min_position}, {max_position}]"
        )


STEP_HANDLERS: dict[type, StepHandler] = {
    SetPropertyStep: _execute_set_property,
    WaitForStep: _execute_wait_for,
    CaptureFrameStep: _execute_capture_frame,
    SlewStep: _execute_slew,
    CoolCameraStep: _execute_cool_camera,
    SelectFilterStep: _execute_select_filter,
    SetFocusPositionStep: _execute_set_focus_position,
    RunScriptStep: _execute_run_script,
    RepeatStep: _execute_repeat,
    IfStep: _execute_if,
}
"""The whitelist of step types this engine knows how to run.

Every step type `script_store.Script` can produce must have a handler
registered here — `_run_one_step` looks up `type(step)` in this dict and
raises `ScriptValidationError` (rather than silently no-op'ing) if a step's
runtime type isn't registered. Since `script_store`'s `Step` union is
already closed to these same 9 types (INDIMCP-6's "no embedded expression
language" rule — see `docs/ScriptSchema.md`), this can't actually be missed
for a script that loaded successfully; it exists as an explicit,
inspectable whitelist rather than an implicit if/elif chain, and as a
deliberate failure mode if a future refactor ever adds a step type to the
schema without adding its handler here.
"""


def _evaluate_condition(
    condition: Condition, ctx: _ExecutionContext, params: dict[str, Any]
) -> tuple[bool, indi_messaging.PropertyState | str | None]:
    """Evaluate `condition`, returning `(matched, vector_state)`.

    `vector_state` is the condition's own property's overall vector state
    (`Idle`/`Ok`/`Busy`/`Alert`/...), fetched here regardless of whether the
    condition itself compares the vector state or one of its elements.
    Callers that poll (`_execute_wait_for`) use it to fail fast on `Alert`
    without a second, separate fetch; callers that evaluate once and don't
    poll (`_execute_if`, `_execute_repeat`'s `until`) just discard it.
    """
    device = _resolve_device(_substituted_role(condition.role, params), ctx)
    vector_state = indi_messaging.get_property_state(device, condition.property)
    target = _substitute(condition.value, params)
    if condition.element is None:
        actual = vector_state
    else:
        values = indi_messaging.get_property_values(device, condition.property)
        if values is not None and condition.element not in values:
            # The property is defined but doesn't have this element — almost
            # certainly a typo'd element name in the script, not the property
            # "just hasn't reported yet" case get_property_values otherwise
            # treats as routine. Left as a warning rather than a raised error
            # since a property's element set can genuinely vary come and go
            # by device/firmware; but a silent `None` here degrades to
            # "condition never true" with no signal beyond an eventual,
            # confusing wait_for timeout, so at least log it.
            logger.warning(
                "Condition references unknown element %r on %s.%s (has: %s)",
                condition.element,
                device,
                condition.property,
                sorted(values),
            )
        actual = values.get(condition.element) if values is not None else None
    return _compare(actual, condition.operator, target), vector_state


def _compare(
    actual: indi_messaging.PropertyState | str | None, operator: ConditionOperator, target: Any
) -> bool:
    if actual is None:
        return False
    if operator in ("equals", "notEquals"):
        if isinstance(target, bool):
            equal = actual == ("On" if target else "Off")
        else:
            actual_num, target_num = _try_float(actual), _try_float(target)
            if actual_num is not None and target_num is not None:
                equal = actual_num == target_num
            else:
                equal = actual == str(target)
        return equal if operator == "equals" else not equal

    actual_num, target_num = _try_float(actual), _try_float(target)
    if actual_num is None or target_num is None:
        raise ScriptExecutionError(
            f"{operator!r} requires a numeric comparison, got {actual!r} vs {target!r}"
        )
    return {
        "greaterThan": actual_num > target_num,
        "lessThan": actual_num < target_num,
        "greaterThanOrEqual": actual_num >= target_num,
        "lessThanOrEqual": actual_num <= target_num,
    }[operator]


def _try_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
