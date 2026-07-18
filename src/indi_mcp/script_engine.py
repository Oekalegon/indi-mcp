"""Executing a loaded `script_store.Script` against a resolved rig.

This is the internal "given a script, a rig, and parameters, run it" engine
(INDIMCP-7) — it sits below the MCP-facing layer. `run_script`/
`get_script_status`/`cancel_script`/etc. as `@mcp.tool()`s, `runId`
bookkeeping, and the `indi://scripts` event stream are INDIMCP-13/14,
separate tickets that wrap `execute_script` below.

Two things are deliberately incomplete here, both noted inline where they
matter:

* `capture_frame` is still a **stub** — it logs and returns without any
  real INDI interaction. Real frame capture needs BLOB draining plus file/
  SQLite storage (INDIMCP-10/11), which doesn't exist yet. `slew` is
  implemented for a `raDec` target (INDIMCP-38); its `objectName` target
  still raises `ScriptExecutionError` pending astropy-based name
  resolution (INDIMCP-29).
* Pause/cancel are supported as plain hooks (`asyncio.Event`s) an eventual
  caller passes in — this engine has no `runId`/task-tracking concept of
  its own; that's INDIMCP-13's job.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypedDict

from indi_mcp import indi_messaging, rig_store, script_store
from indi_mcp.script_store import (
    CaptureFrameStep,
    Condition,
    ConditionOperator,
    IfStep,
    RepeatStep,
    RunScriptStep,
    Script,
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

    Intentionally minimal: the real `scriptCompleted`-shaped result
    (`framesCaptured`, `frames`, ...) has nothing real to report until
    `capture_frame` is no longer a stub (INDIMCP-10/11).
    """

    scriptId: str
    stepsExecuted: int


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
    """

    role_to_device: dict[str, str]
    cancel_event: asyncio.Event | None
    pause_event: asyncio.Event | None
    on_progress: Callable[[ScriptProgress], None] | None
    total_steps: int | None
    scripts: dict[str, Script]
    steps_executed: int = field(default=0)


async def execute_script(
    script_id: str,
    rig_id: str,
    parameters: dict[str, Any],
    *,
    cancel_event: asyncio.Event | None = None,
    pause_event: asyncio.Event | None = None,
    on_progress: Callable[[ScriptProgress], None] | None = None,
) -> ScriptResult:
    """Run the script identified by `script_id` against the rig identified by `rig_id`.

    Resolves every rig-component role referenced anywhere in the call tree
    (this script, and every script it transitively calls via `run_script`)
    to a device up front — a role with no matching component, a matching
    component with no `device`, or a role matching more than one
    device-bearing component all raise `ScriptValidationError` before any
    step runs (see `docs/ScriptSchema.md#resolving-roles-to-devices`). Also
    warns (logs) about any resolved device not currently connected to
    `indiserver` at all, mirroring `check_rig`'s "warn rather than fail"
    behavior for the *whole rig* (a rig might intentionally be used
    without one of its components), and separately, strictly, checks that
    every device this specific run actually needs has `CONNECTION.CONNECT
    = On` — raising `ScriptPreconditionError` if not (see
    `_check_devices_connected`), so a run against a device that's present
    but not yet connected fails clearly up front rather than with a raw,
    confusing error partway through a step.

    `cancel_event`/`pause_event` are checked between steps throughout the
    whole run, including inside nested `run_script` calls (cancellation
    cascades) and `repeat` iterations; `pause_event` is only honored while
    the currently-executing (sub-)script's `pausable` is true (dynamic
    pausability — see `docs/Design.md#composing-scripts`).
    """
    script = _get_script(script_id)
    rig = _get_rig(rig_id)
    scripts = _collect_reachable_scripts(script)
    roles = _collect_roles(scripts)
    role_to_device = _resolve_role_to_device(rig, roles)
    _warn_on_missing_devices(rig_id, role_to_device)
    _check_devices_connected(role_to_device)
    resolved_params = _resolve_parameters(script, parameters)

    ctx = _ExecutionContext(
        role_to_device=role_to_device,
        cancel_event=cancel_event,
        pause_event=pause_event,
        on_progress=on_progress,
        scripts=scripts,
        total_steps=_count_total_steps(script, scripts),
    )
    await _execute_steps(script.steps, ctx, resolved_params, script.id, script.pausable)
    return {"scriptId": script.id, "stepsExecuted": ctx.steps_executed}


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


def _collect_roles(scripts: dict[str, Script]) -> set[str]:
    """Every role referenced anywhere across `scripts` — the run's full reachable set.

    `scripts` (see `_collect_reachable_scripts`) already includes every
    script transitively reachable via `run_script`, so no further
    recursion is needed here.
    """
    roles: set[str] = set()
    for script in scripts.values():
        roles |= script_store.referenced_roles(script)
    return roles


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


def _resolve_role_to_device(rig: rig_store.Rig, roles: set[str]) -> dict[str, str]:
    """Resolve every role in `roles` to exactly one device-bearing rig component.

    A role with no matching component, or matching only components with no
    `device` (e.g. a `telescope`, which has no INDI device of its own), is a
    validation error, per `docs/ScriptSchema.md#resolving-roles-to-devices`.
    A role matching *more than one* device-bearing component is also
    treated as an error here — the schema doc only disambiguates same-role
    rig components by `id`, not by a script's generic role reference, so
    resolving to more than one device is ambiguous rather than a case to
    silently pick one from.
    """
    role_to_device: dict[str, str] = {}
    for role in roles:
        matches = [
            (component, component.device)
            for component in rig.components
            if component.role == role and component.device is not None
        ]
        if not matches:
            raise ScriptValidationError(
                f"role {role!r} has no INDI device in rig {rig.id!r} "
                "(no matching component, or the matching component has no device)"
            )
        if len(matches) > 1:
            ids = ", ".join(component.id for component, _ in matches)
            raise ScriptValidationError(
                f"role {role!r} is ambiguous in rig {rig.id!r}: matches components {ids}"
            )
        _, device = matches[0]
        role_to_device[role] = device
    return role_to_device


def _warn_on_missing_devices(rig_id: str, role_to_device: dict[str, str]) -> None:
    check = rig_store.check_rig(rig_id, indi_messaging.list_devices())
    if not check["ok"]:
        logger.warning(
            "Rig %r has missing device(s) before running script: %s", rig_id, check["missing"]
        )


def _check_devices_connected(role_to_device: dict[str, str]) -> None:
    """Raise `ScriptPreconditionError` for any resolved device that isn't confirmed connected.

    Checks every distinct device this run actually needs (not the whole
    rig — `_warn_on_missing_devices` already covers that, as a warning,
    separately). Discovered via manual testing: sending a command to a
    device that's known to `indi_messaging` (e.g. present in
    `list_devices()`) but not yet `CONNECTION.CONNECT = On` previously
    raised a raw `ValueError` from deep inside `send_property` instead of
    one of this module's documented exception types — this catches that
    case up front instead.

    Unlike `_check_not_parked`/`_ensure_track_on_slew` (which treat an
    undefined property as "not applicable, skip" because `TELESCOPE_PARK`/
    `ON_COORD_SET` are genuinely optional on some mount drivers),
    `CONNECTION` is part of INDI's base `DefaultDevice` class — every
    device defines it. So here, an undefined `CONNECTION` means the
    device's properties haven't been received yet (a startup race, not
    "doesn't apply"), and is treated the same as "confirmed not
    connected": both fail loudly before any step runs, rather than risking
    a raw error leaking out mid-script.

    Deliberately does not auto-connect the device. Connecting isn't
    guaranteed side-effect-free across every driver (some focusers home on
    connect, some filter wheels calibrate to a reference slot, some mounts
    do a brief init move) — silently connecting could move hardware in a
    way the script never asked for, the same reasoning `slew` doesn't
    auto-unpark. Left to the script (an explicit `connect` step, see
    INDIMCP-52) or the operator.
    """
    for device in set(role_to_device.values()):
        values = indi_messaging.get_property_values(device, "CONNECTION")
        if values is None or values.get("CONNECT") != "On":
            raise ScriptPreconditionError(
                f"device {device!r} is not connected; connect it before running this script"
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
    device = _resolve_device(step.role, ctx)
    elements = {name: str(_substitute(value, params)) for name, value in step.elements.items()}
    await indi_messaging.send_property(device, step.property, elements)


async def _execute_wait_for(
    step: WaitForStep,
    ctx: _ExecutionContext,
    params: dict[str, Any],
    script_id: str,
    pausable: bool,
) -> None:
    timeout = float(_substitute(step.timeoutSeconds, params))
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        await _check_cancelled(ctx)
        if _evaluate_condition(step.condition, ctx, params):
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise ScriptExecutionError(
                f"wait_for timed out after {timeout}s waiting on {step.condition.property}"
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
    this one is for engine-implemented primitives (`slew` today, `capture_frame`
    later) that need to wait for their own specific `Busy`->`Ok` transition,
    with no `Condition` for a script author to write.
    """
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        await _check_cancelled(ctx)
        state = indi_messaging.get_property_state(device, property_name)
        if state == target_state:
            return
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
        if _evaluate_condition(step.until, ctx, params):
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
    branch = step.then if _evaluate_condition(step.condition, ctx, params) else step.else_
    await _execute_steps(branch, ctx, params, script_id, pausable)


