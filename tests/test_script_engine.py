import asyncio
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from indi_mcp import frame_store, indi_messaging, rig_store, script_engine, script_store

_known_devices: list[str] = []


@pytest.fixture(autouse=True)
def _reset_stores() -> None:
    rig_store._rigs = {}
    script_store._scripts = {}
    _known_devices.clear()


def _default_get_property_values(device: str, name: str) -> dict[str, str] | None:
    """Every device is reported connected by default; every other property is undefined."""
    if name == "CONNECTION":
        return {"CONNECT": "On", "DISCONNECT": "Off"}
    return None


@pytest.fixture(autouse=True)
def _mock_indi_messaging_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test drives `execute_script`, which always calls `list_devices`/`check_rig`.

    Default to "every device `_rig()` registers is known to indiserver and
    reports connected, rig is fine, no other property values defined" so
    tests that only care about one specific bit of step execution don't
    each need to stub all of this out themselves; tests exercising the
    missing-device warning path override `check_rig`, tests exercising a
    device unknown to indiserver entirely override `list_devices`, tests
    exercising the not-connected check override `get_property_values` for
    `CONNECTION` specifically, and tests exercising a specific property's
    values (e.g. `TELESCOPE_PARK`, a `wait_for` condition's element)
    override `get_property_values` for that property while still
    delegating to `_default_get_property_values` for anything else (see
    those tests). `get_property_state` defaults to `"Idle"` — never
    `"Alert"` — so a `wait_for`/`slew`/`capture_frame` test that doesn't
    care about vector state (e.g. one only exercising an element-based
    `Condition`) doesn't spuriously trip the Alert fast-fail while it
    polls; tests exercising vector state directly override it themselves.
    """
    monkeypatch.setattr(indi_messaging, "list_devices", lambda: list(_known_devices))
    monkeypatch.setattr(indi_messaging, "get_property_values", _default_get_property_values)
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Idle")


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
    assert result == {"scriptId": "cool", "stepsExecuted": 1, "framesCaptured": 0}


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


async def test_execute_script_substitutes_a_parameterized_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A step's `role` may itself be a `"{{ paramName }}"` reference, resolved up front
    (before any step runs) against the run's own concrete parameter values — not just a
    literal, as a single generic connect/disconnect script needs."""
    _rig(
        rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"),
        rig_store.Component(role="mount", id="mount-1", device="Telescope Simulator"),
    )
    _script(
        "connect",
        parameters={"role": {"type": "string", "required": True}},
        steps=[_set_property("{{ role }}", "CONNECTION", {"CONNECT": "On"})],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)

    await script_engine.execute_script("connect", "test-rig", {"role": "mount"})

    send_property.assert_awaited_once_with("Telescope Simulator", "CONNECTION", {"CONNECT": "On"})


async def test_execute_script_raises_when_parameterized_role_has_no_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bad parameterized role fails before any step runs, same as a bad literal role."""
    _rig(rig_store.Component(role="mount", id="mount-1", device="Telescope Simulator"))
    _script(
        "connect",
        parameters={"role": {"type": "string", "required": True}},
        steps=[_set_property("{{ role }}", "CONNECTION", {"CONNECT": "On"})],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)

    with pytest.raises(script_engine.ScriptValidationError, match="camera"):
        await script_engine.execute_script("connect", "test-rig", {"role": "camera"})

    send_property.assert_not_awaited()


async def test_execute_script_raises_when_parameterized_role_is_not_a_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="mount", id="mount-1", device="Telescope Simulator"))
    _script(
        "connect",
        parameters={"role": {"type": "number", "required": True}},
        steps=[_set_property("{{ role }}", "CONNECTION", {"CONNECT": "On"})],
    )

    with pytest.raises(script_engine.ScriptValidationError, match="string"):
        await script_engine.execute_script("connect", "test-rig", {"role": 1})


async def test_execute_script_threads_parameterized_role_through_run_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A callee's parameterized role resolves against the *caller's* current parameter
    values, walked recursively through the whole run_script call tree up front."""
    _rig(rig_store.Component(role="mount", id="mount-1", device="Telescope Simulator"))
    _script(
        "connect",
        parameters={"role": {"type": "string", "required": True}},
        steps=[_set_property("{{ role }}", "CONNECTION", {"CONNECT": "On"})],
    )
    _script(
        "connect_mount",
        steps=[
            {"step": "run_script", "script": "connect", "parameters": {"role": "mount"}},
        ],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)

    await script_engine.execute_script("connect_mount", "test-rig", {})

    send_property.assert_awaited_once_with("Telescope Simulator", "CONNECTION", {"CONNECT": "On"})


async def test_execute_script_calling_same_sub_script_with_different_roles_resolves_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Role-usage collection memoizes by (script id, resolved params), not script id alone —
    two run_script calls to the same generic connect script with *different* roles must
    each still resolve and execute correctly, not have the second call's role usage dropped
    as if it were a repeat of the first."""
    _rig(
        rig_store.Component(role="mount", id="mount-1", device="Telescope Simulator"),
        rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"),
    )
    _script(
        "connect",
        parameters={"role": {"type": "string", "required": True}},
        steps=[_set_property("{{ role }}", "CONNECTION", {"CONNECT": "On"})],
    )
    _script(
        "connect_all",
        steps=[
            {"step": "run_script", "script": "connect", "parameters": {"role": "mount"}},
            {"step": "run_script", "script": "connect", "parameters": {"role": "camera"}},
            {"step": "run_script", "script": "connect", "parameters": {"role": "mount"}},
        ],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)

    await script_engine.execute_script("connect_all", "test-rig", {})

    assert send_property.await_args_list == [
        call("Telescope Simulator", "CONNECTION", {"CONNECT": "On"}),
        call("CCD Simulator", "CONNECTION", {"CONNECT": "On"}),
        call("Telescope Simulator", "CONNECTION", {"CONNECT": "On"}),
    ]


async def test_execute_script_generic_connect_script_is_exempt_for_its_own_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single generic, role-parameterized connect script is exempt from "must already be
    connected" for whichever role it's invoked with — same deadlock-avoidance as the
    per-role connect scripts, now for a single reusable script."""
    _rig(rig_store.Component(role="mount", id="mount-1", device="Telescope Simulator"))
    _script(
        "connect",
        parameters={"role": {"type": "string", "required": True}},
        steps=[_set_property("{{ role }}", "CONNECTION", {"CONNECT": "On"})],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)
    monkeypatch.setattr(
        indi_messaging,
        "get_property_values",
        lambda device, name: (
            {"CONNECT": "Off", "DISCONNECT": "On"} if name == "CONNECTION" else None
        ),
    )

    await script_engine.execute_script("connect", "test-rig", {"role": "mount"})

    send_property.assert_awaited_once()


async def test_execute_script_raises_validation_error_for_unknown_script_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig()

    with pytest.raises(script_engine.ScriptValidationError, match="Unknown script"):
        await script_engine.execute_script("does-not-exist", "test-rig", {})


async def test_execute_script_raises_validation_error_for_unknown_rig_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _script("cool")

    with pytest.raises(script_engine.ScriptValidationError, match="Unknown rig"):
        await script_engine.execute_script("cool", "does-not-exist", {})


async def test_execute_script_raises_validation_error_for_run_script_to_a_since_removed_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig()
    _script("caller", steps=[{"step": "run_script", "script": "does-not-exist"}])

    with pytest.raises(script_engine.ScriptValidationError, match="Unknown script"):
        await script_engine.execute_script("caller", "test-rig", {})


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


async def test_execute_script_raises_when_device_is_not_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script("cool", steps=[_set_property("camera", "CCD_TEMPERATURE", {"X": "1"})])
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)
    monkeypatch.setattr(
        indi_messaging,
        "get_property_values",
        lambda device, name: (
            {"CONNECT": "Off", "DISCONNECT": "On"} if name == "CONNECTION" else None
        ),
    )

    with pytest.raises(script_engine.ScriptPreconditionError, match="camera.*not connected"):
        await script_engine.execute_script("cool", "test-rig", {})

    send_property.assert_not_awaited()


