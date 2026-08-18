"""Tests for `flat_calibration_sweep.py` (INDIMCP-103).

Uses a fake `capture_flat_sequence` script registered directly in `script_store._scripts`
(mirroring `test_sensor_calibration_sweep.py`'s own pattern) rather than the real shipped YAML,
so these tests exercise the sweep's own orchestration logic (combination order, progress,
fail-fast, cancellation) without depending on real capture-frame INDI traffic.
"""

import asyncio
from typing import Any, cast
from unittest.mock import patch

import pytest

from indi_mcp import (
    event_streams,
    flat_calibration_sweep,
    indi_messaging,
    rig_store,
    script_runs,
    script_store,
)

_known_devices: list[str] = []


@pytest.fixture(autouse=True)
def _reset_stores() -> None:
    rig_store._rigs = {}
    script_store._scripts = {}
    script_runs._runs = {}
    flat_calibration_sweep._sweeps = {}
    event_streams._scripts.clear()
    event_streams._subscribers.clear()
    event_streams._background_tasks.clear()
    _known_devices.clear()


def _default_get_property_values(device: str, name: str) -> dict[str, str] | None:
    if name == "CONNECTION":
        return {"CONNECT": "On", "DISCONNECT": "Off"}
    return None


@pytest.fixture(autouse=True)
def _mock_indi_messaging_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(indi_messaging, "list_devices", lambda: list(_known_devices))
    monkeypatch.setattr(indi_messaging, "get_property_values", _default_get_property_values)
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Idle")


def _rig(*components: rig_store.Component, rig_id: str = "test-rig") -> rig_store.Rig:
    rig = rig_store.Rig(id=rig_id, name="Test rig", components=list(components))
    rig_store._rigs[rig.id] = rig
    _known_devices.extend(c.device for c in components if c.device is not None)
    return rig


_FLAT_SEQUENCE_PARAMETERS = {
    "filterName": {"type": "string", "required": True},
    "focusPosition": {"type": "integer", "required": True},
    "exposureSeconds": {"type": "number", "required": True},
    "count": {"type": "integer", "required": True},
    "gain": {"type": "number"},
    "offset": {"type": "number"},
}


def _register_noop_flat_sequence() -> None:
    """A `capture_flat_sequence` stand-in that completes instantly with no steps."""
    _rig()
    script_store._scripts["capture_flat_sequence"] = script_store.Script(
        id="capture_flat_sequence",
        name="capture_flat_sequence",
        pausable=False,
        parameters=_FLAT_SEQUENCE_PARAMETERS,
        steps=[],
    )


def _register_failing_flat_sequence() -> None:
    """A `capture_flat_sequence` stand-in that always fails (`scriptFailed`) — references a
    role no rig ever has, so `execute_script` rejects it up front."""
    _rig()
    script_store._scripts["capture_flat_sequence"] = script_store.Script(
        id="capture_flat_sequence",
        name="capture_flat_sequence",
        pausable=False,
        parameters=_FLAT_SEQUENCE_PARAMETERS,
        steps=[
            {
                "step": "set_property",
                "role": "nonexistent-role",
                "property": "CCD_EXPOSURE",
                "elements": {"CCD_EXPOSURE_VALUE": "0"},
            }
        ],
    )


def _register_hanging_flat_sequence() -> None:
    """A `capture_flat_sequence` stand-in whose single `wait_for` never resolves — stays in
    flight until cancelled, mirroring `test_sensor_calibration_sweep.py`'s own equivalent."""
    _rig(rig_store.Component(role="mount", id="mount-1", device="Telescope Simulator"))
    script_store._scripts["capture_flat_sequence"] = script_store.Script(
        id="capture_flat_sequence",
        name="capture_flat_sequence",
        pausable=False,
        parameters=_FLAT_SEQUENCE_PARAMETERS,
        steps=[
            {
                "step": "wait_for",
                "condition": {
                    "role": "mount",
                    "property": "CONNECTION",
                    "element": "DISCONNECT",
                    "operator": "equals",
                    "value": "On",
                },
                "timeoutSeconds": 100,
            }
        ],
    )


async def _await_sweep(sweep_id: str) -> None:
    task = flat_calibration_sweep._sweeps[sweep_id].task
    assert task is not None
    await task