async def _execute_capture_frame_stub(
    step: CaptureFrameStep,
    ctx: _ExecutionContext,
    params: dict[str, Any],
    script_id: str,
    pausable: bool,
) -> None:
    """Stub: log the intended capture; no real INDI interaction yet.

    Real frame capture — setting `CCD_FRAME_TYPE`/`CCD_EXPOSURE`, waiting
    through the `Busy`->`Ok` transition, draining the BLOB, and writing
    frame + SQLite metadata — is INDIMCP-10/11, not built yet.
    """
    device = _resolve_device(step.role, ctx)
    exposure = _substitute(step.exposureSeconds, params)
    logger.info(
        "capture_frame stub: device=%s exposureSeconds=%s frameType=%s "
        "(no real capture yet, see INDIMCP-10/11)",
        device,
        exposure,
        step.frameType,
    )


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
    device = _resolve_device(step.role, ctx)
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


STEP_HANDLERS: dict[type, StepHandler] = {
    SetPropertyStep: _execute_set_property,
    WaitForStep: _execute_wait_for,
    CaptureFrameStep: _execute_capture_frame_stub,
    SlewStep: _execute_slew,
    RunScriptStep: _execute_run_script,
    RepeatStep: _execute_repeat,
    IfStep: _execute_if,
}
"""The whitelist of step types this engine knows how to run.

Every step type `script_store.Script` can produce must have a handler
registered here — `_run_one_step` looks up `type(step)` in this dict and
raises `ScriptValidationError` (rather than silently no-op'ing) if a step's
runtime type isn't registered. Since `script_store`'s `Step` union is
already closed to these same 7 types (INDIMCP-6's "no embedded expression
language" rule — see `docs/ScriptSchema.md`), this can't actually be missed
for a script that loaded successfully; it exists as an explicit,
inspectable whitelist rather than an implicit if/elif chain, and as a
deliberate failure mode if a future refactor ever adds a step type to the
schema without adding its handler here.
"""


def _evaluate_condition(
    condition: Condition, ctx: _ExecutionContext, params: dict[str, Any]
) -> bool:
    device = _resolve_device(condition.role, ctx)
    target = _substitute(condition.value, params)
    if condition.element is None:
        actual = indi_messaging.get_property_state(device, condition.property)
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
    return _compare(actual, condition.operator, target)


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