async def test_execute_script_raises_when_device_is_unknown_to_indiserver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device indiserver has never heard of gets a distinct message from "not connected" —

    there's no CONNECTION property to set On for a device that isn't
    plugged in / whose driver isn't running, so the fix is different
    (check the physical connection / start the driver), and the error
    should say so rather than suggesting "connect it".
    """
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script("cool", steps=[_set_property("camera", "CCD_TEMPERATURE", {"X": "1"})])
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)
    monkeypatch.setattr(indi_messaging, "list_devices", lambda: [])

    with pytest.raises(
        script_engine.ScriptPreconditionError, match="camera.*not known to indiserver"
    ):
        await script_engine.execute_script("cool", "test-rig", {})

    send_property.assert_not_awaited()


async def test_execute_script_raises_when_connection_state_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CONNECTION is always defined on a real INDI device, unlike TELESCOPE_PARK/ON_COORD_SET —

    an undefined CONNECTION means the property hasn't been received yet
    (a startup race), not "doesn't apply", so it's treated the same as
    "confirmed not connected" rather than silently skipped.
    """
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script("cool", steps=[_set_property("camera", "CCD_TEMPERATURE", {"X": "1"})])
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)
    monkeypatch.setattr(indi_messaging, "get_property_values", lambda device, name: None)

    with pytest.raises(script_engine.ScriptPreconditionError, match="not connected"):
        await script_engine.execute_script("cool", "test-rig", {})

    send_property.assert_not_awaited()


async def test_execute_script_proceeds_when_device_is_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script("cool", steps=[_set_property("camera", "CCD_TEMPERATURE", {"X": "1"})])
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)

    await script_engine.execute_script("cool", "test-rig", {})

    send_property.assert_awaited_once()


