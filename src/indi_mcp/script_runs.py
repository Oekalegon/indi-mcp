"""Tracking asyncio-`Task`-backed runs of `script_engine.execute_script`, keyed by `runId`.

This is the MCP-facing wrapper INDIMCP-7's `script_engine` module docstring
defers to INDIMCP-13: `execute_script` itself is a plain async function with
no notion of a `runId` or of more than one concurrent invocation — it's
this module's job to launch each run as its own `asyncio.Task`, hand back a
`runId` immediately (per `docs/Design.md#calling-scripts-and-script-results`),
and let that `runId` be polled/cancelled/paused/resumed independently of
whether the caller that started it is still connected.

Every status object below is a `kind`-tagged envelope, the same convention
`indi_messaging.IndiEvent` and `rig_store`'s `RigCheck`/`RigSuggestion`/
`RigDraft` already use — matching the exact shapes laid out in Design.md's
"Calling scripts and script results" section, plus a `rigId` on every
envelope (not in Design.md's illustrative JSON, but requested so progress/
results stay traceable to the physical rig a run used even after the caller
only has a bare `runId` to poll with).

Deliberately out of scope here (left for later tickets, per Design.md's
"Composing scripts"/"Event streams" sections): nested runs don't get their
own `runId`/`parentRunId` — a composed script's `run_script` sub-calls stay
inside `execute_script`'s single flat step count, so `scriptProgress.step`
already walks across nested calls, just without a separate per-sub-script
identity. Every `kind`-tagged status this module produces is also published
to `event_streams` (backing the `indi://scripts` subscribable resource,
INDIMCP-14, and the durable SQLite event log behind it, INDIMCP-15).
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TypedDict

from indi_mcp import event_streams, script_engine, script_store

logger = logging.getLogger(__name__)

__all__ = [
    "ScriptRunCancelled",
    "ScriptRunCompleted",
    "ScriptRunFailed",
    "ScriptRunMessage",
    "ScriptRunPauseRejected",
    "ScriptRunPaused",
    "ScriptRunProgress",
    "ScriptRunResumed",
    "ScriptRunStarted",
    "ScriptRunStatus",
    "cancel_script",
    "get_script_status",
    "pause_script",
    "resume_script",
    "start_script",
]


class ScriptRunStarted(TypedDict):
    """Acknowledges a run has started; returned immediately by `start_script`.

    `pausable` is the top-level script's own declared flag, decided by the
    script definition rather than the caller — see `docs/Design.md#calling-
    scripts-and-script-results`. It's fixed for the lifetime of this run:
    `execute_script` re-evaluates dynamic pausability per (sub-)script
    internally as a composed run moves between callees, but that isn't
    surfaced back through this flat runId — see this module's docstring.
    """

    kind: str
    runId: str
    script: str
    rigId: str
    startedAt: str
    pausable: bool


class ScriptRunProgress(TypedDict):
    """The most recently reported progress for a run, fetched via `get_script_status`.

    `role`/`device` (INDIMCP-58) identify the rig component the step this progress reports
    on is acting on — `None` for a step with no single role of its own (`run_script`,
    `repeat`, `if` with no `condition.role`), see `script_engine.ScriptProgress`.
    """

    kind: str
    runId: str
    rigId: str
    step: int
    totalSteps: int | None
    message: str | None
    role: str | None
    device: str | None


class ScriptRunMessage(TypedDict):
    """A message-only status update for a run — `scriptMessage`, INDIMCP-58.

    Deliberately not fetchable via `get_script_status`/`run.latest_status` (see
    `_run_and_record`'s `on_status`): unlike `scriptProgress`, this doesn't represent the
    run's current state, just a point-in-time note a step handler chose to report (e.g.
    `capture_frame` reporting the frame it just saved) — surfacing it through
    `get_script_status`'s "current state" polling story would risk clobbering whatever real
    progress/terminal status a reconnecting client actually needs to see there. Still
    published live (`indi://scripts`) and durably logged, same as every other `kind` here —
    a client that cares about it subscribes or replays the event log, it just isn't part of
    "the currently recorded status for this run."
    """

    kind: str
    runId: str
    rigId: str
    message: str
    role: str | None
    device: str | None


class ScriptRunError(TypedDict):
    """The error accompanying a `scriptFailed` status.

    Just `message` for now: `script_engine`'s exceptions (`ScriptValidationError`/
    `ScriptPreconditionError`/`ScriptExecutionError`) carry a human-readable
    message but no structured `propertyState`-style detail to surface
    beyond it yet.
    """

    message: str


class ScriptRunCompleted(TypedDict):
    """A successful terminal status; `result` is whatever `execute_script` returned."""

    kind: str
    runId: str
    rigId: str
    finishedAt: str
    result: script_engine.ScriptResult


class ScriptRunFailed(TypedDict):
    """An unsuccessful terminal status."""

    kind: str
    runId: str
    rigId: str
    failedAtStep: int
    error: ScriptRunError


class ScriptRunCancelled(TypedDict):
    """The terminal status of a run stopped via `cancel_script`."""

    kind: str
    runId: str
    rigId: str
    cancelledAtStep: int
    finishedAt: str


class ScriptRunPaused(TypedDict):
    """Returned by a `pause_script` call that succeeded."""

    kind: str
    runId: str
    rigId: str
    pausedAtStep: int


class ScriptRunResumed(TypedDict):
    """Returned by a `resume_script` call that succeeded."""

    kind: str
    runId: str
    rigId: str
    resumedAtStep: int


class ScriptRunPauseRejected(TypedDict):
    """Returned instead of `ScriptRunPaused`/`ScriptRunResumed` when the run can't (yet) honor it.

    Rejected rather than silently ignored or queued, per `docs/Design.md`:
    a run whose (top-level) script isn't `pausable`, or one that's already
    reached a terminal state, can't be paused/resumed at all.
    """

    kind: str
    runId: str
    rigId: str
    reason: str


ScriptRunStatus = (
    ScriptRunStarted
    | ScriptRunProgress
    | ScriptRunCompleted
    | ScriptRunFailed
    | ScriptRunCancelled
    | ScriptRunPaused
    | ScriptRunResumed
    | ScriptRunPauseRejected
)
"""Whatever `get_script_status` (or `cancel_script`) currently has on file for a `runId` —
one of the `kind`-tagged envelopes above, whichever was most recently recorded."""


@dataclass
class _Run:
    """Everything this module tracks for one in-flight or finished run, keyed by `run_id`."""

    run_id: str
    script_id: str
    rig_id: str
    location_id: str | None
    pausable: bool
    cancel_event: asyncio.Event
    pause_event: asyncio.Event
    latest_status: ScriptRunStatus
    latest_step: int = 0
    task: "asyncio.Task[None] | None" = field(default=None)


_runs: dict[str, _Run] = {}

_TERMINAL_KINDS = frozenset({"scriptCompleted", "scriptFailed", "scriptCancelled"})

_MAX_FINISHED_RUNS = 200
"""Cap on how many terminal (completed/failed/cancelled) runs `_runs` retains at once.