async def test_start_sweep_returns_started_with_total_combinations() -> None:
    _register_noop_flat_sequence()

    started = await flat_calibration_sweep.start_sweep(
        "test-rig", [50, 100], [10, 20], [1.0], "Luminance", 5000, 5
    )

    assert started["kind"] == "flatCalibrationSweepStarted"
    assert started["rigId"] == "test-rig"
    assert started["totalCombinations"] == 4
    assert started["sweepId"]

    await _await_sweep(started["sweepId"])


async def test_start_sweep_rejects_an_empty_list() -> None:
    _register_noop_flat_sequence()

    with pytest.raises(ValueError, match="non-empty|at least one"):
        await flat_calibration_sweep.start_sweep("test-rig", [], [10], [1.0], "Luminance", 5000, 5)


async def test_sweep_runs_every_combination_in_cartesian_order() -> None:
    _register_noop_flat_sequence()
    calls: list[dict[str, Any]] = []
    run_ids: list[str | None] = []
    original = script_runs.start_script

    async def spy(
        script_id: str, rig_id: str, parameters: dict[str, Any] | None = None, **kw: Any
    ) -> Any:
        calls.append(dict(parameters or {}))
        run_ids.append(kw.get("run_id"))
        return await original(script_id, rig_id, parameters, **kw)

    with patch.object(script_runs, "start_script", spy):
        started = await flat_calibration_sweep.start_sweep(
            "test-rig", [50, 100], [10, 20], [1.0, 2.0], "Luminance", 5000, 3
        )
        await _await_sweep(started["sweepId"])

    assert [(c["gain"], c["offset"], c["exposureSeconds"]) for c in calls] == [
        (50, 10, 1.0),
        (50, 10, 2.0),
        (50, 20, 1.0),
        (50, 20, 2.0),
        (100, 10, 1.0),
        (100, 10, 2.0),
        (100, 20, 1.0),
        (100, 20, 2.0),
    ]
    assert all(
        c["filterName"] == "Luminance" and c["focusPosition"] == 5000 and c["count"] == 3
        for c in calls
    )

    # Every combination is started with the sweep's own id as its run_id (matching
    # sensor_calibration_sweep's own convention) — so every frame the sweep captures, across
    # every combination, is tagged with one id and `list_frames(run_id=sweepId)` retrieves all
    # of them.
    assert run_ids == [started["sweepId"]] * 8

    status = flat_calibration_sweep.get_sweep_status(started["sweepId"])
    assert status["kind"] == "flatCalibrationSweepCompleted"
    completed = cast(flat_calibration_sweep.FlatCalibrationSweepCompleted, status)
    assert len(completed["results"]) == 8
    assert all(r["status"]["kind"] == "scriptCompleted" for r in completed["results"])
    assert all(r["runId"] == started["sweepId"] for r in completed["results"])


async def test_sweep_reports_running_progress_and_partial_results() -> None:
    _register_noop_flat_sequence()

    started = await flat_calibration_sweep.start_sweep(
        "test-rig", [50], [10], [1.0, 2.0, 3.0], "Luminance", 5000, 5
    )
    await _await_sweep(started["sweepId"])

    status = flat_calibration_sweep.get_sweep_status(started["sweepId"])
    assert status["kind"] == "flatCalibrationSweepCompleted"
    completed = cast(flat_calibration_sweep.FlatCalibrationSweepCompleted, status)
    assert [r["exposureSeconds"] for r in completed["results"]] == [1.0, 2.0, 3.0]


async def test_sweep_stops_at_the_first_failed_combination() -> None:
    _register_failing_flat_sequence()

    started = await flat_calibration_sweep.start_sweep(
        "test-rig", [50, 100], [10, 20], [1.0], "Luminance", 5000, 5
    )
    await _await_sweep(started["sweepId"])

    status = flat_calibration_sweep.get_sweep_status(started["sweepId"])
    assert status["kind"] == "flatCalibrationSweepFailed"
    failed = cast(flat_calibration_sweep.FlatCalibrationSweepFailed, status)
    assert failed["failedAtCombination"] == 0
    assert len(failed["results"]) == 1
    assert failed["results"][0]["status"]["kind"] == "scriptFailed"