async def test_execute_script_checks_every_distinct_device_the_run_needs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second, not-connected device is caught even if the first one is fine."""
    _rig(
        rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"),
        rig_store.Component(role="mount", id="mount-1", device="Telescope Simulator"),
    )
    _script(
        "sequence",
        steps=[
            _set_property("camera", "CCD_TEMPERATURE", {"X": "1"}),
            {"step": "slew", "role": "mount", "target": {"raDec": {"ra": 1.0, "dec": 2.0}}},
        ],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)
    monkeypatch.setattr(
        indi_messaging,
        "get_property_values",
        lambda device, name: (
            {"CONNECT": "On", "DISCONNECT": "Off"}
            if name == "CONNECTION" and device == "CCD Simulator"
            else {"CONNECT": "Off", "DISCONNECT": "On"}
            if name == "CONNECTION"
            else None
        ),
    )

    with pytest.raises(script_engine.ScriptPreconditionError, match="Telescope Simulator"):
        await script_engine.execute_script("sequence", "test-rig", {})

    send_property.assert_not_awaited()


async def test_execute_script_connect_script_proceeds_when_device_is_not_yet_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A script whose own steps set CONNECTION for a role is exempt from "must already be
    connected" — otherwise a connect script could never run against the very device it
    exists to connect (a deadlock)."""
    _rig(rig_store.Component(role="mount", id="mount-1", device="Telescope Simulator"))
    _script(
        "connect_mount",
        steps=[_set_property("mount", "CONNECTION", {"CONNECT": "On"})],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)
    monkeypatch.setattr(
        indi_messaging,
        "get_property_values",
        lambda device, name: (
            {"CONNECT": "Off", "DISCONNECT": "On"} if name == "CONNECTION" else None
        ),
    )

    await script_engine.execute_script("connect_mount", "test-rig", {})

    send_property.assert_awaited_once()


async def test_execute_script_connect_script_still_requires_device_known_to_indiserver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exemption only covers "must already be connected" — a device indiserver has
    never heard of still fails, since no script can connect a driver that isn't running."""
    _rig(rig_store.Component(role="mount", id="mount-1", device="Telescope Simulator"))
    _script(
        "connect_mount",
        steps=[
            _set_property("mount", "CONNECTION", {"CONNECT": "On"}),
            _wait_for("mount", "CONNECTION", "equals", "Ok"),
        ],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)
    monkeypatch.setattr(indi_messaging, "list_devices", lambda: [])

    with pytest.raises(
        script_engine.ScriptPreconditionError, match="mount.*not known to indiserver"
    ):
        await script_engine.execute_script("connect_mount", "test-rig", {})

    send_property.assert_not_awaited()


async def test_execute_script_non_exempt_script_still_requires_connection_for_same_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A script that doesn't itself manage CONNECTION for a role still requires the device
    already be connected, even if some other loaded script happens to manage that role."""
    _rig(rig_store.Component(role="mount", id="mount-1", device="Telescope Simulator"))
    _script(
        "connect_mount",
        steps=[
            _set_property("mount", "CONNECTION", {"CONNECT": "On"}),
            _wait_for("mount", "CONNECTION", "equals", "Ok"),
        ],
    )
    _script("park", steps=[_set_property("mount", "TELESCOPE_PARK", {"PARK": "On"})])
    monkeypatch.setattr(indi_messaging, "send_property", AsyncMock())
    monkeypatch.setattr(
        indi_messaging,
        "get_property_values",
        lambda device, name: (
            {"CONNECT": "Off", "DISCONNECT": "On"} if name == "CONNECTION" else None
        ),
    )

    with pytest.raises(script_engine.ScriptPreconditionError, match="mount.*not connected"):
        await script_engine.execute_script("park", "test-rig", {})


async def test_execute_script_composed_sequence_exempts_role_when_connect_runs_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A composed sequence that calls `connect` for a role before using that role for
    something else is exempt for that role — the connect call is genuinely first in
    execution order, so it gets to run against the not-yet-connected device it exists to
    connect (INDIMCP-53)."""
    _rig(rig_store.Component(role="mount", id="mount-1", device="Telescope Simulator"))
    _script(
        "connect",
        parameters={"role": {"type": "string", "required": True}},
        steps=[_set_property("{{ role }}", "CONNECTION", {"CONNECT": "On"})],
    )
    _script(
        "connect_then_park",
        steps=[
            {"step": "run_script", "script": "connect", "parameters": {"role": "mount"}},
            _set_property("mount", "TELESCOPE_PARK", {"PARK": "On"}),
        ],
    )
    monkeypatch.setattr(indi_messaging, "send_property", AsyncMock())
    monkeypatch.setattr(
        indi_messaging,
        "get_property_values",
        lambda device, name: (
            {"CONNECT": "Off", "DISCONNECT": "On"} if name == "CONNECTION" else None
        ),
    )

    await script_engine.execute_script("connect_then_park", "test-rig", {})


async def test_execute_script_composed_sequence_still_requires_connection_before_connect_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A composed sequence that uses a role for something else *before* calling `connect`
    for it is not exempt: that earlier use still needs the device already connected, since
    the connect call hasn't run yet at that point in the sequence (INDIMCP-53)."""
    _rig(rig_store.Component(role="mount", id="mount-1", device="Telescope Simulator"))
    _script(
        "connect",
        parameters={"role": {"type": "string", "required": True}},
        steps=[_set_property("{{ role }}", "CONNECTION", {"CONNECT": "On"})],
    )
    _script(
        "park_then_connect",
        steps=[
            _set_property("mount", "TELESCOPE_PARK", {"PARK": "On"}),
            {"step": "run_script", "script": "connect", "parameters": {"role": "mount"}},
        ],
    )
    monkeypatch.setattr(indi_messaging, "send_property", AsyncMock())
    monkeypatch.setattr(
        indi_messaging,
        "get_property_values",
        lambda device, name: (
            {"CONNECT": "Off", "DISCONNECT": "On"} if name == "CONNECTION" else None
        ),
    )

    with pytest.raises(script_engine.ScriptPreconditionError, match="mount.*not connected"):
        await script_engine.execute_script("park_then_connect", "test-rig", {})