Only terminal runs are ever evicted (see `_evict_finished_runs`) — an
in-flight or paused run is never removed regardless of this cap. Without
some bound, `_runs` would grow for as long as this service keeps running
(it's deployed as a long-lived systemd service on a resource-constrained
Pi, not restarted per session), accumulating every run ever started. This
is a coarse memory-bound safety net, not a retention policy — there's no
durability requirement for run status the way there is for the (separate)
SQLite-backed event log (`event_log`, INDIMCP-15), so evicting the oldest
terminal entries once the cap is exceeded is enough.
"""


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


def _is_terminal(run: _Run) -> bool:
    """Whether `run` has reached one of the terminal status kinds.

    Checked against `latest_status["kind"]` rather than `run.task.done()`:
    the latter is still `False` for the remainder of `_run_and_record`'s own
    execution right up until it returns, i.e. exactly when
    `_evict_finished_runs` needs to see a just-finished run as eligible.
    """
    return run.latest_status["kind"] in _TERMINAL_KINDS


def _evict_finished_runs() -> None:
    """Drop the oldest terminal runs once more than `_MAX_FINISHED_RUNS` are on file.

    `_runs` preserves insertion (start) order (a plain `dict`), so the
    oldest entries are visited first — evicting oldest-first keeps whichever
    runs were started most recently pollable the longest, which is what a
    client that just started (or just reconnected after) a run actually
    needs `get_script_status` to still have.
    """
    finished_ids = [run_id for run_id, run in _runs.items() if _is_terminal(run)]
    excess = len(finished_ids) - _MAX_FINISHED_RUNS
    for run_id in finished_ids[:excess]:
        del _runs[run_id]


def _get_run(run_id: str) -> _Run:
    run = _runs.get(run_id)
    if run is None:
        raise ValueError(f"no script run found for runId {run_id!r}")
    return run


async def start_script(
    script_id: str,
    rig_id: str,
    parameters: dict[str, Any] | None = None,
    *,
    location_id: str | None = None,
) -> ScriptRunStarted:
    """Start `script_id` against `rig_id` as a background task and return immediately.

    Looks up `script_id` up front (propagating `script_store`'s `ValueError`
    for an unknown id) purely to report its `pausable` flag in the returned
    envelope — everything else (rig/role/parameter validation) happens
    inside `execute_script`, asynchronously, and shows up as a `scriptFailed`
    status if it doesn't hold up, exactly as it would for any other step
    failure. This mirrors Design.md: starting a script is asynchronous, so a
    bad `rig_id` or bad `parameters` is only discovered once the run
    actually begins, not before this call returns.

    `location_id`, if given, is threaded straight through to `execute_script` — see its own
    docstring (INDIMCP-60). Not echoed into the `scriptStarted`/`scriptProgress`/... envelopes
    below, the same way `parameters` itself isn't: it's an input to the run, not part of its
    reported status.
    """
    script = script_store.get_script(script_id)
    run_id = str(uuid.uuid4())
    started: ScriptRunStarted = {
        "kind": "scriptStarted",
        "runId": run_id,
        "script": script_id,
        "rigId": rig_id,
        "startedAt": _now(),
        "pausable": script.pausable,
    }
    run = _Run(
        run_id=run_id,
        script_id=script_id,
        rig_id=rig_id,
        location_id=location_id,
        pausable=script.pausable,
        cancel_event=asyncio.Event(),
        pause_event=asyncio.Event(),
        latest_status=started,
    )
    _runs[run_id] = run
    event_streams.publish_script_event(started)
    run.task = asyncio.create_task(_run_and_record(run, parameters or {}))
    return started


async def _run_and_record(run: _Run, parameters: dict[str, Any]) -> None:
    """Drive `execute_script` for `run`, recording its progress and terminal status.

    `ScriptCancelled` is reported as `scriptCancelled`, and the three
    documented `script_engine` failure exceptions
    (`ScriptValidationError`/`ScriptPreconditionError`/`ScriptExecutionError`)
    as `scriptFailed` — none of them propagate. Neither does anything else:
    `_run_and_record` runs inside an `asyncio.Task` nobody `await`s under
    normal polling (only `cancel_script` ever awaits it, and only for a run
    it already knows about), so an exception outside that documented
    contract — a genuine bug in `execute_script`/`on_progress`, say — would
    otherwise propagate uncaught, get logged only by asyncio's default
    handler (not this module's own `logger`), and leave `run.latest_status`
    frozen at whatever it last was forever. For a system polling a run
    controlling physical hardware, that reads as "still safely running or
    paused" indefinitely, with nothing to say otherwise. The catch-all
    below is a last-resort safety net for exactly that case, not part of
    the documented exception contract itself.
    """

    def on_progress(progress: script_engine.ScriptProgress) -> None:
        run.latest_step = progress["stepsExecuted"]
        run.latest_status = {
            "kind": "scriptProgress",
            "runId": run.run_id,
            "rigId": run.rig_id,
            "step": progress["stepsExecuted"],
            "totalSteps": progress["totalSteps"],
            "message": progress["message"],
            "role": progress["role"],
            "device": progress["device"],
        }
        event_streams.publish_script_event(run.latest_status)

    def on_status(status: script_engine.ScriptStatusMessage) -> None:
        message: ScriptRunMessage = {
            "kind": "scriptMessage",
            "runId": run.run_id,
            "rigId": run.rig_id,
            "message": status["message"],
            "role": status["role"],
            "device": status["device"],
        }
        event_streams.publish_script_event(message)

    try:
        result = await script_engine.execute_script(
            run.script_id,
            run.rig_id,
            parameters,
            location_id=run.location_id,
            cancel_event=run.cancel_event,
            pause_event=run.pause_event,
            on_progress=on_progress,
            on_status=on_status,
            run_id=run.run_id,
        )
    except script_engine.ScriptCancelled:
        run.latest_status = {
            "kind": "scriptCancelled",
            "runId": run.run_id,
            "rigId": run.rig_id,
            "cancelledAtStep": run.latest_step,
            "finishedAt": _now(),
        }
    except (
        script_engine.ScriptValidationError,
        script_engine.ScriptPreconditionError,
        script_engine.ScriptExecutionError,
    ) as exc:
        run.latest_status = {
            "kind": "scriptFailed",
            "runId": run.run_id,
            "rigId": run.rig_id,
            "failedAtStep": run.latest_step,
            "error": {"message": str(exc)},
        }
    except Exception as exc:  # safety net for anything undocumented, see docstring above
        logger.exception("Unexpected error while running script run %s", run.run_id)
        run.latest_status = {
            "kind": "scriptFailed",
            "runId": run.run_id,
            "rigId": run.rig_id,
            "failedAtStep": run.latest_step,
            "error": {"message": f"internal error: {exc}"},
        }
    else:
        run.latest_status = {
            "kind": "scriptCompleted",
            "runId": run.run_id,
            "rigId": run.rig_id,
            "finishedAt": _now(),
            "result": result,
        }
    event_streams.publish_script_event(run.latest_status)
    _evict_finished_runs()


def get_script_status(run_id: str) -> ScriptRunStatus:
    """Return the most recently recorded status for `run_id`.

    This is the reconnect story from `docs/Design.md`: whatever a client
    would have received as a live progress notification is also available
    here by polling, so a client that was disconnected when a run finished
    can still fetch its outcome.
    """
    return _get_run(run_id).latest_status


async def cancel_script(run_id: str) -> ScriptRunStatus:
    """Request cancellation of `run_id` and wait for it to actually stop.

    Always applies, regardless of `pausable` (per Design.md) — sets
    `cancel_event`, which `execute_script` checks between every step
    (including inside nested `run_script` calls and `repeat` iterations),
    then awaits the run's own task so this returns the real terminal status
    rather than a status that's merely "requested." If the run had already
    reached a terminal state before cancellation was noticed (or finishes
    anyway before observing the cancel), that terminal status — completed
    or failed — is returned as-is rather than being overwritten with a
    fabricated `scriptCancelled`.
    """
    run = _get_run(run_id)
    run.cancel_event.set()
    if run.task is not None:
        await run.task
    return run.latest_status


def _pause_rejected(run: _Run, reason: str) -> ScriptRunPauseRejected:
    """Build a `scriptPauseRejected` envelope for `run`.

    Deliberately does *not* write to `run.latest_status` — a rejection is
    this call's own response, not a change to the run's actual state, so it
    must never clobber whatever `scriptProgress`/terminal status is already
    on file for polling. This is what keeps a stray `pause_script`/
    `resume_script` call on an already-finished run from destroying its
    recorded `scriptCompleted`/`scriptFailed`/`scriptCancelled` outcome
    (including a completed run's `result`) — the exact thing
    `get_script_status`'s reconnect story in `docs/Design.md` depends on
    still being there.
    """
    rejected: ScriptRunPauseRejected = {
        "kind": "scriptPauseRejected",
        "runId": run.run_id,
        "rigId": run.rig_id,
        "reason": reason,
    }
    event_streams.publish_script_event(rejected)
    return rejected


def pause_script(run_id: str) -> ScriptRunPaused | ScriptRunPauseRejected:
    """Request `run_id` pause at its next safe point, if its script allows it.

    Gated on the top-level script's `pausable` flag captured at
    `start_script` time, per Design.md ("These only succeed if the run's
    `pausable` flag was `true`"), and on the run not having already reached
    a terminal state — pausing a finished run makes no sense and, if
    allowed, would silently overwrite its recorded outcome (see
    `_pause_rejected`). Doesn't wait for the run to actually reach the
    paused state — `execute_script`'s `pause_event` is only honored between
    steps, so `pausedAtStep` reports the last step progress is known for,
    same as `scriptProgress` would.
    """
    run = _get_run(run_id)
    if _is_terminal(run):
        return _pause_rejected(run, "This run has already finished")
    if not run.pausable:
        return _pause_rejected(run, "This script has no safe point to pause at")
    run.pause_event.set()
    paused: ScriptRunPaused = {
        "kind": "scriptPaused",
        "runId": run.run_id,
        "rigId": run.rig_id,
        "pausedAtStep": run.latest_step,
    }
    run.latest_status = paused
    event_streams.publish_script_event(paused)
    return paused


def resume_script(run_id: str) -> ScriptRunResumed | ScriptRunPauseRejected:
    """Clear a pending pause for `run_id`, if it's actually paused and its script allows it.

    Gated like `pause_script` (already-finished check, `pausable` flag —
    see there), plus one more: `pause_event` must actually be set. Without
    this, calling `resume_script` on a run that was never paused would
    "succeed" — clearing an already-clear event and reporting a misleading
    `scriptResumed` as if something had changed — while also overwriting
    whatever real `scriptProgress`/terminal status was on file for polling.
    """
    run = _get_run(run_id)
    if _is_terminal(run):
        return _pause_rejected(run, "This run has already finished")
    if not run.pausable:
        return _pause_rejected(run, "This script has no safe point to pause at")
    if not run.pause_event.is_set():
        return _pause_rejected(run, "This run is not currently paused")
    run.pause_event.clear()
    resumed: ScriptRunResumed = {
        "kind": "scriptResumed",
        "runId": run.run_id,
        "rigId": run.rig_id,
        "resumedAtStep": run.latest_step,
    }
    run.latest_status = resumed
    event_streams.publish_script_event(resumed)
    return resumed
