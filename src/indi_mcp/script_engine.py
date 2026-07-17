"""Executing a loaded `script_store.Script` against a resolved rig.

This is the internal "given a script, a rig, and parameters, run it" engine
(INDIMCP-7) — it sits below the MCP-facing layer. `run_script`/
`get_script_status`/`cancel_script`/etc. as `@mcp.tool()`s, `runId`
bookkeeping, and the `indi://scripts` event stream are INDIMCP-13/14,
separate tickets that wrap `execute_script` below.

Two things are deliberately incomplete here, both noted inline where they
matter:

* `capture_frame`/`slew` are **stubs** — they log and return without any
  real INDI interaction. Real frame capture needs BLOB draining plus file/
  SQLite storage (INDIMCP-10/11); `slew`'s `objectName` target needs
  astropy (INDIMCP-29). Neither exists yet. This lets the fully-specified
  primitives (`set_property`, `wait_for`, `run_script`, `repeat`, `if`) be
  implemented and tested for real now.
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
    "ScriptProgress",
    "ScriptResult",
    "ScriptValidationError",
    "execute_script",
]

_WAIT_POLL_INTERVAL_SECONDS = 0.2
_PAUSE_POLL_INTERVAL_SECONDS = 0.1


class ScriptValidationError(Exception):
    """Raised before execution starts: a role/rig problem that isn't a per-step failure."""


class ScriptExecutionError(Exception):
    """Raised when a step fails at runtime (`wait_for` timeout, `maxIterations` exceeded, ...)."""


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
    """State shared, unchanged, across an entire run — including into nested `run_script` calls."""

    role_to_device: dict[str, str]
    cancel_event: asyncio.Event | None
    pause_event: asyncio.Event | None
    on_progress: Callable[[ScriptProgress], None] | None
    total_steps: int | None
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
    warns (logs) about any resolved device not currently connected,
    mirroring `check_rig`'s "warn rather than fail" behavior, so a run
    against a rig with a missing device surfaces early rather than failing
    confusingly mid-script.

    `cancel_event`/`pause_event` are checked between steps throughout the
    whole run, including inside nested `run_script` calls (cancellation
    cascades) and `repeat` iterations; `pause_event` is only honored while
    the currently-executing (sub-)script's `pausable` is true (dynamic
    pausability — see `docs/Design.md#composing-scripts`).
    """
    script = script_store.get_script(script_id)
    rig = rig_store.get_rig(rig_id)
    roles = _collect_roles(script)
    role_to_device = _resolve_role_to_device(rig, roles)
    _warn_on_missing_devices(rig_id, role_to_device)
    resolved_params = _resolve_parameters(script, parameters)

    ctx = _ExecutionContext(
        role_to_device=role_to_device,
        cancel_event=cancel_event,
        pause_event=pause_event,
        on_progress=on_progress,
        total_steps=_count_total_steps(script),
    )
    await _execute_steps(script.steps, ctx, resolved_params, script.id, script.pausable)
    return {"scriptId": script.id, "stepsExecuted": ctx.steps_executed}


def _collect_roles(script: Script, _visited: set[str] | None = None) -> set[str]:
    """Every role referenced by `script`'s own steps, plus every script it calls transitively."""
    visited = _visited if _visited is not None else set()
    if script.id in visited:
        return set()
    visited.add(script.id)
    roles = set(script_store.referenced_roles(script))
    for call in _run_script_calls(script.steps):
        callee = script_store.get_script(call.script)
        roles |= _collect_roles(callee, visited)
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


def _count_total_steps(script: Script) -> int | None:
    """The exact number of steps a run of `script` will dispatch, or `None` if that isn't knowable.

    Walks the whole call tree (this script, plus every script it calls via
    `run_script`, transitively — safe to recurse without cycle-tracking
    here, since `script_store.load_scripts` already rejects any `run_script`
    call cycle at load time) counting one per dispatched step — matching
    `stepsExecuted`'s own accounting, including container steps like
    `repeat`/`run_script` counting themselves. Deliberately not
    cycle-tracked as a "visited" set the way `_collect_roles` is: a script
    called twice (two separate `run_script` steps naming the same callee)
    must be counted twice, not skipped the second time.

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
    return _count_steps_list(script.steps)


def _count_steps_list(steps: list[Step]) -> int | None:
    total = 0
    for step in steps:
        count = _count_one_step(step)
        if count is None:
            return None
        total += count
    return total


def _count_one_step(step: Step) -> int | None:
    if isinstance(step, RepeatStep):
        if step.until is not None:
            return None
        assert step.count is not None
        body = _count_steps_list(step.steps)
        return None if body is None else 1 + body * step.count
    if isinstance(step, IfStep):
        then_count = _count_steps_list(step.then)
        else_count = _count_steps_list(step.else_)
        if then_count is None or else_count is None or then_count != else_count:
            return None
        return 1 + then_count
    if isinstance(step, RunScriptStep):
        callee = script_store.get_script(step.script)
        callee_total = _count_total_steps(callee)
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


async def _execute_run_script(
    step: RunScriptStep,
    ctx: _ExecutionContext,
    params: dict[str, Any],
    script_id: str,
    pausable: bool,
) -> None:
    callee = script_store.get_script(step.script)
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

    assert step.until is not None and step.maxIterations is not None
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


async def _execute_slew_stub(
    step: SlewStep,
    ctx: _ExecutionContext,
    params: dict[str, Any],
    script_id: str,
    pausable: bool,
) -> None:
    """Stub: log the intended slew; no real INDI interaction yet.

    Real slewing (setting `EQUATORIAL_EOD_COORD` and waiting through the
    `Busy`->`Ok` transition) and `objectName` resolution (astropy,
    INDIMCP-29) aren't built yet.
    """
    device = _resolve_device(step.role, ctx)
    logger.info(
        "slew stub: device=%s target=%s (no real slew yet, see INDIMCP-29)", device, step.target
    )


STEP_HANDLERS: dict[type, StepHandler] = {
    SetPropertyStep: _execute_set_property,
    WaitForStep: _execute_wait_for,
    CaptureFrameStep: _execute_capture_frame_stub,
    SlewStep: _execute_slew_stub,
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