async def test_execute_script_if_branch_exemption_is_coarse_across_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Documented limitation (see `_RoleUsage`): `then`/`else` are walked as if sequential,
    `then` before `else`, even though only one runs at execution time. A role connected in
    `then` reads as already exempt by the time `else` is walked, regardless of which branch a
    real run takes — pinned down here so a future change to `_walk_role_usage` doesn't
    silently make this better or worse without a test noticing (INDIMCP-53)."""
    _rig(
        rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"),
        rig_store.Component(role="mount", id="mount-1", device="Telescope Simulator"),
    )
    _script(
        "conditional_connect",
        steps=[
            {
                "step": "if",
                "condition": {
                    "role": "camera",
                    "property": "CONNECTION",
                    "operator": "equals",
                    "value": "On",
                },
                "then": [_set_property("mount", "CONNECTION", {"CONNECT": "On"})],
                "else": [_set_property("mount", "TELESCOPE_PARK", {"PARK": "On"})],
            }
        ],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)
    # Condition false (camera reports "Off"), so only `else` actually runs — it never
    # manages `mount`'s CONNECTION itself, but `then`'s connect step was already walked
    # (recording `mount` as exempt) before `else` was walked, regardless of which branch
    # executes. Camera stays reported "connected" throughout so only mount's exemption is
    # under test here.
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Off")
    monkeypatch.setattr(
        indi_messaging,
        "get_property_values",
        lambda device, name: (
            {"CONNECT": "Off", "DISCONNECT": "On"}
            if name == "CONNECTION" and device == "Telescope Simulator"
            else _default_get_property_values(device, name)
        ),
    )

    await script_engine.execute_script("conditional_connect", "test-rig", {})

    send_property.assert_awaited_once_with("Telescope Simulator", "TELESCOPE_PARK", {"PARK": "On"})


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


async def test_execute_script_wait_for_fails_fast_on_alert_vector_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `wait_for` condition comparing the vector state itself shouldn't wait out the full
    timeout once the driver reports `Alert` — a fault isn't something more polling fixes.
    """
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script("wait", steps=[_wait_for("camera", "CCD_TEMPERATURE", "equals", "Ok", timeout=60)])
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Alert")
    monkeypatch.setattr(script_engine, "_WAIT_POLL_INTERVAL_SECONDS", 0.001)

    with pytest.raises(script_engine.ScriptExecutionError, match="went to Alert"):
        await asyncio.wait_for(script_engine.execute_script("wait", "test-rig", {}), timeout=1.0)


async def test_execute_script_wait_for_fails_fast_on_alert_with_an_element_condition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same fast-fail, but for a condition that compares an element value rather than the
    vector state directly — the vector can still be `Alert` while an element's value hasn't
    (and never will) reach the awaited target.
    """
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
                "timeoutSeconds": 60,
            }
        ],
    )
    monkeypatch.setattr(
        indi_messaging,
        "get_property_values",
        lambda device, name: (
            {"CCD_TEMPERATURE_VALUE": "5.0"}
            if name == "CCD_TEMPERATURE"
            else _default_get_property_values(device, name)
        ),
    )
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Alert")
    monkeypatch.setattr(script_engine, "_WAIT_POLL_INTERVAL_SECONDS", 0.001)

    with pytest.raises(script_engine.ScriptExecutionError, match="went to Alert"):
        await asyncio.wait_for(script_engine.execute_script("wait", "test-rig", {}), timeout=1.0)


async def test_execute_script_wait_for_deliberately_waiting_for_alert_still_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A script that's explicitly waiting *for* `Alert` (e.g. a diagnostic check) isn't treated
    as a fault — the condition is evaluated before the fast-fail check, so it wins.
    """
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script("wait", steps=[_wait_for("camera", "CCD_TEMPERATURE", "equals", "Alert")])
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Alert")
    monkeypatch.setattr(script_engine, "_WAIT_POLL_INTERVAL_SECONDS", 0.001)

    result = await script_engine.execute_script("wait", "test-rig", {})

    assert result["stepsExecuted"] == 1


