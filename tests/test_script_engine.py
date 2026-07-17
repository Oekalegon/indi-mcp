import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from indi_mcp import indi_messaging, rig_store, script_engine, script_store


@pytest.fixture(autouse=True)
def _reset_stores() -> None:
    rig_store._rigs = {}
    script_store._scripts = {}


@pytest.fixture(autouse=True)
def _mock_indi_messaging_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test drives `execute_script`, which always calls `list_devices`/`check_rig`.

    Default to "nothing connected, rig is fine" so tests that only care
    about step execution don't each need to stub this out themselves;
    tests exercising the missing-device warning path override `check_rig`.
    """
    monkeypatch.setattr(indi_messaging, "list_devices", lambda: [])


def _rig(*components: rig_store.Component, rig_id: str = "test-rig") -> rig_store.Rig:
    rig = rig_store.Rig(id=rig_id, name="Test rig", components=list(components))
    rig_store._rigs[rig.id] = rig
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


def _wait_for(role: str, property: str, operator: str, value: Any, timeout: float = 5) -> dict:
    return {
        "step": "wait_for",
        "condition": {"role": role, "property": property, "operator": operator, "value": value},
        "timeoutSeconds": timeout,
    }


async def test_execute_script_runs_set_property_against_resolved_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "cool",
        steps=[
            _set_property("camera", "CCD_TEMPERATURE", {"CCD_TEMPERATURE_VALUE": "-10"}),
        ],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)

    result = await script_engine.execute_script("cool", "test-rig", {})

    send_property.assert_awaited_once_with(
        "CCD Simulator", "CCD_TEMPERATURE", {"CCD_TEMPERATURE_VALUE": "-10"}
    )
    assert result == {"scriptId": "cool", "stepsExecuted": 1}


async def test_execute_script_substitutes_parameter_references(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "cool",
        parameters={"targetTempC": {"type": "number", "required": True}},
        steps=[
            _set_property(
                "camera", "CCD_TEMPERATURE", {"CCD_TEMPERATURE_VALUE": "{{ targetTempC }}"}
            ),
        ],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)

    await script_engine.execute_script("cool", "test-rig", {"targetTempC": -15})

    send_property.assert_awaited_once_with(
        "CCD Simulator", "CCD_TEMPERATURE", {"CCD_TEMPERATURE_VALUE": "-15"}
    )


async def test_execute_script_raises_on_missing_required_parameter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig()
    _script("cool", parameters={"targetTempC": {"type": "number", "required": True}})

    with pytest.raises(script_engine.ScriptValidationError, match="targetTempC"):
        await script_engine.execute_script("cool", "test-rig", {})


async def test_execute_script_raises_on_undeclared_parameter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig()
    _script("cool")

    with pytest.raises(script_engine.ScriptValidationError, match="undeclared"):
        await script_engine.execute_script("cool", "test-rig", {"bogus": 1})


async def test_execute_script_raises_when_role_has_no_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig()
    _script("cool", steps=[_set_property("camera", "CCD_TEMPERATURE", {"X": "1"})])

    with pytest.raises(script_engine.ScriptValidationError, match="camera"):
        await script_engine.execute_script("cool", "test-rig", {})


async def test_execute_script_raises_when_matching_component_has_no_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="telescope", id="main-scope", apertureMm=200))
    _script("cool", steps=[_set_property("telescope", "SOME_PROP", {"X": "1"})])

    with pytest.raises(script_engine.ScriptValidationError, match="telescope"):
        await script_engine.execute_script("cool", "test-rig", {})


async def test_execute_script_raises_when_role_is_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(
        rig_store.Component(role="guideCamera", id="cam-a", device="Camera A"),
        rig_store.Component(role="guideCamera", id="cam-b", device="Camera B"),
    )
    _script("cool", steps=[_set_property("guideCamera", "CCD_EXPOSURE", {"X": "1"})])

    with pytest.raises(script_engine.ScriptValidationError, match="ambiguous"):
        await script_engine.execute_script("cool", "test-rig", {})


async def test_execute_script_warns_but_does_not_fail_on_missing_devices(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script("cool", steps=[_set_property("camera", "CCD_TEMPERATURE", {"X": "1"})])
    monkeypatch.setattr(indi_messaging, "send_property", AsyncMock())
    monkeypatch.setattr(
        rig_store,
        "check_rig",
        lambda rig_id, connected: {"ok": False, "missing": ["cam-1"], "present": []},
    )

    with caplog.at_level("WARNING"):
        await script_engine.execute_script("cool", "test-rig", {})

    assert any("missing device" in record.message for record in caplog.records)


async def test_execute_script_wait_for_succeeds_once_condition_is_met(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "wait",
        steps=[_wait_for("camera", "CCD_TEMPERATURE", "equals", "Ok")],
    )
    states = iter(["Busy", "Busy", "Ok"])
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: next(states))
    monkeypatch.setattr(script_engine, "_WAIT_POLL_INTERVAL_SECONDS", 0.001)

    result = await script_engine.execute_script("wait", "test-rig", {})

    assert result["stepsExecuted"] == 1


async def test_execute_script_wait_for_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script("wait", steps=[_wait_for("camera", "CCD_TEMPERATURE", "equals", "Ok", timeout=0.01)])
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Busy")
    monkeypatch.setattr(script_engine, "_WAIT_POLL_INTERVAL_SECONDS", 0.001)

    with pytest.raises(script_engine.ScriptExecutionError, match="timed out"):
        await script_engine.execute_script("wait", "test-rig", {})


async def test_execute_script_wait_for_compares_a_numeric_element(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "wait",
        steps=[
            {
                "step": "wait_for",
                "condition": {
                    "role": "camera",
                    "property": "CCD_TEMPERATURE",
                    "element": "CCD_TEMPERATURE_VALUE",
                    "operator": "lessThanOrEqual",
                    "value": -10,
                },
                "timeoutSeconds": 5,
            }
        ],
    )
    monkeypatch.setattr(
        indi_messaging,
        "get_property_values",
        lambda device, name: {"CCD_TEMPERATURE_VALUE": "-12.5"},
    )

    result = await script_engine.execute_script("wait", "test-rig", {})

    assert result["stepsExecuted"] == 1


async def test_execute_script_if_runs_then_branch_when_condition_met(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "conditional",
        steps=[
            {
                "step": "if",
                "condition": {
                    "role": "camera",
                    "property": "CONNECTION",
                    "operator": "equals",
                    "value": "On",
                },
                "then": [_set_property("camera", "CCD_EXPOSURE", {"X": "then"})],
                "else": [_set_property("camera", "CCD_EXPOSURE", {"X": "else"})],
            }
        ],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "On")

    await script_engine.execute_script("conditional", "test-rig", {})

    send_property.assert_awaited_once_with("CCD Simulator", "CCD_EXPOSURE", {"X": "then"})


async def test_execute_script_repeat_count_runs_the_right_number_of_times(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "repeat-count",
        steps=[
            {
                "step": "repeat",
                "count": 3,
                "steps": [_set_property("camera", "CCD_EXPOSURE", {"X": "1"})],
            }
        ],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)

    result = await script_engine.execute_script("repeat-count", "test-rig", {})

    assert send_property.await_count == 3
    # +1 for the `repeat` step itself: stepsExecuted counts every dispatched
    # step, including container steps like `repeat`/`run_script`, not just
    # leaf work.
    assert result["stepsExecuted"] == 4


async def test_execute_script_repeat_honors_every(monkeypatch: pytest.MonkeyPatch) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "repeat-every",
        steps=[
            {
                "step": "repeat",
                "count": 6,
                "steps": [_set_property("camera", "CCD_EXPOSURE", {"X": "1"}, every=2)],
            }
        ],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)

    result = await script_engine.execute_script("repeat-every", "test-rig", {})

    assert send_property.await_count == 3
    assert result["stepsExecuted"] == 4  # +1 for the `repeat` step itself


async def test_execute_script_repeat_until_stops_once_condition_is_met(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "repeat-until",
        steps=[
            {
                "step": "repeat",
                "until": {
                    "role": "camera",
                    "property": "CCD_TEMPERATURE",
                    "operator": "equals",
                    "value": "Ok",
                },
                "maxIterations": 10,
                "steps": [_set_property("camera", "CCD_EXPOSURE", {"X": "1"})],
            }
        ],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)
    states = iter(["Busy", "Busy", "Ok"])
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: next(states))

    await script_engine.execute_script("repeat-until", "test-rig", {})

    assert send_property.await_count == 3


async def test_execute_script_repeat_until_raises_when_max_iterations_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "repeat-until",
        steps=[
            {
                "step": "repeat",
                "until": {
                    "role": "camera",
                    "property": "CCD_TEMPERATURE",
                    "operator": "equals",
                    "value": "Ok",
                },
                "maxIterations": 2,
                "steps": [_set_property("camera", "CCD_EXPOSURE", {"X": "1"})],
            }
        ],
    )
    monkeypatch.setattr(indi_messaging, "send_property", AsyncMock())
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Busy")

    with pytest.raises(script_engine.ScriptExecutionError, match="maxIterations"):
        await script_engine.execute_script("repeat-until", "test-rig", {})


async def test_execute_script_run_script_recurses_with_substituted_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "cool_camera",
        parameters={"targetTempC": {"type": "number", "required": True}},
        steps=[
            _set_property(
                "camera", "CCD_TEMPERATURE", {"CCD_TEMPERATURE_VALUE": "{{ targetTempC }}"}
            )
        ],
    )
    _script(
        "capture_sequence",
        parameters={"coolTo": {"type": "number", "required": True}},
        steps=[
            {
                "step": "run_script",
                "script": "cool_camera",
                "parameters": {"targetTempC": "{{ coolTo }}"},
            }
        ],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)

    result = await script_engine.execute_script("capture_sequence", "test-rig", {"coolTo": -20})

    send_property.assert_awaited_once_with(
        "CCD Simulator", "CCD_TEMPERATURE", {"CCD_TEMPERATURE_VALUE": "-20"}
    )
    assert result["stepsExecuted"] == 2  # the run_script step itself + the substituted set_property


async def test_execute_script_capture_frame_is_a_stub_with_no_indi_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "capture",
        steps=[
            {
                "step": "capture_frame",
                "role": "camera",
                "exposureSeconds": 30,
                "frameType": "Light",
            }
        ],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)

    result = await script_engine.execute_script("capture", "test-rig", {})

    send_property.assert_not_awaited()
    assert result["stepsExecuted"] == 1


async def test_execute_script_slew_is_a_stub_with_no_indi_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="mount", id="mount-1", device="Telescope Simulator"))
    _script(
        "slew",
        steps=[
            {
                "step": "slew",
                "role": "mount",
                "target": {"objectName": "M101"},
            }
        ],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)

    result = await script_engine.execute_script("slew", "test-rig", {})

    send_property.assert_not_awaited()
    assert result["stepsExecuted"] == 1


async def test_execute_script_reports_progress_for_each_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "two-steps",
        steps=[
            _set_property("camera", "CCD_EXPOSURE", {"X": "1"}, description="first"),
            _set_property("camera", "CCD_EXPOSURE", {"X": "2"}, description="second"),
        ],
    )
    monkeypatch.setattr(indi_messaging, "send_property", AsyncMock())
    progress: list[script_engine.ScriptProgress] = []

    await script_engine.execute_script("two-steps", "test-rig", {}, on_progress=progress.append)

    assert [p["message"] for p in progress] == ["first", "second"]
    assert [p["stepsExecuted"] for p in progress] == [1, 2]
    assert [p["totalSteps"] for p in progress] == [2, 2]


async def test_execute_script_progress_message_is_none_without_a_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script("undescribed", steps=[_set_property("camera", "CCD_EXPOSURE", {"X": "1"})])
    monkeypatch.setattr(indi_messaging, "send_property", AsyncMock())
    progress: list[script_engine.ScriptProgress] = []

    await script_engine.execute_script("undescribed", "test-rig", {}, on_progress=progress.append)

    assert progress[0]["message"] is None


async def test_execute_script_total_steps_counts_a_fixed_count_repeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "repeat-count",
        steps=[
            {
                "step": "repeat",
                "count": 3,
                "steps": [_set_property("camera", "CCD_EXPOSURE", {"X": "1"})],
            }
        ],
    )
    monkeypatch.setattr(indi_messaging, "send_property", AsyncMock())
    progress: list[script_engine.ScriptProgress] = []

    await script_engine.execute_script("repeat-count", "test-rig", {}, on_progress=progress.append)

    # 1 (the repeat step itself) + 3 * 1 (its body, once per iteration)
    assert progress[0]["totalSteps"] == 4


async def test_execute_script_total_steps_is_none_when_a_repeat_until_is_in_reach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "repeat-until",
        steps=[
            {
                "step": "repeat",
                "until": {
                    "role": "camera",
                    "property": "CCD_TEMPERATURE",
                    "operator": "equals",
                    "value": "Ok",
                },
                "maxIterations": 10,
                "steps": [_set_property("camera", "CCD_EXPOSURE", {"X": "1"})],
            }
        ],
    )
    monkeypatch.setattr(indi_messaging, "send_property", AsyncMock())
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Ok")
    progress: list[script_engine.ScriptProgress] = []

    await script_engine.execute_script("repeat-until", "test-rig", {}, on_progress=progress.append)

    assert progress[0]["totalSteps"] is None


async def test_execute_script_total_steps_counts_through_run_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "callee",
        steps=[_set_property("camera", "CCD_EXPOSURE", {"X": "1"})],
    )
    _script(
        "caller",
        steps=[{"step": "run_script", "script": "callee"}],
    )
    monkeypatch.setattr(indi_messaging, "send_property", AsyncMock())
    progress: list[script_engine.ScriptProgress] = []

    await script_engine.execute_script("caller", "test-rig", {}, on_progress=progress.append)

    # 1 (run_script step) + 1 (the callee's own set_property step)
    assert progress[0]["totalSteps"] == 2


async def test_execute_script_total_steps_is_none_when_if_branches_have_different_lengths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "conditional",
        steps=[
            {
                "step": "if",
                "condition": {
                    "role": "camera",
                    "property": "CONNECTION",
                    "operator": "equals",
                    "value": "On",
                },
                "then": [_set_property("camera", "CCD_EXPOSURE", {"X": "1"})],
                "else": [
                    _set_property("camera", "CCD_EXPOSURE", {"X": "1"}),
                    _set_property("camera", "CCD_EXPOSURE", {"X": "2"}),
                ],
            }
        ],
    )
    monkeypatch.setattr(indi_messaging, "send_property", AsyncMock())
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "On")
    progress: list[script_engine.ScriptProgress] = []

    await script_engine.execute_script("conditional", "test-rig", {}, on_progress=progress.append)

    assert progress[0]["totalSteps"] is None


async def test_execute_script_total_steps_is_exact_when_if_branches_match_in_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "conditional",
        steps=[
            {
                "step": "if",
                "condition": {
                    "role": "camera",
                    "property": "CONNECTION",
                    "operator": "equals",
                    "value": "On",
                },
                "then": [_set_property("camera", "CCD_EXPOSURE", {"X": "1"})],
                "else": [_set_property("camera", "CCD_EXPOSURE", {"X": "2"})],
            }
        ],
    )
    monkeypatch.setattr(indi_messaging, "send_property", AsyncMock())
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "On")
    progress: list[script_engine.ScriptProgress] = []

    await script_engine.execute_script("conditional", "test-rig", {}, on_progress=progress.append)

    # 1 (the if step itself) + 1 (whichever single-step branch runs)
    assert progress[0]["totalSteps"] == 2


async def test_execute_script_cancel_event_stops_a_run_mid_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "many-steps",
        steps=[_set_property("camera", "CCD_EXPOSURE", {"X": str(i)}) for i in range(5)],
    )
    monkeypatch.setattr(indi_messaging, "send_property", AsyncMock())
    cancel_event = asyncio.Event()

    def on_progress(progress: script_engine.ScriptProgress) -> None:
        if progress["stepsExecuted"] == 2:
            cancel_event.set()

    with pytest.raises(script_engine.ScriptCancelled):
        await script_engine.execute_script(
            "many-steps", "test-rig", {}, cancel_event=cancel_event, on_progress=on_progress
        )


async def test_execute_script_pausable_script_honors_pause_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "pausable",
        pausable=True,
        steps=[
            _set_property("camera", "CCD_EXPOSURE", {"X": "1"}),
            _set_property("camera", "CCD_EXPOSURE", {"X": "2"}),
        ],
    )
    monkeypatch.setattr(indi_messaging, "send_property", AsyncMock())
    monkeypatch.setattr(script_engine, "_PAUSE_POLL_INTERVAL_SECONDS", 0.001)
    pause_event = asyncio.Event()
    pause_event.set()
    resumed = False

    async def clear_pause_soon() -> None:
        nonlocal resumed
        await asyncio.sleep(0.01)
        resumed = True
        pause_event.clear()

    clearer = asyncio.create_task(clear_pause_soon())
    await script_engine.execute_script("pausable", "test-rig", {}, pause_event=pause_event)
    await clearer

    assert resumed is True


async def test_execute_script_non_pausable_script_ignores_pause_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "not-pausable",
        pausable=False,
        steps=[_set_property("camera", "CCD_EXPOSURE", {"X": "1"})],
    )
    monkeypatch.setattr(indi_messaging, "send_property", AsyncMock())
    pause_event = asyncio.Event()
    pause_event.set()

    result = await asyncio.wait_for(
        script_engine.execute_script("not-pausable", "test-rig", {}, pause_event=pause_event),
        timeout=1,
    )

    assert result["stepsExecuted"] == 1


def test_step_handlers_covers_every_closed_step_type() -> None:
    step_types = {
        script_store.SetPropertyStep,
        script_store.WaitForStep,
        script_store.CaptureFrameStep,
        script_store.SlewStep,
        script_store.RunScriptStep,
        script_store.RepeatStep,
        script_store.IfStep,
    }

    assert set(script_engine.STEP_HANDLERS) == step_types


async def test_run_one_step_rejects_a_step_type_with_no_registered_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = script_engine._ExecutionContext(
        role_to_device={},
        cancel_event=None,
        pause_event=None,
        on_progress=None,
        total_steps=None,
        scripts={},
    )

    class _UnregisteredStep:
        description = None
        every = None

    with pytest.raises(script_engine.ScriptValidationError, match="no handler registered"):
        await script_engine._run_one_step(
            cast(script_store.Step, _UnregisteredStep()), ctx, {}, "script", False
        )


async def test_execute_script_run_script_resolves_from_the_runs_own_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run_script callee resolves from the snapshot taken at run start, not the live store.

    Simulates a concurrent script_store.load_scripts()/reload happening
    partway through a run (here, during a wait_for's poll loop) that
    removes the callee from the live store entirely. Without snapshotting
    (ctx.scripts), the later run_script step would raise "Unknown script"
    even though this run already validated and started before the reload.
    """
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script("callee", steps=[_set_property("camera", "CCD_EXPOSURE", {"X": "1"})])
    _script(
        "caller",
        steps=[
            _wait_for("camera", "CCD_TEMPERATURE", "equals", "Ok", timeout=5),
            {"step": "run_script", "script": "callee"},
        ],
    )
    monkeypatch.setattr(indi_messaging, "send_property", AsyncMock())
    monkeypatch.setattr(script_engine, "_WAIT_POLL_INTERVAL_SECONDS", 0.001)
    states = iter(["Busy", "Ok"])

    def fake_get_property_state(device: str, name: str) -> str:
        state = next(states)
        if state == "Ok":
            # Simulate a concurrent reload dropping "callee" from the live
            # store while this run is already in progress.
            del script_store._scripts["callee"]
        return state

    monkeypatch.setattr(indi_messaging, "get_property_state", fake_get_property_state)

    result = await script_engine.execute_script("caller", "test-rig", {})

    # wait_for + run_script + the callee's own set_property step
    assert result["stepsExecuted"] == 3
