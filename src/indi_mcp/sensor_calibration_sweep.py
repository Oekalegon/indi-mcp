"""Sweeping gain/offset/exposure combinations for a sensor calibration analysis (INDIMCP-102).

Background: `docs/SensorCalibration.md` and `docs/SensorAnalysis.md`. A sensor's read noise,
gain, and full-well capacity are all per-gain/offset-setting quantities, and a
photon-transfer-curve fit needs several flat exposure levels per setting — so a full
characterization means running `capture_sensor_calibration_set` (bias + flat-dark; see that
script's own docstring for why flats are captured separately) once per
(gain, offset, flatExposureSeconds) combination. `capture_sensor_calibration_set`'s own
`parameters` can't carry that sweep itself: `docs/ScriptSchema.md`'s `Parameter.type` is a
closed scalar vocabulary (string/integer/number/boolean, no arrays), so there's no way for a
script to receive a caller-supplied *list* of values through the normal parameter/`run_script`
mechanism, regardless of where any looping happens. This module is the sweep orchestration that
lives outside the script schema instead — a plain Python loop over `script_runs.start_script`
calls, exposed as its own MCP tool (`run_sensor_calibration_sweep` in `server.py`) rather than a
new script step.

**Why this needs its own `sweepId`, not just a list of `runId`s.** `run_script` never blocks its
caller (`docs/Design.md#calling-scripts-and-script-results`), and a full sweep can run for a long
time — many combinations, each a real bias/dark capture sequence — so this tool can't block
either. But unlike a caller looping `run_script` itself, this module can't hand back every
combination's `runId` up front: captures are sequenced one at a time (starting them all
concurrently would race multiple capture commands against the same camera), so a combination's
`runId` doesn't exist until every earlier combination has already finished. A `sweepId`-tracked
background task — mirroring `script_runs.py`'s own `_Run`/`_runs` pattern — is what lets a
caller poll one identifier for the whole sweep's progress instead.

**Fail-fast, not best-effort.** If one combination's capture run doesn't complete successfully,
the sweep stops rather than continuing on to later combinations — a failure partway through
usually means something about the rig/settings needs attention before spending more capture time
on likely-bad data. `get_sweep_status`'s `results` still reports every combination that did
complete before the stop, successful or not.

**Every combination is started with `run_id=sweep_id`** (`script_runs.start_script`'s optional
`run_id` override), not a fresh id per combination. Deliberate, not an oversight: after a sweep
finishes, nothing needs to distinguish which specific combination captured a given frame by
`run_id` alone — each frame's own FITS headers (`CCD_GAIN`/`CCD_OFFSET`/`CCD_EXPOSURE`) already
carry that — so sharing one id across every combination in the sweep means every frame the sweep
captures, anywhere, is tagged with the same `run_id`, and `list_frames(run_id=sweepId)` retrieves
all of them in a single call with no `frame_store` schema changes. It also means the existing
`indi://mcp-server/scripts/{runId}`-scoped event stream doubles as a per-sweep event feed for
free, since every combination's `scriptStarted`/`scriptProgress`/`scriptCompleted` events
publish under that same id. Safe only because combinations run strictly sequentially — see
`start_script`'s own docstring for the collision caveat this relies on.

One consequence worth knowing: `get_script_status`/`cancel_script`/`pause_script` (the
individual-run tools in `script_runs.py`) also resolve against a `sweepId`, since it's a real key
in `script_runs`'s own `_runs` dict for as long as a combination is in flight under it — they
just answer about whichever single combination currently occupies that slot, not the sweep as a
whole. Calling the wrong tool on a sweep id doesn't error, it just gives a differently-grained
answer than `get_sensor_calibration_sweep_status`/`cancel_sensor_calibration_sweep` would.

Deliberately out of scope for this pass, matching this module's narrow todo scope
(INDIMCP-102): pausing a sweep (only `cancel_sweep`) — bias/flat-dark capture has no manual
precondition to pause for the way a flat sweep's panel-staging step will (INDIMCP-103).
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
    "SensorCalibrationSweepCancelled",
    "SensorCalibrationSweepCombinationResult",
    "SensorCalibrationSweepCompleted",
    "SensorCalibrationSweepFailed",
    "SensorCalibrationSweepProgress",
    "SensorCalibrationSweepStarted",
    "SensorCalibrationSweepStatus",
    "cancel_sweep",
    "get_sweep_status",
    "start_sweep",
]


class SensorCalibrationSweepStarted(TypedDict):
    """Acknowledges a sweep has started; returned immediately by `start_sweep`."""

    kind: str
    sweepId: str
    rigId: str
    totalCombinations: int
    startedAt: str


class SensorCalibrationSweepCombinationResult(TypedDict):
    """One (gain, offset, flatExposureSeconds) combination's finished capture run.

    `status` is whatever `script_runs.wait_for_completion` returned for that run —
    `scriptCompleted`/`scriptFailed`/`scriptCancelled` — so a caller inspecting `results` after
    the fact can see exactly why a given combination didn't produce usable frames, not just that
    the sweep as a whole stopped.

    `runId` always equals the sweep's own `sweepId` — every combination is deliberately started
    with the same `run_id` (see this module's own docstring for why) — kept as its own field for
    shape symmetry with `script_runs.ScriptRunStatus`, not because it varies per combination.
    """

    gain: float
    offset: float
    flatExposureSeconds: float
    runId: str
    status: script_runs.ScriptRunStatus


class SensorCalibrationSweepProgress(TypedDict):
    """The most recently reported progress for a sweep, fetched via `get_sweep_status`.

    `results` carries every combination finished so far (same shape as the terminal statuses'
    own `results`) — a caller can inspect each combination's outcome as it lands rather than
    waiting for the whole sweep to reach a terminal state.

    `currentRunId` is `None` between combinations and the sweep's own `sweepId` while one is in
    flight (every combination shares that id — see this module's own docstring) — kept mainly to
    say plainly whether a combination is currently running at all.
    """

    kind: str
    sweepId: str
    rigId: str
    combinationsCompleted: int
    totalCombinations: int
    currentRunId: str | None
    results: list[SensorCalibrationSweepCombinationResult]


class SensorCalibrationSweepCompleted(TypedDict):
    """A sweep that ran every combination to a successful `scriptCompleted`."""

    kind: str
    sweepId: str
    rigId: str
    finishedAt: str
    results: list[SensorCalibrationSweepCombinationResult]


class SensorCalibrationSweepFailed(TypedDict):
    """A sweep stopped because one combination's run didn't complete successfully.

    `failedAtCombination` is a 0-indexed position into the sweep's own combination order (the
    same order `itertools.product(gains, offsets, flatExposureSecondsList)` produces) — the
    failing combination's own outcome is the last entry of `results`.
    """

    kind: str
    sweepId: str
    rigId: str
    failedAtCombination: int
    message: str
    results: list[SensorCalibrationSweepCombinationResult]


class SensorCalibrationSweepCancelled(TypedDict):
    """The terminal status of a sweep stopped via `cancel_sweep`."""

    kind: str
    sweepId: str
    rigId: str
    cancelledAtCombination: int
    finishedAt: str
    results: list[SensorCalibrationSweepCombinationResult]


SensorCalibrationSweepStatus = (
    SensorCalibrationSweepStarted
    | SensorCalibrationSweepProgress
    | SensorCalibrationSweepCompleted
    | SensorCalibrationSweepFailed
    | SensorCalibrationSweepCancelled
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
    latest_status: SensorCalibrationSweepStatus
    results: list[SensorCalibrationSweepCombinationResult] = field(default_factory=list)
    current_run_id: str | None = None
    task: "asyncio.Task[None] | None" = field(default=None)


_sweeps: dict[str, _Sweep] = {}

_TERMINAL_KINDS = frozenset(
    {
        "sensorCalibrationSweepCompleted",
        "sensorCalibrationSweepFailed",
        "sensorCalibrationSweepCancelled",
    }
)

_MAX_FINISHED_SWEEPS = 50
"""Cap on how many terminal sweeps `_sweeps` retains at once — same memory-bound rationale as
`script_runs._MAX_FINISHED_RUNS`, just a smaller number since a sweep is a much coarser-grained,
less frequent operation than an individual script run."""


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
        raise ValueError(f"no sensor calibration sweep found for sweepId {sweep_id!r}")
    return sweep


async def start_sweep(
    rig_id: str,
    gains: list[float],
    offsets: list[float],
    flat_exposure_seconds_list: list[float],
    bias_count: int,
    dark_count: int,
    *,
    bias_exposure_seconds: float = 0.0,
    binning_x: int = 1,
    binning_y: int = 1,
    frame_x: int | None = None,
    frame_y: int | None = None,
    frame_width: int | None = None,
    frame_height: int | None = None,
    location_id: str | None = None,
) -> SensorCalibrationSweepStarted:
    """Start a bias/flat-dark sweep over every (gain, offset, flatExposureSeconds) combination.

    Combinations are the cartesian product of the three lists, in `itertools.product` order
    (gains outermost, then offsets, then flat exposure lengths) — the simplest sweep shape, and
    the one a full per-setting PTC characterization actually needs (every gain/offset setting
    gets flat-darks at every planned flat exposure length). Returns immediately, as a background
    `asyncio.Task` per `_run_sweep`'s own docstring — poll `get_sweep_status(sweepId)` for
    progress and the eventual terminal outcome, or use `cancel_sweep` to stop it early.

    `binning_x`/`binning_y`/`frame_x`/`frame_y`/`frame_width`/`frame_height` (INDIMCP-126) are
    fixed for the whole sweep, shared across every combination, exactly like `bias_count`/
    `dark_count` — same "must match the light frames this calibrates" convention as
    `capture_sensor_calibration_set`'s own binning/ROI parameters.
    """
    if not gains or not offsets or not flat_exposure_seconds_list:
        raise ValueError(
            "gains, offsets, and flatExposureSecondsList must each contain at least one value"
        )
    combinations = list(itertools.product(gains, offsets, flat_exposure_seconds_list))
    sweep_id = str(uuid.uuid4())
    started: SensorCalibrationSweepStarted = {
        "kind": "sensorCalibrationSweepStarted",
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
        _run_sweep(
            sweep,
            bias_count,
            dark_count,
            bias_exposure_seconds,
            binning_x=binning_x,
            binning_y=binning_y,
            frame_x=frame_x,
            frame_y=frame_y,
            frame_width=frame_width,
            frame_height=frame_height,
            location_id=location_id,
        )
    )
    return started


def _progress(sweep: _Sweep, combinations_completed: int) -> SensorCalibrationSweepProgress:
    return {
        "kind": "sensorCalibrationSweepProgress",
        "sweepId": sweep.sweep_id,
        "rigId": sweep.rig_id,
        "combinationsCompleted": combinations_completed,
        "totalCombinations": len(sweep.combinations),
        "currentRunId": sweep.current_run_id,
        "results": list(sweep.results),
    }


def _completed(sweep: _Sweep) -> SensorCalibrationSweepCompleted:
    return {
        "kind": "sensorCalibrationSweepCompleted",
        "sweepId": sweep.sweep_id,
        "rigId": sweep.rig_id,
        "finishedAt": _now(),
        "results": list(sweep.results),
    }


def _failed(sweep: _Sweep, index: int, message: str) -> SensorCalibrationSweepFailed:
    return {
        "kind": "sensorCalibrationSweepFailed",
        "sweepId": sweep.sweep_id,
        "rigId": sweep.rig_id,
        "failedAtCombination": index,
        "message": message,
        "results": list(sweep.results),
    }


def _cancelled(sweep: _Sweep, index: int) -> SensorCalibrationSweepCancelled:
    return {
        "kind": "sensorCalibrationSweepCancelled",
        "sweepId": sweep.sweep_id,
        "rigId": sweep.rig_id,
        "cancelledAtCombination": index,
        "finishedAt": _now(),
        "results": list(sweep.results),
    }


async def _run_sweep(
    sweep: _Sweep,
    bias_count: int,
    dark_count: int,
    bias_exposure_seconds: float,
    *,
    binning_x: int,
    binning_y: int,
    frame_x: int | None,
    frame_y: int | None,
    frame_width: int | None,
    frame_height: int | None,
    location_id: str | None,
) -> None:
    """Drive the per-combination capture loop for `sweep`, recording progress and outcome.

    Mirrors `script_runs._run_and_record`'s discipline: this runs inside an `asyncio.Task`
    nobody `await`s under normal polling, so a genuinely unexpected exception (not one of the
    documented per-run outcomes) is caught as a last-resort safety net rather than propagating
    uncaught and leaving `sweep.latest_status` frozen forever — see that function's own docstring
    for why that matters on a long-lived service.
    """
    index = -1
    try:
        for index, (gain, offset, flat_exposure_seconds) in enumerate(sweep.combinations):
            if sweep.cancel_event.is_set():
                sweep.latest_status = _cancelled(sweep, index)
                return
            started = await script_runs.start_script(
                "capture_sensor_calibration_set",
                sweep.rig_id,
                {
                    "gain": gain,
                    "offset": offset,
                    "biasExposureSeconds": bias_exposure_seconds,
                    "flatExposureSeconds": flat_exposure_seconds,
                    "biasCount": bias_count,
                    "darkCount": dark_count,
                    "binningX": binning_x,
                    "binningY": binning_y,
                    "frameX": frame_x,
                    "frameY": frame_y,
                    "frameWidth": frame_width,
                    "frameHeight": frame_height,
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
                    "flatExposureSeconds": flat_exposure_seconds,
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
        logger.exception(
            "Unexpected error while running sensor calibration sweep %s", sweep.sweep_id
        )
        sweep.latest_status = _failed(sweep, index, f"internal error: {exc}")
    finally:
        _evict_finished_sweeps()


def sweep_exists(sweep_id: str) -> bool:
    """Whether `sweep_id` is a sensor calibration sweep tracked here.

    Lets a caller juggling more than one sweep tracker (`server.py`'s `manage_calibration_sweep`,
    which also has `flat_calibration_sweep`'s own sweeps to consider) find the right one by
    membership rather than by triggering and catching `_get_sweep`'s `ValueError` — the two
    trackers' ids never overlap, but probing by exception would tie that caller's correctness to
    `get_sweep_status`/`cancel_sweep` never raising `ValueError` for any other reason, which
    isn't this function's contract to keep.
    """
    return sweep_id in _sweeps


def get_sweep_status(sweep_id: str) -> SensorCalibrationSweepStatus:
    """Return the most recently recorded status for `sweep_id` — the reconnect story for sweeps,
    same as `script_runs.get_script_status` for individual runs."""
    return _get_sweep(sweep_id).latest_status


async def cancel_sweep(sweep_id: str) -> SensorCalibrationSweepStatus:
    """Request cancellation of `sweep_id` and wait for it to actually stop.

    Cancels whichever combination's script run is currently in flight (if any) via
    `script_runs.cancel_script`, so the sweep doesn't wait out a whole capture sequence it's
    about to discard, then awaits the sweep's own task so this returns the real terminal status.
    If the sweep had already reached a terminal state (finished or failed on its own) before
    cancellation was noticed, that terminal status is returned as-is rather than being
    overwritten with a fabricated `sensorCalibrationSweepCancelled`.
    """
    sweep = _get_sweep(sweep_id)
    sweep.cancel_event.set()
    if sweep.current_run_id is not None and not _is_terminal(sweep):
        with contextlib.suppress(ValueError):
            await script_runs.cancel_script(sweep.current_run_id)
    if sweep.task is not None:
        await sweep.task
    return sweep.latest_status
