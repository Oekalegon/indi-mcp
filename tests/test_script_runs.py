import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from indi_mcp import indi_messaging, rig_store, script_runs, script_store

_known_devices: list[str] = []


@pytest.fixture(autouse=True)
def _reset_stores() -> None:
    rig_store._rigs = {}
    script_store._scripts = {}
    script_runs._runs = {}
    _known_devices.clear()


def _default_get_property_values(device: str, name: str) -> dict[str, str] | None:
    if name == "CONNECTION":
        return {"CONNECT": "On", "DISCONNECT": "Off"}
    return None


@pytest.fixture(autouse=True)
def _mock_indi_messaging_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(indi_messaging, "list_devices", lambda: list(_known_devices))
    monkeypatch.setattr(indi_messaging, "get_property_values", _default_get_property_values)


def _rig(*components: rig_store.Component, rig_id: str = "test-rig") -> rig_store.Rig:
    rig = rig_store.Rig(id=rig_id, name="Test rig", components=list(components))
    rig_store._rigs[rig.id] = rig
    _known_devices.extend(c.device for c in components if c.device is not None)
    return rig


def _script(script_id: str, **fields: Any) -> script_store.Script:
    fields.setdefault("name", script_id)
    fields.setdefault("pausable", False)
    fields.setdefault("steps", [])
    script = script_store.Script(id=script_id, **fields)
    script_store._scripts[script.id] = script
    return script


def _set_property(role: str, property: str, elements: dict[str, str], **extra: Any) -> dict:
    return {
        "step": "set_property",
        "role": role,
        "property": property,
        "elements": elements,
        **extra,
    }


def _wait_for(
    role: str,
    property: str,
    operator: str,
    value: Any,
    timeout: float = 5,
    element: str | None = None,
) -> dict:
    condition: dict[str, Any] = {
        "role": role,
        "property": property,
        "operator": operator,
        "value": value,
    }
    if element is not None:
        condition["element"] = element
    return {"step": "wait_for", "condition": condition, "timeoutSeconds": timeout}


async def _await_run(run_id: str) -> None:
    """Await the background task tracking `run_id`, so it can't outlive its test."""
    task = script_runs._runs[run_id].task
    assert task is not None
    await task


async def test_start_script_returns_scriptStarted_with_rig_and_pausable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "cool",
        pausable=True,
        steps=[_set_property("camera", "CCD_TEMPERATURE", {"CCD_TEMPERATURE_VALUE": "-10"})],
    )
    monkeypatch.setattr(indi_messaging, "send_property", AsyncMock())

    started = await script_runs.start_script("cool", "test-rig", {})

    assert started["kind"] == "scriptStarted"
    assert started["script"] == "cool"
    assert started["rigId"] == "test-rig"
    assert started["pausable"] is True
    assert started["runId"]

    # Let the background task finish so it doesn't outlive the test.
    await _await_run(started["runId"])


async def test_start_script_raises_for_unknown_script_id() -> None:
    with pytest.raises(ValueError, match="cool"):
        await script_runs.start_script("cool", "test-rig", {})


async def test_run_completes_and_get_script_status_reports_scriptCompleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "cool",
        steps=[_set_property("camera", "CCD_TEMPERATURE", {"CCD_TEMPERATURE_VALUE": "-10"})],
    )
    monkeypatch.setattr(indi_messaging, "send_property", AsyncMock())

    started = await script_runs.start_script("cool", "test-rig", {})
    await _await_run(started["runId"])

    status = script_runs.get_script_status(started["runId"])

    assert status["kind"] == "scriptCompleted"
    completed = cast(script_runs.ScriptRunCompleted, status)
    assert completed["runId"] == started["runId"]
    assert completed["rigId"] == "test-rig"
    assert completed["result"] == {"scriptId": "cool", "stepsExecuted": 1}
    assert "finishedAt" in completed


async def test_run_that_fails_reports_scriptFailed(monkeypatch: pytest.MonkeyPatch) -> None:
    _rig(rig_store.Component(role="mount", id="mount-1", device="Telescope Simulator"))
    _script("connect", steps=[_set_property("camera", "CONNECTION", {"CONNECT": "On"})])
    monkeypatch.setattr(indi_messaging, "send_property", AsyncMock())

    started = await script_runs.start_script("connect", "test-rig", {})
    await _await_run(started["runId"])

    status = script_runs.get_script_status(started["runId"])

    assert status["kind"] == "scriptFailed"
    failed = cast(script_runs.ScriptRunFailed, status)
    assert failed["rigId"] == "test-rig"
    assert failed["failedAtStep"] == 0
    assert "camera" in failed["error"]["message"]


async def test_get_script_status_raises_for_unknown_run_id() -> None:
    with pytest.raises(ValueError, match="does-not-exist"):
        script_runs.get_script_status("does-not-exist")


