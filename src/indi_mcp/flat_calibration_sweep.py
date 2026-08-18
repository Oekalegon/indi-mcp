"""Sweeping gain/offset/exposure combinations for the flat side of a sensor calibration
analysis (INDIMCP-103).

The flat-side sibling of `sensor_calibration_sweep.py` (INDIMCP-102) — same shape, same
reasoning for nearly everything (`sweepId` over a bare `runId` list, fail-fast, sharing
`run_id=sweep_id` across combinations so a sweep's frames retrieve in one `list_frames` call),
deliberately kept as a separate module rather than generalized into one shared engine: with only
two sweep types so far, a shared abstraction would be guessing at the right shape prematurely —
see `docs/SensorCalibration.md` for the full design and the two modules' shared background.

**What's actually different here, and why it's a separate module rather than a branch on the
existing one:** capturing a flat needs a light source (a flat panel or equivalent) physically
staged in front of the optics first — something the script engine has no way to do or verify
(see `capture_flat_sequence.yaml`'s own description, and
`docs/SensorCalibration.md`'s "Decision: split capture into a bias/flat-dark script and a
separate flat script"). Unlike `sensor_calibration_sweep`'s bias+flat-dark sweep, this one can't
just start unattended — **decision: no in-band pause/confirmation step here either.** The
client app driving this tool is expected to confirm with its human operator that the panel is
staged *before* calling `start_sweep` at all, the same "caller already knows" precondition
`capture_flat_sequence` itself already carries. This was a deliberate choice over an in-band
`sensorCalibrationSweepAwaitingConfirmation`-style pause: it keeps this module's shape identical
to `sensor_calibration_sweep`'s rather than adding a new status kind and confirmation tool for a
precondition every flat-capturing script already assumes. If a rig ever has a queryable/
controllable INDI flat-panel device, driving it automatically instead of trusting the operator is
tracked separately (INDIMCP-106) rather than folded into this pass.

Runs `capture_flat_sequence` (INDIMCP-103's extension of it with optional `gain`/`offset`, same
"omit to leave the device's current setting alone" convention `capture_sensor_calibration_set`
already uses) once per (gain, offset, exposureSeconds) combination — `filterName`/
`focusPosition`/`count` are fixed for the whole sweep, shared across every combination, exactly
like `sensor_calibration_sweep`'s own `biasCount`/`darkCount`.
"""

import asyncio
import contextlib
import itertools
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TypedDict

from indi_mcp import script_runs

logger = logging.getLogger(__name__)

__all__ = [
    "FlatCalibrationSweepCancelled",
    "FlatCalibrationSweepCombinationResult",
    "FlatCalibrationSweepCompleted",
    "FlatCalibrationSweepFailed",
    "FlatCalibrationSweepProgress",
    "FlatCalibrationSweepStarted",
    "FlatCalibrationSweepStatus",
    "cancel_sweep",
    "get_sweep_status",
    "start_sweep",
]


class FlatCalibrationSweepStarted(TypedDict):
    """Acknowledges a sweep has started; returned immediately by `start_sweep`."""

    kind: str
    sweepId: str
    rigId: str
    totalCombinations: int
    startedAt: str


class FlatCalibrationSweepCombinationResult(TypedDict):
    """One (gain, offset, exposureSeconds) combination's finished capture run.

    `status` is whatever `script_runs.wait_for_completion` returned for that run —
    `scriptCompleted`/`scriptFailed`/`scriptCancelled` — so a caller inspecting `results` after
    the fact can see exactly why a given combination didn't produce usable frames, not just that
    the sweep as a whole stopped.

    `runId` always equals the sweep's own `sweepId` — every combination is deliberately started
    with the same `run_id` (see this module's own docstring, and `sensor_calibration_sweep`'s,
    for why) — kept as its own field for shape symmetry with `script_runs.ScriptRunStatus`, not
    because it varies per combination.
    """

    gain: float
    offset: float
    exposureSeconds: float
    runId: str
    status: script_runs.ScriptRunStatus


class FlatCalibrationSweepProgress(TypedDict):
    """The most recently reported progress for a sweep, fetched via `get_sweep_status`.

    `results` carries every combination finished so far (same shape as the terminal statuses'
    own `results`) — a caller can inspect each combination's outcome as it lands rather than
    waiting for the whole sweep to reach a terminal state.

    `currentRunId` is `None` between combinations and the sweep's own `sweepId` while one is in
    flight (every combination shares that id) — kept mainly to say plainly whether a combination
    is currently running at all.
    """

    kind: str
    sweepId: str
    rigId: str
    combinationsCompleted: int
    totalCombinations: int
    currentRunId: str | None
    results: list[FlatCalibrationSweepCombinationResult]


class FlatCalibrationSweepCompleted(TypedDict):
    """A sweep that ran every combination to a successful `scriptCompleted`."""

    kind: str
    sweepId: str
    rigId: str
    finishedAt: str
    results: list[FlatCalibrationSweepCombinationResult]