async def test_cancel_sweep_stops_the_in_flight_combination_and_reports_cancelled() -> None:
    _register_hanging_flat_sequence()

    started = await flat_calibration_sweep.start_sweep(
        "test-rig", [50, 100], [10], [1.0], "Luminance", 5000, 5
    )
    # Give the sweep task a tick to actually start the first combination's run.
    await asyncio.sleep(0)

    status = await asyncio.wait_for(
        flat_calibration_sweep.cancel_sweep(started["sweepId"]), timeout=2
    )

    assert status["kind"] == "flatCalibrationSweepCancelled"
    cancelled = cast(flat_calibration_sweep.FlatCalibrationSweepCancelled, status)
    assert cancelled["cancelledAtCombination"] == 0
    # The in-flight combination's own outcome is still recorded, even though it was the one
    # cancelled — `cancel_script` reports it as `scriptCancelled`, not silently dropped.
    assert len(cancelled["results"]) == 1
    assert cancelled["results"][0]["status"]["kind"] == "scriptCancelled"
    # The second combination was never started.
    assert cancelled["results"][0]["gain"] == 50

    # get_sweep_status agrees with what cancel_sweep returned.
    assert flat_calibration_sweep.get_sweep_status(started["sweepId"]) == status


async def test_get_sweep_status_raises_for_unknown_sweep_id() -> None:
    with pytest.raises(ValueError, match="unknown-sweep"):
        flat_calibration_sweep.get_sweep_status("unknown-sweep")


async def test_cancel_sweep_raises_for_unknown_sweep_id() -> None:
    with pytest.raises(ValueError, match="unknown-sweep"):
        await flat_calibration_sweep.cancel_sweep("unknown-sweep")


async def test_evicts_the_oldest_finished_sweeps_once_over_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_sweeps` must not grow without bound on a long-lived server — once more than
    `_MAX_FINISHED_SWEEPS` terminal sweeps are on file, the oldest ones are dropped."""
    monkeypatch.setattr(flat_calibration_sweep, "_MAX_FINISHED_SWEEPS", 2)
    _register_noop_flat_sequence()

    sweep_ids = []
    for _ in range(3):
        started = await flat_calibration_sweep.start_sweep(
            "test-rig", [50], [10], [1.0], "Luminance", 5000, 5
        )
        await _await_sweep(started["sweepId"])
        sweep_ids.append(started["sweepId"])

    # The oldest finished sweep was evicted once the 3rd pushed the count over the cap.
    with pytest.raises(ValueError, match=sweep_ids[0]):
        flat_calibration_sweep.get_sweep_status(sweep_ids[0])
    assert flat_calibration_sweep.get_sweep_status(sweep_ids[1])["kind"] == (
        "flatCalibrationSweepCompleted"
    )
    assert flat_calibration_sweep.get_sweep_status(sweep_ids[2])["kind"] == (
        "flatCalibrationSweepCompleted"
    )


async def test_does_not_evict_an_in_flight_sweep_regardless_of_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An in-flight sweep is never evicted, even if the cap is already exceeded by other,
    finished sweeps — only terminal sweeps are ever eligible for eviction."""
    monkeypatch.setattr(flat_calibration_sweep, "_MAX_FINISHED_SWEEPS", 0)
    _register_hanging_flat_sequence()

    # Left running — this is the in-flight sweep that must survive eviction.
    in_flight = await flat_calibration_sweep.start_sweep(
        "test-rig", [50], [10], [1.0], "Luminance", 5000, 5
    )
    await asyncio.sleep(0)

    # Two sweeps that reach a terminal state (cancelled) — enough to trigger eviction against
    # a cap of 0, which evicts every terminal sweep on file.
    for _ in range(2):
        started = await flat_calibration_sweep.start_sweep(
            "test-rig", [50], [10], [1.0], "Luminance", 5000, 5
        )
        await asyncio.sleep(0)
        await asyncio.wait_for(flat_calibration_sweep.cancel_sweep(started["sweepId"]), timeout=2)

    assert (
        flat_calibration_sweep.get_sweep_status(in_flight["sweepId"])["kind"]
        == "flatCalibrationSweepProgress"
    )

    # Clean up the still-running sweep so it doesn't outlive the test.
    await asyncio.wait_for(flat_calibration_sweep.cancel_sweep(in_flight["sweepId"]), timeout=2)