async def test_cancel_script_stops_a_running_script_and_reports_scriptCancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `wait_for` whose condition never holds loops until cancelled, checking
    `cancel_event` every poll interval — cancelling should stop it quickly rather
    than waiting for its (long) timeout."""
    _rig(rig_store.Component(role="mount", id="mount-1", device="Telescope Simulator"))
    _script(
        "wait_forever",
        steps=[_wait_for("mount", "CONNECTION", "equals", "On", element="DISCONNECT", timeout=100)],
    )

    started = await script_runs.start_script("wait_forever", "test-rig", {})

    status = await asyncio.wait_for(script_runs.cancel_script(started["runId"]), timeout=2)

    assert status["kind"] == "scriptCancelled"
    cancelled = cast(script_runs.ScriptRunCancelled, status)
    assert cancelled["runId"] == started["runId"]
    assert cancelled["rigId"] == "test-rig"
    assert cancelled["cancelledAtStep"] == 0

    # get_script_status agrees with what cancel_script returned.
    assert script_runs.get_script_status(started["runId"]) == status


async def test_pause_script_rejects_when_script_is_not_pausable() -> None:
    _rig(rig_store.Component(role="mount", id="mount-1", device="Telescope Simulator"))
    _script(
        "wait_forever",
        pausable=False,
        steps=[_wait_for("mount", "CONNECTION", "equals", "On", element="DISCONNECT", timeout=100)],
    )

    started = await script_runs.start_script("wait_forever", "test-rig", {})

    result = script_runs.pause_script(started["runId"])

    assert result["kind"] == "scriptPauseRejected"
    rejected = cast(script_runs.ScriptRunPauseRejected, result)
    assert rejected["runId"] == started["runId"]
    assert rejected["rigId"] == "test-rig"
    assert rejected["reason"] == "This script has no safe point to pause at"

    # Clean up the still-running background task.
    await script_runs.cancel_script(started["runId"])


async def test_pause_script_rejects_and_does_not_clobber_an_already_finished_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pausing a run that already completed must not overwrite its recorded outcome —
    otherwise get_script_status would lose the real scriptCompleted result."""
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "cool",
        pausable=True,
        steps=[_set_property("camera", "CCD_TEMPERATURE", {"CCD_TEMPERATURE_VALUE": "-10"})],
    )
    monkeypatch.setattr(indi_messaging, "send_property", AsyncMock())

    started = await script_runs.start_script("cool", "test-rig", {})
    await _await_run(started["runId"])
    completed_status = script_runs.get_script_status(started["runId"])
    assert completed_status["kind"] == "scriptCompleted"

    result = script_runs.pause_script(started["runId"])

    assert result["kind"] == "scriptPauseRejected"
    rejected = cast(script_runs.ScriptRunPauseRejected, result)
    assert rejected["reason"] == "This run has already finished"

    # The run's real outcome must still be there afterward, untouched.
    assert script_runs.get_script_status(started["runId"]) == completed_status


async def test_resume_script_rejects_and_does_not_clobber_an_already_finished_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "cool",
        pausable=True,
        steps=[_set_property("camera", "CCD_TEMPERATURE", {"CCD_TEMPERATURE_VALUE": "-10"})],
    )
    monkeypatch.setattr(indi_messaging, "send_property", AsyncMock())

    started = await script_runs.start_script("cool", "test-rig", {})
    await _await_run(started["runId"])
    completed_status = script_runs.get_script_status(started["runId"])

    result = script_runs.resume_script(started["runId"])

    assert result["kind"] == "scriptPauseRejected"
    rejected = cast(script_runs.ScriptRunPauseRejected, result)
    assert rejected["reason"] == "This run has already finished"
    assert script_runs.get_script_status(started["runId"]) == completed_status


async def test_resume_script_rejects_when_script_is_not_pausable() -> None:
    _rig(rig_store.Component(role="mount", id="mount-1", device="Telescope Simulator"))
    _script(
        "wait_forever",
        pausable=False,
        steps=[_wait_for("mount", "CONNECTION", "equals", "On", element="DISCONNECT", timeout=100)],
    )

    started = await script_runs.start_script("wait_forever", "test-rig", {})

    rejected = script_runs.resume_script(started["runId"])

    assert rejected["kind"] == "scriptPauseRejected"
    await script_runs.cancel_script(started["runId"])


async def test_pause_then_resume_a_pausable_script_lets_it_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pausing right after `start_script` (before the task has run any code) freezes it
    before its first step; resuming lets it proceed and finish."""
    _rig(rig_store.Component(role="mount", id="mount-1", device="Telescope Simulator"))
    _script(
        "wait_connected",
        pausable=True,
        steps=[_wait_for("mount", "CONNECTION", "equals", "On", element="CONNECT", timeout=5)],
    )

    started = await script_runs.start_script("wait_connected", "test-rig", {})

    pause_result = script_runs.pause_script(started["runId"])
    assert pause_result["kind"] == "scriptPaused"
    paused = cast(script_runs.ScriptRunPaused, pause_result)
    assert paused["rigId"] == "test-rig"
    assert paused["pausedAtStep"] == 0

    # Give the background task a moment to actually reach the pause loop.
    await asyncio.sleep(0.05)
    assert script_runs.get_script_status(started["runId"])["kind"] != "scriptCompleted"

    resume_result = script_runs.resume_script(started["runId"])
    assert resume_result["kind"] == "scriptResumed"
    resumed = cast(script_runs.ScriptRunResumed, resume_result)
    assert resumed["resumedAtStep"] == 0

    await asyncio.wait_for(_await_run(started["runId"]), timeout=2)
    status = script_runs.get_script_status(started["runId"])
    assert status["kind"] == "scriptCompleted"