class FlatCalibrationSweepFailed(TypedDict):
    """A sweep stopped because one combination's run didn't complete successfully.

    `failedAtCombination` is a 0-indexed position into the sweep's own combination order (the
    same order `itertools.product(gains, offsets, exposureSecondsList)` produces) — the failing
    combination's own outcome is the last entry of `results`.
    """

    kind: str
    sweepId: str
    rigId: str
    failedAtCombination: int
    message: str
    results: list[FlatCalibrationSweepCombinationResult]


class FlatCalibrationSweepCancelled(TypedDict):
    """The terminal status of a sweep stopped via `cancel_sweep`."""

    kind: str
    sweepId: str
    rigId: str
    cancelledAtCombination: int
    finishedAt: str
    results: list[FlatCalibrationSweepCombinationResult]


FlatCalibrationSweepStatus = (
    FlatCalibrationSweepStarted
    | FlatCalibrationSweepProgress
    | FlatCalibrationSweepCompleted
    | FlatCalibrationSweepFailed
    | FlatCalibrationSweepCancelled
)
"""Whatever `get_sweep_status` currently has on file for a `sweepId` — one of the `kind`-tagged
envelopes above, whichever was most recently recorded, mirroring `script_runs.ScriptRunStatus`."""


@dataclass
class _Sweep:
    """Everything this module tracks for one in-flight or finished sweep, keyed by `sweep_id`."""

    sweep_id: str
    rig_id: str
    combinations: list[tuple[float, float, float]]
    cancel_event: asyncio.Event
    latest_status: FlatCalibrationSweepStatus
    results: list[FlatCalibrationSweepCombinationResult] = field(default_factory=list)
    current_run_id: str | None = None
    task: "asyncio.Task[None] | None" = field(default=None)


_sweeps: dict[str, _Sweep] = {}

_TERMINAL_KINDS = frozenset(
    {
        "flatCalibrationSweepCompleted",
        "flatCalibrationSweepFailed",
        "flatCalibrationSweepCancelled",
    }
)

_MAX_FINISHED_SWEEPS = 50
"""Cap on how many terminal sweeps `_sweeps` retains at once — same rationale as
`sensor_calibration_sweep._MAX_FINISHED_SWEEPS`/`script_runs._MAX_FINISHED_RUNS`."""


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


def _is_terminal(sweep: _Sweep) -> bool:
    return sweep.latest_status["kind"] in _TERMINAL_KINDS


def _evict_finished_sweeps() -> None:
    finished_ids = [sweep_id for sweep_id, sweep in _sweeps.items() if _is_terminal(sweep)]
    excess = len(finished_ids) - _MAX_FINISHED_SWEEPS
    for sweep_id in finished_ids[:excess]:
        del _sweeps[sweep_id]


def _get_sweep(sweep_id: str) -> _Sweep:
    sweep = _sweeps.get(sweep_id)
    if sweep is None:
        raise ValueError(f"no flat calibration sweep found for sweepId {sweep_id!r}")
    return sweep


async def start_sweep(
    rig_id: str,
    gains: list[float],
    offsets: list[float],
    exposure_seconds_list: list[float],
    filter_name: str,
    focus_position: int,
    count: int,
    *,
    location_id: str | None = None,
) -> FlatCalibrationSweepStarted:
    """Start a flat sweep over every (gain, offset, exposureSeconds) combination.

    Combinations are the cartesian product of the three lists, in `itertools.product` order
    (gains outermost, then offsets, then exposure lengths), matching
    `sensor_calibration_sweep.start_sweep`'s own resolution of the same question — the simplest
    sweep shape, and the one a full per-setting PTC characterization actually needs (every
    gain/offset setting gets flats at every planned exposure level). Returns immediately, as a
    background `asyncio.Task` per `_run_sweep`'s own docstring — poll `get_sweep_status(sweepId)`
    for progress and the eventual terminal outcome, or use `cancel_sweep` to stop it early.

    Assumes the flat panel is already staged — see this module's own docstring for why there's
    no in-band confirmation step here.
    """
    if not gains or not offsets or not exposure_seconds_list:
        raise ValueError(
            "gains, offsets, and exposureSecondsList must each contain at least one value"
        )
    combinations = list(itertools.product(gains, offsets, exposure_seconds_list))
    sweep_id = str(uuid.uuid4())
    started: FlatCalibrationSweepStarted = {
        "kind": "flatCalibrationSweepStarted",
        "sweepId": sweep_id,
        "rigId": rig_id,
        "totalCombinations": len(combinations),
        "startedAt": _now(),
    }
    sweep = _Sweep(
        sweep_id=sweep_id,
        rig_id=rig_id,
        combinations=combinations,
        cancel_event=asyncio.Event(),
        latest_status=started,
    )
    _sweeps[sweep_id] = sweep
    sweep.task = asyncio.create_task(
        _run_sweep(sweep, filter_name, focus_position, count, location_id)
    )
    return started