async def test_execute_script_wait_for_fetches_vector_state_once_per_poll_vector_condition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_evaluate_condition` fetches the vector state once and `_execute_wait_for` reuses that
    same result for its Alert check — a regression guard against re-introducing a second,
    redundant `get_property_state` call per poll iteration (an earlier draft of this fast-fail
    did exactly that).
    """
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script("wait", steps=[_wait_for("camera", "CCD_TEMPERATURE", "equals", "Ok")])
    calls: list[tuple[str, str]] = []
    states = iter(["Busy", "Busy", "Ok"])

    def counting_get_property_state(device: str, name: str) -> str:
        calls.append((device, name))
        return next(states)

    monkeypatch.setattr(indi_messaging, "get_property_state", counting_get_property_state)
    monkeypatch.setattr(script_engine, "_WAIT_POLL_INTERVAL_SECONDS", 0.001)

    await script_engine.execute_script("wait", "test-rig", {})

    # Three poll iterations (Busy, Busy, Ok) fed from `states` — one call each. If
    # get_property_state were fetched twice per iteration, `states` would run dry
    # (StopIteration) before "Ok" was ever reached.
    assert len(calls) == 3


async def test_execute_script_wait_for_fetches_vector_state_once_per_poll_element_condition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same regression guard, for a condition that compares an element value rather than the
    vector state directly — `get_property_state` is only used here for the Alert check, but it
    should still be called exactly once per poll, not twice.
    """
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
    state_calls: list[tuple[str, str]] = []
    values = iter(["5.0", "5.0", "-12.5"])

    def counting_get_property_state(device: str, name: str) -> str:
        state_calls.append((device, name))
        return "Busy"

    def get_property_values(device: str, name: str) -> dict[str, str] | None:
        if name == "CCD_TEMPERATURE":
            return {"CCD_TEMPERATURE_VALUE": next(values)}
        return _default_get_property_values(device, name)

    monkeypatch.setattr(indi_messaging, "get_property_state", counting_get_property_state)
    monkeypatch.setattr(indi_messaging, "get_property_values", get_property_values)
    monkeypatch.setattr(script_engine, "_WAIT_POLL_INTERVAL_SECONDS", 0.001)

    await script_engine.execute_script("wait", "test-rig", {})

    # `values` has exactly 3 items, driving exactly 3 poll iterations — one
    # get_property_state call each, independent of how many times `values` itself
    # is consumed.
    assert len(state_calls) == 3


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
        lambda device, name: (
            {"CCD_TEMPERATURE_VALUE": "-12.5"}
            if name == "CCD_TEMPERATURE"
            else _default_get_property_values(device, name)
        ),
    )

    result = await script_engine.execute_script("wait", "test-rig", {})

    assert result["stepsExecuted"] == 1


async def test_execute_script_wait_for_warns_on_a_typo_d_element_name(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
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
                    "element": "CCD_TEMPERATURE_VALU",  # typo'd, missing the trailing E
                    "operator": "lessThanOrEqual",
                    "value": -10,
                },
                "timeoutSeconds": 0.01,
            }
        ],
    )
    monkeypatch.setattr(
        indi_messaging,
        "get_property_values",
        lambda device, name: (
            {"CCD_TEMPERATURE_VALUE": "-12.5"}
            if name == "CCD_TEMPERATURE"
            else _default_get_property_values(device, name)
        ),
    )
    monkeypatch.setattr(script_engine, "_WAIT_POLL_INTERVAL_SECONDS", 0.001)

    with (
        caplog.at_level("WARNING"),
        pytest.raises(script_engine.ScriptExecutionError, match="timed out"),
    ):
        await script_engine.execute_script("wait", "test-rig", {})

    assert any("unknown element" in record.message for record in caplog.records)
    assert any("CCD_TEMPERATURE_VALU" in record.message for record in caplog.records)


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


def _blob_snapshot(
    data: bytes = b"fits-bytes",
    *,
    member: str = "CCD1",
    fmt: str = ".fits",
    timestamp: datetime | None = None,
) -> indi_messaging.BlobSnapshot:
    return {
        "values": {member: data},
        "sizeformat": {member: (len(data), fmt)},
        "timestamp": timestamp if timestamp is not None else datetime.now(tz=UTC),
    }


def _mock_capture_frame_success(
    monkeypatch: pytest.MonkeyPatch,
    *,
    exposure_states: list[str] | None = None,
    blob: indi_messaging.BlobSnapshot | None = None,
    saved_metadata: dict[str, Any] | None = None,
) -> tuple[AsyncMock, MagicMock]:
    """Wire up the mocks a successful `capture_frame` run needs.

    Returns `(send_property, save_frame)`.
    """
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)
    states = iter(exposure_states if exposure_states is not None else ["Ok"])
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: next(states))

    def get_latest_blob(device: str, name: str) -> indi_messaging.BlobSnapshot:
        # Stamped with the current time on every poll (not once, up front, when this
        # helper runs) — the handler's `since` marker is captured live, after this
        # helper returns, so a fixed timestamp baked in here would always predate it
        # and the capture would spuriously time out waiting for "a newer BLOB".
        template = blob or _blob_snapshot()
        return {**template, "timestamp": datetime.now(tz=UTC)}

    monkeypatch.setattr(indi_messaging, "get_latest_blob", get_latest_blob)
    monkeypatch.setattr(script_engine, "_WAIT_POLL_INTERVAL_SECONDS", 0.001)
    save_frame = MagicMock(
        return_value=saved_metadata
        or {
            "frameId": "frame-1",
            "runId": None,
            "device": "CCD Simulator",
            "sizeBytes": 10,
            "capturedAt": "2026-07-20T00:00:00.000000+00:00",
            "transferredAt": None,
        }
    )
    monkeypatch.setattr(frame_store, "save_frame", save_frame)
    return send_property, save_frame


async def test_execute_script_capture_frame_sends_exposure_and_saves_the_drained_blob(
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
    send_property, save_frame = _mock_capture_frame_success(
        monkeypatch, blob=_blob_snapshot(b"the-frame-bytes", fmt=".fits")
    )

    result = await script_engine.execute_script("capture", "test-rig", {})

    send_property.assert_awaited_once_with(
        "CCD Simulator", "CCD_EXPOSURE", {"CCD_EXPOSURE_VALUE": "30.0"}
    )
    save_frame.assert_called_once_with(
        b"the-frame-bytes", device="CCD Simulator", extension=".fits", run_id=None
    )
    assert result["stepsExecuted"] == 1
    assert result["framesCaptured"] == 1


async def test_execute_script_capture_frame_sets_frame_type_and_binning_when_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "capture",
        steps=[
            {
                "step": "capture_frame",
                "role": "camera",
                "exposureSeconds": 5,
                "frameType": "Dark",
                "binningX": 2,
                "binningY": 2,
            }
        ],
    )

    def get_property_values(device: str, name: str) -> dict[str, str] | None:
        if name in ("CCD_FRAME_TYPE", "CCD_BINNING"):
            return {"placeholder": "value"}
        return _default_get_property_values(device, name)

    monkeypatch.setattr(indi_messaging, "get_property_values", get_property_values)
    send_property, _ = _mock_capture_frame_success(monkeypatch)

    await script_engine.execute_script("capture", "test-rig", {})

    send_property.assert_any_call("CCD Simulator", "CCD_FRAME_TYPE", {"FRAME_DARK": "On"})
    send_property.assert_any_call("CCD Simulator", "CCD_BINNING", {"HOR_BIN": "2", "VER_BIN": "2"})
    send_property.assert_any_call("CCD Simulator", "CCD_EXPOSURE", {"CCD_EXPOSURE_VALUE": "5.0"})


async def test_execute_script_capture_frame_skips_frame_type_and_binning_when_undefined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`CCD_FRAME_TYPE`/`CCD_BINNING` are undefined by default (`_default_get_property_values`)."""
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "capture",
        steps=[{"step": "capture_frame", "role": "camera", "exposureSeconds": 5}],
    )
    send_property, _ = _mock_capture_frame_success(monkeypatch)

    await script_engine.execute_script("capture", "test-rig", {})

    send_property.assert_awaited_once_with(
        "CCD Simulator", "CCD_EXPOSURE", {"CCD_EXPOSURE_VALUE": "5.0"}
    )


async def test_execute_script_capture_frame_tags_saved_frame_with_run_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "capture",
        steps=[{"step": "capture_frame", "role": "camera", "exposureSeconds": 5}],
    )
    _, save_frame = _mock_capture_frame_success(monkeypatch)

    await script_engine.execute_script("capture", "test-rig", {}, run_id="run-42")

    save_frame.assert_called_once_with(
        b"fits-bytes", device="CCD Simulator", extension=".fits", run_id="run-42"
    )


async def test_execute_script_capture_frame_times_out_waiting_for_exposure_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "capture",
        steps=[{"step": "capture_frame", "role": "camera", "exposureSeconds": 0}],
    )
    monkeypatch.setattr(indi_messaging, "send_property", AsyncMock())
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Busy")
    monkeypatch.setattr(script_engine, "_WAIT_POLL_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr(script_engine, "_CAPTURE_READOUT_BUFFER_SECONDS", 0.01)

    with pytest.raises(script_engine.ScriptExecutionError, match="did not reach"):
        await script_engine.execute_script("capture", "test-rig", {})


async def test_execute_script_capture_frame_fails_fast_on_exposure_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A driver-reported `Alert` (aborted exposure, camera fault) shouldn't be waited out for
    the full timeout — it's not something more polling will resolve.
    """
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "capture",
        steps=[{"step": "capture_frame", "role": "camera", "exposureSeconds": 0}],
    )
    monkeypatch.setattr(indi_messaging, "send_property", AsyncMock())
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Alert")
    monkeypatch.setattr(script_engine, "_WAIT_POLL_INTERVAL_SECONDS", 0.001)
    # A large timeout that a correct fast-fail must not wait out.
    monkeypatch.setattr(script_engine, "_CAPTURE_READOUT_BUFFER_SECONDS", 60.0)

    with pytest.raises(script_engine.ScriptExecutionError, match="went to Alert"):
        await asyncio.wait_for(script_engine.execute_script("capture", "test-rig", {}), timeout=1.0)


async def test_wait_for_property_state_fails_fast_on_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Alert")
    monkeypatch.setattr(script_engine, "_WAIT_POLL_INTERVAL_SECONDS", 0.001)
    ctx = script_engine._ExecutionContext(
        role_to_device={},
        cancel_event=None,
        pause_event=None,
        on_progress=None,
        total_steps=None,
        scripts={},
        run_id=None,
    )

    with pytest.raises(script_engine.ScriptExecutionError, match="went to Alert"):
        await asyncio.wait_for(
            script_engine._wait_for_property_state(
                ctx, "CCD Simulator", "CCD_EXPOSURE", indi_messaging.PropertyState.OK, 60.0
            ),
            timeout=1.0,
        )


async def test_execute_script_capture_frame_times_out_waiting_for_blob(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "capture",
        steps=[{"step": "capture_frame", "role": "camera", "exposureSeconds": 0}],
    )
    monkeypatch.setattr(indi_messaging, "send_property", AsyncMock())
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Ok")
    monkeypatch.setattr(indi_messaging, "get_latest_blob", lambda device, name: None)
    monkeypatch.setattr(script_engine, "_WAIT_POLL_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr(script_engine, "_CAPTURE_READOUT_BUFFER_SECONDS", 0.01)

    with pytest.raises(script_engine.ScriptExecutionError, match="no BLOB received"):
        await script_engine.execute_script("capture", "test-rig", {})


async def test_execute_script_capture_frame_shares_a_single_deadline_across_both_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exposure-wait and BLOB-wait draw from one combined budget, not two independent
    full timeouts — a driver that's slow to reach `Ok` should eat into the time left for
    the BLOB to arrive, not get a fresh full budget on top of it.
    """
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "capture",
        steps=[{"step": "capture_frame", "role": "camera", "exposureSeconds": 0}],
    )
    loop = asyncio.get_running_loop()
    start = loop.time()

    def get_property_state(device: str, name: str) -> str:
        return "Ok" if loop.time() - start >= 0.08 else "Busy"

    monkeypatch.setattr(indi_messaging, "send_property", AsyncMock())
    monkeypatch.setattr(indi_messaging, "get_property_state", get_property_state)
    monkeypatch.setattr(indi_messaging, "get_latest_blob", lambda device, name: None)
    monkeypatch.setattr(script_engine, "_WAIT_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(script_engine, "_CAPTURE_READOUT_BUFFER_SECONDS", 0.1)

    with pytest.raises(script_engine.ScriptExecutionError, match="no BLOB received"):
        await script_engine.execute_script("capture", "test-rig", {})

    elapsed = loop.time() - start
    # The old (buggy) behavior gave the BLOB wait its own fresh 0.1s on top of the ~0.08s
    # already spent reaching `Ok`, for ~0.18s total. A shared deadline caps the whole
    # capture at ~0.1s instead.
    assert elapsed < 0.15


async def test_execute_script_capture_frame_ignores_a_stale_blob_from_an_earlier_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A BLOB already cached from before this capture's exposure command doesn't count."""
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "capture",
        steps=[{"step": "capture_frame", "role": "camera", "exposureSeconds": 0}],
    )
    stale = _blob_snapshot(b"stale-bytes", timestamp=datetime(2020, 1, 1, tzinfo=UTC))
    monkeypatch.setattr(indi_messaging, "send_property", AsyncMock())
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Ok")
    monkeypatch.setattr(indi_messaging, "get_latest_blob", lambda device, name: stale)
    monkeypatch.setattr(script_engine, "_WAIT_POLL_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr(script_engine, "_CAPTURE_READOUT_BUFFER_SECONDS", 0.01)

    with pytest.raises(script_engine.ScriptExecutionError, match="no BLOB received"):
        await script_engine.execute_script("capture", "test-rig", {})


async def test_execute_script_capture_frame_rejects_a_blob_with_more_than_one_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "capture",
        steps=[{"step": "capture_frame", "role": "camera", "exposureSeconds": 0}],
    )

    def ambiguous(device: str, name: str) -> indi_messaging.BlobSnapshot:
        return {
            "values": {"CCD1": b"a", "CCD2": b"b"},
            "sizeformat": {"CCD1": (1, ".fits"), "CCD2": (1, ".fits")},
            "timestamp": datetime.now(tz=UTC),
        }

    monkeypatch.setattr(indi_messaging, "send_property", AsyncMock())
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Ok")
    monkeypatch.setattr(indi_messaging, "get_latest_blob", ambiguous)
    monkeypatch.setattr(script_engine, "_WAIT_POLL_INTERVAL_SECONDS", 0.001)

    with pytest.raises(script_engine.ScriptExecutionError, match="expected exactly one"):
        await script_engine.execute_script("capture", "test-rig", {})


async def test_execute_script_slew_sets_ra_dec_and_waits_for_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="mount", id="mount-1", device="Telescope Simulator"))
    _script(
        "slew",
        steps=[
            {
                "step": "slew",
                "role": "mount",
                "target": {"raDec": {"ra": 10.5, "dec": -20.25}},
            }
        ],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)
    states = iter(["Busy", "Busy", "Ok"])
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: next(states))
    monkeypatch.setattr(script_engine, "_WAIT_POLL_INTERVAL_SECONDS", 0.001)

    result = await script_engine.execute_script("slew", "test-rig", {})

    send_property.assert_awaited_once_with(
        "Telescope Simulator", "EQUATORIAL_EOD_COORD", {"RA": "10.5", "DEC": "-20.25"}
    )
    assert result["stepsExecuted"] == 1


async def test_execute_script_slew_substitutes_parameter_references_in_ra_dec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="mount", id="mount-1", device="Telescope Simulator"))
    _script(
        "slew",
        parameters={
            "ra": {"type": "number", "required": True},
            "dec": {"type": "number", "required": True},
        },
        steps=[
            {
                "step": "slew",
                "role": "mount",
                "target": {"raDec": {"ra": "{{ ra }}", "dec": "{{ dec }}"}},
            }
        ],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Ok")

    await script_engine.execute_script("slew", "test-rig", {"ra": 5.0, "dec": 45.0})

    send_property.assert_awaited_once_with(
        "Telescope Simulator", "EQUATORIAL_EOD_COORD", {"RA": "5.0", "DEC": "45.0"}
    )


async def test_execute_script_slew_times_out_waiting_for_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="mount", id="mount-1", device="Telescope Simulator"))
    _script(
        "slew",
        steps=[{"step": "slew", "role": "mount", "target": {"raDec": {"ra": 1.0, "dec": 2.0}}}],
    )
    monkeypatch.setattr(indi_messaging, "send_property", AsyncMock())
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Busy")
    monkeypatch.setattr(script_engine, "_WAIT_POLL_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr(script_engine, "_SLEW_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(script_engine.ScriptExecutionError, match="did not reach"):
        await script_engine.execute_script("slew", "test-rig", {})


async def test_execute_script_slew_object_name_raises_not_yet_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="mount", id="mount-1", device="Telescope Simulator"))
    _script(
        "slew",
        steps=[{"step": "slew", "role": "mount", "target": {"objectName": "M101"}}],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)

    with pytest.raises(script_engine.ScriptExecutionError, match="objectName"):
        await script_engine.execute_script("slew", "test-rig", {})

    send_property.assert_not_awaited()


async def test_execute_script_slew_rejects_a_parked_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="mount", id="mount-1", device="Telescope Simulator"))
    _script(
        "slew",
        steps=[{"step": "slew", "role": "mount", "target": {"raDec": {"ra": 1.0, "dec": 2.0}}}],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)
    monkeypatch.setattr(
        indi_messaging,
        "get_property_values",
        lambda device, name: (
            {"PARK": "On", "UNPARK": "Off"}
            if name == "TELESCOPE_PARK"
            else _default_get_property_values(device, name)
        ),
    )

    with pytest.raises(script_engine.ScriptPreconditionError, match="parked"):
        await script_engine.execute_script("slew", "test-rig", {})

    send_property.assert_not_awaited()


async def test_execute_script_slew_proceeds_when_mount_is_unparked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="mount", id="mount-1", device="Telescope Simulator"))
    _script(
        "slew",
        steps=[{"step": "slew", "role": "mount", "target": {"raDec": {"ra": 1.0, "dec": 2.0}}}],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)
    monkeypatch.setattr(
        indi_messaging,
        "get_property_values",
        lambda device, name: (
            {"PARK": "Off", "UNPARK": "On"}
            if name == "TELESCOPE_PARK"
            else _default_get_property_values(device, name)
        ),
    )
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Ok")

    await script_engine.execute_script("slew", "test-rig", {})

    send_property.assert_awaited_once()


async def test_execute_script_slew_proceeds_when_mount_has_no_park_property(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mount driver with no TELESCOPE_PARK support (park is optional) is treated as unparked."""
    _rig(rig_store.Component(role="mount", id="mount-1", device="Telescope Simulator"))
    _script(
        "slew",
        steps=[{"step": "slew", "role": "mount", "target": {"raDec": {"ra": 1.0, "dec": 2.0}}}],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Ok")

    await script_engine.execute_script("slew", "test-rig", {})

    send_property.assert_awaited_once()


async def test_execute_script_slew_sets_on_coord_set_to_track_before_slewing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="mount", id="mount-1", device="Telescope Simulator"))
    _script(
        "slew",
        steps=[{"step": "slew", "role": "mount", "target": {"raDec": {"ra": 1.0, "dec": 2.0}}}],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)
    monkeypatch.setattr(
        indi_messaging,
        "get_property_values",
        lambda device, name: (
            {"SLEW": "Off", "TRACK": "Off", "SYNC": "On"}
            if name == "ON_COORD_SET"
            else _default_get_property_values(device, name)
        ),
    )
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Ok")

    await script_engine.execute_script("slew", "test-rig", {})

    assert send_property.await_args_list == [
        call("Telescope Simulator", "ON_COORD_SET", {"TRACK": "On"}),
        call("Telescope Simulator", "EQUATORIAL_EOD_COORD", {"RA": "1.0", "DEC": "2.0"}),
    ]


async def test_execute_script_slew_skips_on_coord_set_when_mount_has_no_such_property(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A driver with no ON_COORD_SET support at all is skipped, not an error."""
    _rig(rig_store.Component(role="mount", id="mount-1", device="Telescope Simulator"))
    _script(
        "slew",
        steps=[{"step": "slew", "role": "mount", "target": {"raDec": {"ra": 1.0, "dec": 2.0}}}],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Ok")

    await script_engine.execute_script("slew", "test-rig", {})

    send_property.assert_awaited_once_with(
        "Telescope Simulator", "EQUATORIAL_EOD_COORD", {"RA": "1.0", "DEC": "2.0"}
    )


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
        run_id=None,
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