def _progress(sweep: _Sweep, combinations_completed: int) -> FlatCalibrationSweepProgress:
    return {
        "kind": "flatCalibrationSweepProgress",
        "sweepId": sweep.sweep_id,
        "rigId": sweep.rig_id,
        "combinationsCompleted": combinations_completed,
        "totalCombinations": len(sweep.combinations),
        "currentRunId": sweep.current_run_id,
        "results": list(sweep.results),
    }


def _completed(sweep: _Sweep) -> FlatCalibrationSweepCompleted:
    return {
        "kind": "flatCalibrationSweepCompleted",
        "sweepId": sweep.sweep_id,
        "rigId": sweep.rig_id,
        "finishedAt": _now(),
        "results": list(sweep.results),
    }


def _failed(sweep: _Sweep, index: int, message: str) -> FlatCalibrationSweepFailed:
    return {
        "kind": "flatCalibrationSweepFailed",
        "sweepId": sweep.sweep_id,
        "rigId": sweep.rig_id,
        "failedAtCombination": index,
        "message": message,
        "results": list(sweep.results),
    }


def _cancelled(sweep: _Sweep, index: int) -> FlatCalibrationSweepCancelled:
    return {
        "kind": "flatCalibrationSweepCancelled",
        "sweepId": sweep.sweep_id,
        "rigId": sweep.rig_id,
        "cancelledAtCombination": index,
        "finishedAt": _now(),
        "results": list(sweep.results),
    }


async def _run_sweep(
    sweep: _Sweep,
    filter_name: str,
    focus_position: int,
    count: int,
    location_id: str | None,
) -> None:
    """Drive the per-combination capture loop for `sweep`, recording progress and outcome.

    Mirrors `sensor_calibration_sweep._run_sweep`/`script_runs._run_and_record`'s discipline:
    this runs inside an `asyncio.Task` nobody `await`s under normal polling, so a genuinely
    unexpected exception (not one of the documented per-run outcomes) is caught as a last-resort
    safety net rather than propagating uncaught and leaving `sweep.latest_status` frozen forever.
    """
    index = -1
    try:
        for index, (gain, offset, exposure_seconds) in enumerate(sweep.combinations):
            if sweep.cancel_event.is_set():
                sweep.latest_status = _cancelled(sweep, index)
                return
            started = await script_runs.start_script(
                "capture_flat_sequence",
                sweep.rig_id,
                {
                    "filterName": filter_name,
                    "focusPosition": focus_position,
                    "exposureSeconds": exposure_seconds,
                    "count": count,
                    "gain": gain,
                    "offset": offset,
                },
                location_id=location_id,
                run_id=sweep.sweep_id,
            )
            sweep.current_run_id = started["runId"]
            sweep.latest_status = _progress(sweep, index)
            status = await script_runs.wait_for_completion(started["runId"])
            sweep.results.append(
                {
                    "gain": gain,
                    "offset": offset,
                    "exposureSeconds": exposure_seconds,
                    "runId": started["runId"],
                    "status": status,
                }
            )
            sweep.current_run_id = None
            sweep.latest_status = _progress(sweep, index + 1)
            if sweep.cancel_event.is_set():
                sweep.latest_status = _cancelled(sweep, index)
                return
            if status["kind"] != "scriptCompleted":
                message = (
                    status.get("error", {}).get("message", f"run ended as {status['kind']!r}")
                    if status["kind"] == "scriptFailed"
                    else f"run ended as {status['kind']!r}"
                )
                sweep.latest_status = _failed(sweep, index, message)
                return
        sweep.latest_status = _completed(sweep)
    except Exception as exc:  # safety net — see docstring above
        logger.exception("Unexpected error while running flat calibration sweep %s", sweep.sweep_id)
        sweep.latest_status = _failed(sweep, index, f"internal error: {exc}")
    finally:
        _evict_finished_sweeps()


def get_sweep_status(sweep_id: str) -> FlatCalibrationSweepStatus:
    """Return the most recently recorded status for `sweep_id` — the reconnect story for sweeps,
    same as `script_runs.get_script_status` for individual runs."""
    return _get_sweep(sweep_id).latest_status


async def cancel_sweep(sweep_id: str) -> FlatCalibrationSweepStatus:
    """Request cancellation of `sweep_id` and wait for it to actually stop.

    Cancels whichever combination's script run is currently in flight (if any) via
    `script_runs.cancel_script`, so the sweep doesn't wait out a whole capture sequence it's
    about to discard, then awaits the sweep's own task so this returns the real terminal status.
    If the sweep had already reached a terminal state (finished or failed on its own) before
    cancellation was noticed, that terminal status is returned as-is rather than being
    overwritten with a fabricated `flatCalibrationSweepCancelled`.
    """
    sweep = _get_sweep(sweep_id)
    sweep.cancel_event.set()
    if sweep.current_run_id is not None and not _is_terminal(sweep):
        with contextlib.suppress(ValueError):
            await script_runs.cancel_script(sweep.current_run_id)
    if sweep.task is not None:
        await sweep.task
    return sweep.latest_status
