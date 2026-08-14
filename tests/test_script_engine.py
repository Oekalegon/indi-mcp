import asyncio
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from indi_mcp import (
    fits_headers,
    frame_store,
    indi_messaging,
    observatory_store,
    rig_store,
    script_engine,
    script_store,
)

_known_devices: list[str] = []
_BUILTIN_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


@pytest.fixture(autouse=True)
def _reset_stores() -> None:
    rig_store._rigs = {}
    script_store._scripts = {}
    observatory_store._observatories = {}
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


def _observatory(
    observatory_id: str = "test-observatory", **fields: Any
) -> observatory_store.Observatory:
    fields.setdefault("name", observatory_id)
    fields.setdefault("latitudeDeg", 60.369722)
    fields.setdefault("longitudeDeg", 11.363611)
    fields.setdefault("elevationMeters", 350)
    observatory = observatory_store.Observatory(id=observatory_id, **fields)
    observatory_store._observatories[observatory.id] = observatory
    return observatory


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
    assert result == {
        "scriptId": "cool",
        "stepsExecuted": 1,
        "framesCaptured": 0,
        "warnings": [],
    }


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


async def test_execute_script_raises_validation_error_for_unknown_location_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig()
    _script("cool")

    with pytest.raises(script_engine.ScriptValidationError, match="Unknown observatory"):
        await script_engine.execute_script("cool", "test-rig", {}, location_id="does-not-exist")


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


async def test_execute_script_set_property_substitutes_parameter_references_in_element_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="mount", id="mount-1", device="Telescope Simulator"))
    _script(
        "set_rate",
        parameters={"rate": script_store.Parameter(type="number", required=True)},
        steps=[_set_property("mount", "TELESCOPE_TRACK_RATE", {"TRACK_RATE_RA": "{{ rate }}"})],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)

    await script_engine.execute_script("set_rate", "test-rig", {"rate": 15.04})

    send_property.assert_awaited_once_with(
        "Telescope Simulator", "TELESCOPE_TRACK_RATE", {"TRACK_RATE_RA": "15.04"}
    )


async def test_execute_script_set_property_substitutes_parameter_references_in_element_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `{{ paramName }}` reference in an `elements` *key*, not just a value, lets one
    parameterized script pick which switch member to enable (INDIMCP-49) — e.g. a `mode`
    parameter resolving to which `TELESCOPE_TRACK_MODE` element gets set to "On"."""
    _rig(rig_store.Component(role="mount", id="mount-1", device="Telescope Simulator"))
    _script(
        "set_track_mode",
        parameters={"modeSwitchElement": script_store.Parameter(type="string", required=True)},
        steps=[_set_property("mount", "TELESCOPE_TRACK_MODE", {"{{ modeSwitchElement }}": "On"})],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)

    await script_engine.execute_script(
        "set_track_mode", "test-rig", {"modeSwitchElement": "TRACK_SIDEREAL"}
    )

    send_property.assert_awaited_once_with(
        "Telescope Simulator", "TELESCOPE_TRACK_MODE", {"TRACK_SIDEREAL": "On"}
    )


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


async def test_execute_script_repeat_count_accepts_a_parameter_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`repeat.count` may be a `"{{ paramName }}"` reference, not just a literal — the caller
    picks the frame count per run instead of it being fixed in the script file. This also
    exercises `_count_total_steps` resolving the reference (not falling back to `None`),
    since `stepsExecuted`'s total is only reported when it can be computed exactly."""
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "repeat-count-param",
        parameters={"count": script_store.Parameter(type="integer", required=True)},
        steps=[
            {
                "step": "repeat",
                "count": "{{ count }}",
                "steps": [_set_property("camera", "CCD_EXPOSURE", {"X": "1"})],
            }
        ],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)
    progress: list[script_engine.ScriptProgress] = []

    result = await script_engine.execute_script(
        "repeat-count-param", "test-rig", {"count": 5}, on_progress=progress.append
    )

    assert send_property.await_count == 5
    assert result["stepsExecuted"] == 6
    assert all(event["totalSteps"] == 6 for event in progress)


async def test_execute_script_repeat_count_rejects_a_non_numeric_parameter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `repeat.count` reference that resolves to something non-numeric fails with the
    engine's documented `ScriptValidationError` (mapped to `scriptFailed` by `script_runs.py`),
    not a raw `ValueError`/`TypeError` from the underlying `int()` call — nothing validates a
    top-level `run_script` call's `parameters` against the script's declared parameter types
    before use, so this guard is the engine's own responsibility."""
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "repeat-count-bad-param",
        parameters={"count": script_store.Parameter(type="integer", required=True)},
        steps=[
            {
                "step": "repeat",
                "count": "{{ count }}",
                "steps": [_set_property("camera", "CCD_EXPOSURE", {"X": "1"})],
            }
        ],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)

    with pytest.raises(script_engine.ScriptValidationError, match="repeat.count"):
        await script_engine.execute_script("repeat-count-bad-param", "test-rig", {"count": "abc"})

    send_property.assert_not_awaited()


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


async def test_execute_script_capture_frame_reports_a_status_message_after_saving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`capture_frame` reports on `on_status` (INDIMCP-58) once the frame is saved — a
    lower-noise channel than `on_progress`, which already fired before the step started."""
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "capture",
        steps=[{"step": "capture_frame", "role": "camera", "exposureSeconds": 5}],
    )
    _mock_capture_frame_success(monkeypatch)
    status: list[script_engine.ScriptStatusMessage] = []

    await script_engine.execute_script("capture", "test-rig", {}, on_status=status.append)

    assert len(status) == 1
    assert status[0]["role"] == "camera"
    assert status[0]["device"] == "CCD Simulator"
    assert status[0]["message"] == "Captured frame frame-1 (10 bytes)"


async def test_execute_script_without_on_status_does_not_call_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`on_status=None` (the default) is a no-op, same as `on_progress=None` — a caller that
    doesn't ask for the channel doesn't pay for it or need to handle it."""
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "capture",
        steps=[{"step": "capture_frame", "role": "camera", "exposureSeconds": 5}],
    )
    _mock_capture_frame_success(monkeypatch)

    result = await script_engine.execute_script("capture", "test-rig", {})

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


async def test_execute_script_capture_frame_substitutes_parameter_reference_in_frame_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "capture",
        parameters={"frameType": {"type": "string", "required": True}},
        steps=[
            {
                "step": "capture_frame",
                "role": "camera",
                "exposureSeconds": 5,
                "frameType": "{{ frameType }}",
            }
        ],
    )

    def get_property_values(device: str, name: str) -> dict[str, str] | None:
        if name == "CCD_FRAME_TYPE":
            return {"placeholder": "value"}
        return _default_get_property_values(device, name)

    monkeypatch.setattr(indi_messaging, "get_property_values", get_property_values)
    send_property, _ = _mock_capture_frame_success(monkeypatch)

    await script_engine.execute_script("capture", "test-rig", {"frameType": "Flat"})

    send_property.assert_any_call("CCD Simulator", "CCD_FRAME_TYPE", {"FRAME_FLAT": "On"})


async def test_execute_script_capture_frame_rejects_an_unknown_runtime_frame_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `frameType` script parameter isn't restricted to FrameType's four literals at the
    Parameter level (it's just `type: string`), so a bad runtime value only gets caught here,
    not at script-load time — must fail clearly rather than send a nonsense CCD_FRAME_TYPE
    command."""
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "capture",
        parameters={"frameType": {"type": "string", "required": True}},
        steps=[
            {
                "step": "capture_frame",
                "role": "camera",
                "exposureSeconds": 5,
                "frameType": "{{ frameType }}",
            }
        ],
    )

    def get_property_values(device: str, name: str) -> dict[str, str] | None:
        if name == "CCD_FRAME_TYPE":
            return {"placeholder": "value"}
        return _default_get_property_values(device, name)

    monkeypatch.setattr(indi_messaging, "get_property_values", get_property_values)
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)

    with pytest.raises(script_engine.ScriptValidationError, match="unknown frameType 'Bogus'"):
        await script_engine.execute_script("capture", "test-rig", {"frameType": "Bogus"})

    send_property.assert_not_awaited()


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


async def test_execute_script_capture_frame_sets_gain_and_offset_when_supported(
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
                "gain": 100,
                "offset": 10,
            }
        ],
    )

    def get_property_values(device: str, name: str) -> dict[str, str] | None:
        if name in ("CCD_GAIN", "CCD_OFFSET"):
            return {"placeholder": "value"}
        return _default_get_property_values(device, name)

    monkeypatch.setattr(indi_messaging, "get_property_values", get_property_values)
    send_property, _ = _mock_capture_frame_success(monkeypatch)

    await script_engine.execute_script("capture", "test-rig", {})

    send_property.assert_any_call("CCD Simulator", "CCD_GAIN", {"GAIN": "100.0"})
    send_property.assert_any_call("CCD Simulator", "CCD_OFFSET", {"OFFSET": "10.0"})


async def test_execute_script_capture_frame_skips_gain_and_offset_when_not_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset gain/offset means "leave the device's current setting alone", not "set to some
    default" — so no CCD_GAIN/CCD_OFFSET command should be sent at all, even though the
    device supports both."""
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "capture",
        steps=[{"step": "capture_frame", "role": "camera", "exposureSeconds": 5}],
    )

    def get_property_values(device: str, name: str) -> dict[str, str] | None:
        if name in ("CCD_GAIN", "CCD_OFFSET"):
            return {"placeholder": "value"}
        return _default_get_property_values(device, name)

    monkeypatch.setattr(indi_messaging, "get_property_values", get_property_values)
    send_property, _ = _mock_capture_frame_success(monkeypatch)

    await script_engine.execute_script("capture", "test-rig", {})

    send_property.assert_awaited_once_with(
        "CCD Simulator", "CCD_EXPOSURE", {"CCD_EXPOSURE_VALUE": "5.0"}
    )


async def test_execute_script_capture_frame_skips_gain_and_offset_when_reference_resolves_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unlike the step above, gain/offset are templated (not omitted) here — matching the
    shape of the real built-in capture_frame.yaml, which always templates them and relies
    on the calling script's own declared parameter defaulting to None when unsupplied."""
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "capture",
        parameters={
            "gain": {"type": "number", "required": False},
            "offset": {"type": "number", "required": False},
        },
        steps=[
            {
                "step": "capture_frame",
                "role": "camera",
                "exposureSeconds": 5,
                "gain": "{{ gain }}",
                "offset": "{{ offset }}",
            }
        ],
    )

    def get_property_values(device: str, name: str) -> dict[str, str] | None:
        if name in ("CCD_GAIN", "CCD_OFFSET"):
            return {"placeholder": "value"}
        return _default_get_property_values(device, name)

    monkeypatch.setattr(indi_messaging, "get_property_values", get_property_values)
    send_property, _ = _mock_capture_frame_success(monkeypatch)

    await script_engine.execute_script("capture", "test-rig", {})

    send_property.assert_awaited_once_with(
        "CCD Simulator", "CCD_EXPOSURE", {"CCD_EXPOSURE_VALUE": "5.0"}
    )


async def test_execute_script_capture_frame_skips_gain_and_offset_when_undefined_on_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even when gain/offset are explicitly requested, a device with no CCD_GAIN/CCD_OFFSET
    property (not every camera exposes adjustable gain/offset) is skipped, not an error."""
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "capture",
        steps=[
            {
                "step": "capture_frame",
                "role": "camera",
                "exposureSeconds": 5,
                "gain": 100,
                "offset": 10,
            }
        ],
    )
    send_property, _ = _mock_capture_frame_success(monkeypatch)

    await script_engine.execute_script("capture", "test-rig", {})

    send_property.assert_awaited_once_with(
        "CCD Simulator", "CCD_EXPOSURE", {"CCD_EXPOSURE_VALUE": "5.0"}
    )


async def test_execute_script_capture_frame_substitutes_parameter_references_in_gain_and_offset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "capture",
        parameters={
            "gain": {"type": "number", "required": True},
            "offset": {"type": "number", "required": True},
        },
        steps=[
            {
                "step": "capture_frame",
                "role": "camera",
                "exposureSeconds": 5,
                "gain": "{{ gain }}",
                "offset": "{{ offset }}",
            }
        ],
    )

    def get_property_values(device: str, name: str) -> dict[str, str] | None:
        if name in ("CCD_GAIN", "CCD_OFFSET"):
            return {"placeholder": "value"}
        return _default_get_property_values(device, name)

    monkeypatch.setattr(indi_messaging, "get_property_values", get_property_values)
    send_property, _ = _mock_capture_frame_success(monkeypatch)

    await script_engine.execute_script("capture", "test-rig", {"gain": 200, "offset": 20})

    send_property.assert_any_call("CCD Simulator", "CCD_GAIN", {"GAIN": "200.0"})
    send_property.assert_any_call("CCD Simulator", "CCD_OFFSET", {"OFFSET": "20.0"})


async def test_builtin_capture_sensor_calibration_set_skips_gain_and_offset_when_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end run of the actual shipped `capture_sensor_calibration_set.yaml` (not a
    hand-built stand-in) with gain/offset omitted — the script always templates them
    (matching capture_frame.yaml's own convention), so this exercises that the real file's
    `"{{ gain }}"`/`"{{ offset }}"` references resolve to `None` and CCD_GAIN/CCD_OFFSET are
    never sent, while every requested bias/flat/flat-dark exposure still fires."""
    script_store.load_scripts(_BUILTIN_SCRIPTS_DIR)
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))

    def get_property_values(device: str, name: str) -> dict[str, str] | None:
        if name in ("CCD_GAIN", "CCD_OFFSET"):
            return {"placeholder": "value"}
        return _default_get_property_values(device, name)

    monkeypatch.setattr(indi_messaging, "get_property_values", get_property_values)
    send_property, _ = _mock_capture_frame_success(monkeypatch, exposure_states=["Ok"] * 8)

    result = await script_engine.execute_script(
        "capture_sensor_calibration_set",
        "test-rig",
        {"flatExposureSeconds": 2.0, "biasCount": 2, "flatCount": 1, "darkCount": 1},
    )

    assert result["framesCaptured"] == 4
    gain_or_offset_calls = [
        c for c in send_property.await_args_list if c.args[1] in ("CCD_GAIN", "CCD_OFFSET")
    ]
    assert gain_or_offset_calls == []
    send_property.assert_any_call("CCD Simulator", "CCD_EXPOSURE", {"CCD_EXPOSURE_VALUE": "0.0"})
    send_property.assert_any_call("CCD Simulator", "CCD_EXPOSURE", {"CCD_EXPOSURE_VALUE": "2.0"})


async def test_execute_script_capture_frame_rejects_a_non_numeric_runtime_gain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed gain (e.g. a buggy client passing a non-numeric string) must fail cleanly
    rather than being sent to CCD_GAIN as-is."""
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "capture",
        parameters={"gain": {"type": "string", "required": True}},
        steps=[
            {
                "step": "capture_frame",
                "role": "camera",
                "exposureSeconds": 5,
                "gain": "{{ gain }}",
            }
        ],
    )

    def get_property_values(device: str, name: str) -> dict[str, str] | None:
        if name == "CCD_GAIN":
            return {"placeholder": "value"}
        return _default_get_property_values(device, name)

    monkeypatch.setattr(indi_messaging, "get_property_values", get_property_values)
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)

    with pytest.raises(ValueError, match="could not convert string to float"):
        await script_engine.execute_script("capture", "test-rig", {"gain": "not-a-number"})

    send_property.assert_not_awaited()


async def test_execute_script_capture_frame_sets_sub_frame_roi_when_all_four_given(
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
                "frameX": 100,
                "frameY": 200,
                "frameWidth": 800,
                "frameHeight": 600,
            }
        ],
    )

    def get_property_values(device: str, name: str) -> dict[str, str] | None:
        if name == "CCD_FRAME":
            return {"placeholder": "value"}
        return _default_get_property_values(device, name)

    monkeypatch.setattr(indi_messaging, "get_property_values", get_property_values)
    send_property, _ = _mock_capture_frame_success(monkeypatch)

    await script_engine.execute_script("capture", "test-rig", {})

    send_property.assert_any_call(
        "CCD Simulator",
        "CCD_FRAME",
        {"X": "100", "Y": "200", "WIDTH": "800", "HEIGHT": "600"},
    )


async def test_execute_script_capture_frame_skips_sub_frame_roi_when_none_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "capture",
        steps=[{"step": "capture_frame", "role": "camera", "exposureSeconds": 5}],
    )

    def get_property_values(device: str, name: str) -> dict[str, str] | None:
        if name == "CCD_FRAME":
            return {"placeholder": "value"}
        return _default_get_property_values(device, name)

    monkeypatch.setattr(indi_messaging, "get_property_values", get_property_values)
    send_property, _ = _mock_capture_frame_success(monkeypatch)

    await script_engine.execute_script("capture", "test-rig", {})

    send_property.assert_awaited_once_with(
        "CCD Simulator", "CCD_EXPOSURE", {"CCD_EXPOSURE_VALUE": "5.0"}
    )


async def test_execute_script_capture_frame_skips_sub_frame_roi_when_undefined_on_device(
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
                "frameX": 0,
                "frameY": 0,
                "frameWidth": 800,
                "frameHeight": 600,
            }
        ],
    )
    send_property, _ = _mock_capture_frame_success(monkeypatch)

    await script_engine.execute_script("capture", "test-rig", {})

    send_property.assert_awaited_once_with(
        "CCD Simulator", "CCD_EXPOSURE", {"CCD_EXPOSURE_VALUE": "5.0"}
    )


async def test_execute_script_capture_frame_rejects_a_partial_sub_frame_roi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only frameWidth/frameHeight set, no frameX/frameY — doesn't map to a valid CCD_FRAME
    command, so this must fail loudly rather than silently capture the wrong region."""
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "capture",
        steps=[
            {
                "step": "capture_frame",
                "role": "camera",
                "exposureSeconds": 5,
                "frameWidth": 800,
                "frameHeight": 600,
            }
        ],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)

    with pytest.raises(
        script_engine.ScriptExecutionError,
        match="frameX/frameY/frameWidth/frameHeight must be set together",
    ):
        await script_engine.execute_script("capture", "test-rig", {})

    send_property.assert_not_awaited()


async def test_execute_script_capture_frame_rejects_a_non_numeric_runtime_frame_roi_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed sub-frame value (e.g. a buggy client passing a non-numeric frameWidth)
    must fail with a clear ScriptExecutionError, not a bare ValueError."""
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "capture",
        parameters={"frameWidth": {"type": "string", "required": True}},
        steps=[
            {
                "step": "capture_frame",
                "role": "camera",
                "exposureSeconds": 5,
                "frameX": 0,
                "frameY": 0,
                "frameWidth": "{{ frameWidth }}",
                "frameHeight": 600,
            }
        ],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)

    with pytest.raises(
        script_engine.ScriptExecutionError,
        match="frameX/frameY/frameWidth/frameHeight must all be valid integers",
    ):
        await script_engine.execute_script("capture", "test-rig", {"frameWidth": "not-a-number"})

    send_property.assert_not_awaited()


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


_CELESTIAL_CONTEXT: fits_headers.CelestialContext = {
    "targetAltitudeDeg": 45.0,
    "targetAzimuthDeg": 180.0,
    "airmass": 1.4142,
    "sunAltitudeDeg": -30.0,
    "moonSeparationDeg": 90.0,
    "moonIlluminationFraction": 0.5,
    "elongationDeg": 120.0,
}

_TARGET_POSITION: fits_headers.TargetPosition = {
    "raDegJ2000": 40.9799,
    "decDegJ2000": 62.4523,
    "raSexagesimalJ2000": "02 43 55.18",
    "decSexagesimalJ2000": "+62 27 08.28",
}

_DATE_OBS_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}$")


def _capture_frame_fields(
    monkeypatch: pytest.MonkeyPatch, write_result: bytes | None = b"fits-bytes-with-fields"
) -> MagicMock:
    """Capture whatever `fields` dict `_execute_capture_frame` builds, by mocking
    `fits_headers.write_fits_headers` and returning its `fields` argument from `call_args`.
    """
    write_headers = MagicMock(return_value=write_result)
    monkeypatch.setattr(fits_headers, "write_fits_headers", write_headers)
    return write_headers


async def test_execute_script_capture_frame_always_writes_date_obs_and_instrume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DATE-OBS/INSTRUME are written for every frame type — captured metadata, not tied to
    the mount's pointing or a location the way telescope position/celestial context are."""
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "capture",
        steps=[
            {
                "step": "capture_frame",
                "role": "camera",
                "exposureSeconds": 5,
                "frameType": "Dark",
            }
        ],
    )
    _, save_frame = _mock_capture_frame_success(monkeypatch)
    write_headers = _capture_frame_fields(monkeypatch)

    await script_engine.execute_script("capture", "test-rig", {})

    fields = write_headers.call_args.args[1]
    assert _DATE_OBS_PATTERN.match(fields["DATE-OBS"][0])
    assert fields["INSTRUME"] == ("CCD Simulator", "Camera (INDI device name)")
    assert "GAIN" not in fields
    assert "OFFSET" not in fields
    assert "RA" not in fields
    save_frame.assert_called_once_with(
        b"fits-bytes-with-fields", device="CCD Simulator", extension=".fits", run_id=None
    )


async def test_execute_script_capture_frame_writes_gain_and_offset_when_set(
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
                "gain": 100,
                "offset": 10,
            }
        ],
    )
    _mock_capture_frame_success(monkeypatch)
    write_headers = _capture_frame_fields(monkeypatch)

    await script_engine.execute_script("capture", "test-rig", {})

    fields = write_headers.call_args.args[1]
    assert fields["GAIN"] == (100.0, "Camera gain")
    assert fields["OFFSET"] == (10.0, "Camera offset")


async def test_execute_script_capture_frame_omits_gain_and_offset_when_not_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "capture",
        steps=[{"step": "capture_frame", "role": "camera", "exposureSeconds": 5}],
    )
    _mock_capture_frame_success(monkeypatch)
    write_headers = _capture_frame_fields(monkeypatch)

    await script_engine.execute_script("capture", "test-rig", {})

    fields = write_headers.call_args.args[1]
    assert "GAIN" not in fields
    assert "OFFSET" not in fields


async def test_execute_script_capture_frame_writes_filter_when_resolvable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(
        rig_store.Component(
            role="filterWheel", id="fw-1", device="ASI EFW", slots={1: "Ha", 2: "OIII"}
        ),
        rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"),
    )
    _script(
        "capture",
        steps=[{"step": "capture_frame", "role": "camera", "exposureSeconds": 5}],
    )

    def get_property_values(device: str, name: str) -> dict[str, str] | None:
        if device == "ASI EFW" and name == "FILTER_SLOT":
            return {"FILTER_SLOT_VALUE": "2"}
        return _default_get_property_values(device, name)

    monkeypatch.setattr(indi_messaging, "get_property_values", get_property_values)
    _mock_capture_frame_success(monkeypatch)
    write_headers = _capture_frame_fields(monkeypatch)

    await script_engine.execute_script("capture", "test-rig", {})

    fields = write_headers.call_args.args[1]
    assert fields["FILTER"] == ("OIII", "Filter name")


async def test_execute_script_capture_frame_omits_filter_when_rig_has_no_filter_wheel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "capture",
        steps=[{"step": "capture_frame", "role": "camera", "exposureSeconds": 5}],
    )
    _mock_capture_frame_success(monkeypatch)
    write_headers = _capture_frame_fields(monkeypatch)

    await script_engine.execute_script("capture", "test-rig", {})

    assert "FILTER" not in write_headers.call_args.args[1]


async def test_execute_script_capture_frame_omits_filter_when_slot_not_in_rig_slots_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wheel reports a slot that the rig's own `slots` map never named — same as not
    being able to resolve a filter name at all, not a crash."""
    _rig(
        rig_store.Component(role="filterWheel", id="fw-1", device="ASI EFW", slots={1: "Ha"}),
        rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"),
    )
    _script(
        "capture",
        steps=[{"step": "capture_frame", "role": "camera", "exposureSeconds": 5}],
    )

    def get_property_values(device: str, name: str) -> dict[str, str] | None:
        if device == "ASI EFW" and name == "FILTER_SLOT":
            return {"FILTER_SLOT_VALUE": "5"}
        return _default_get_property_values(device, name)

    monkeypatch.setattr(indi_messaging, "get_property_values", get_property_values)
    _mock_capture_frame_success(monkeypatch)
    write_headers = _capture_frame_fields(monkeypatch)

    await script_engine.execute_script("capture", "test-rig", {})

    assert "FILTER" not in write_headers.call_args.args[1]


async def test_execute_script_capture_frame_writes_filter_for_a_flat_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Flat is taken through a specific filter, same as a Light — calibrating that
    filter's illumination pattern is the whole point of it."""
    _rig(
        rig_store.Component(
            role="filterWheel", id="fw-1", device="ASI EFW", slots={1: "Ha", 2: "OIII"}
        ),
        rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"),
    )
    _script(
        "capture",
        steps=[
            {
                "step": "capture_frame",
                "role": "camera",
                "exposureSeconds": 5,
                "frameType": "Flat",
            }
        ],
    )

    def get_property_values(device: str, name: str) -> dict[str, str] | None:
        if device == "ASI EFW" and name == "FILTER_SLOT":
            return {"FILTER_SLOT_VALUE": "2"}
        return _default_get_property_values(device, name)

    monkeypatch.setattr(indi_messaging, "get_property_values", get_property_values)
    _mock_capture_frame_success(monkeypatch)
    write_headers = _capture_frame_fields(monkeypatch)

    await script_engine.execute_script("capture", "test-rig", {})

    assert write_headers.call_args.args[1]["FILTER"] == ("OIII", "Filter name")


@pytest.mark.parametrize("frame_type", ["Dark", "Bias"])
async def test_execute_script_capture_frame_omits_filter_for_dark_and_bias_frames(
    monkeypatch: pytest.MonkeyPatch, frame_type: str
) -> None:
    """A Dark/Bias is filter-independent (typically capped, sensor readout the same
    regardless of the optical path) — recording a filter would imply a dependency that
    doesn't exist, even though the filter wheel is otherwise perfectly resolvable."""
    _rig(
        rig_store.Component(
            role="filterWheel", id="fw-1", device="ASI EFW", slots={1: "Ha", 2: "OIII"}
        ),
        rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"),
    )
    _script(
        "capture",
        steps=[
            {
                "step": "capture_frame",
                "role": "camera",
                "exposureSeconds": 5,
                "frameType": frame_type,
            }
        ],
    )

    def get_property_values(device: str, name: str) -> dict[str, str] | None:
        if device == "ASI EFW" and name == "FILTER_SLOT":
            return {"FILTER_SLOT_VALUE": "2"}
        return _default_get_property_values(device, name)

    monkeypatch.setattr(indi_messaging, "get_property_values", get_property_values)
    _mock_capture_frame_success(monkeypatch)
    write_headers = _capture_frame_fields(monkeypatch)

    await script_engine.execute_script("capture", "test-rig", {})

    assert "FILTER" not in write_headers.call_args.args[1]


async def test_execute_script_capture_frame_writes_telescope_optics_fields_for_any_frame_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FOCALLEN/APTDIA/TELESCOP/SCALE are equipment metadata, not tied to frame type or
    pointing — should be present even for a Dark frame."""
    _rig(
        rig_store.Component(
            role="telescope",
            id="scope-1",
            make="Sky-Watcher",
            model="Esprit 100",
            apertureMm=100,
            focalLengthMm=550,
        ),
        rig_store.Component(
            role="camera", id="cam-1", device="CCD Simulator", pixelSizeMicron=3.76
        ),
    )
    _script(
        "capture",
        steps=[
            {
                "step": "capture_frame",
                "role": "camera",
                "exposureSeconds": 5,
                "frameType": "Dark",
            }
        ],
    )
    _mock_capture_frame_success(monkeypatch)
    write_headers = _capture_frame_fields(monkeypatch)

    await script_engine.execute_script("capture", "test-rig", {})

    fields = write_headers.call_args.args[1]
    assert fields["FOCALLEN"] == (550, "[mm] Telescope focal length")
    assert fields["APTDIA"] == (100, "[mm] Telescope aperture")
    assert fields["TELESCOP"] == ("Sky-Watcher Esprit 100", "Telescope")
    assert fields["SCALE"] == (round(206.265 * 3.76 / 550, 5), "[arcsec/pixel] Plate scale")


async def test_execute_script_capture_frame_omits_telescope_optics_when_rig_has_no_telescope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "capture",
        steps=[{"step": "capture_frame", "role": "camera", "exposureSeconds": 5}],
    )
    _mock_capture_frame_success(monkeypatch)
    write_headers = _capture_frame_fields(monkeypatch)

    await script_engine.execute_script("capture", "test-rig", {})

    fields = write_headers.call_args.args[1]
    assert "FOCALLEN" not in fields
    assert "APTDIA" not in fields
    assert "TELESCOP" not in fields
    assert "SCALE" not in fields


async def test_execute_script_capture_frame_omits_scale_when_camera_has_no_pixel_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FOCALLEN/APTDIA/TELESCOP don't need the camera at all, but SCALE needs both a
    focal length and a pixel size — should be independently best-effort."""
    _rig(
        rig_store.Component(role="telescope", id="scope-1", focalLengthMm=550),
        rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"),
    )
    _script(
        "capture",
        steps=[{"step": "capture_frame", "role": "camera", "exposureSeconds": 5}],
    )
    _mock_capture_frame_success(monkeypatch)
    write_headers = _capture_frame_fields(monkeypatch)

    await script_engine.execute_script("capture", "test-rig", {})

    fields = write_headers.call_args.args[1]
    assert fields["FOCALLEN"] == (550, "[mm] Telescope focal length")
    assert "SCALE" not in fields


async def test_execute_script_capture_frame_writes_focuser_fields_when_resolvable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(
        rig_store.Component(role="focuser", id="focus-1", device="Focuser Simulator"),
        rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"),
    )
    _script(
        "capture",
        steps=[{"step": "capture_frame", "role": "camera", "exposureSeconds": 5}],
    )

    def get_property_values(device: str, name: str) -> dict[str, str] | None:
        if device == "Focuser Simulator" and name == "ABS_FOCUS_POSITION":
            return {"FOCUS_ABSOLUTE_POSITION": "12345"}
        if device == "Focuser Simulator" and name == "FOCUS_TEMPERATURE":
            return {"TEMPERATURE": "18.347"}
        return _default_get_property_values(device, name)

    monkeypatch.setattr(indi_messaging, "get_property_values", get_property_values)
    _mock_capture_frame_success(monkeypatch)
    write_headers = _capture_frame_fields(monkeypatch)

    await script_engine.execute_script("capture", "test-rig", {})

    fields = write_headers.call_args.args[1]
    assert fields["FOCUSPOS"] == (12345, "Focuser position in steps")
    assert fields["FOCUSTEM"] == (18.35, "[C] Focuser temperature")


async def test_execute_script_capture_frame_writes_focuser_position_without_temperature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A focuser reporting a position but not a temperature (no temperature probe) is
    common — the two fields must be independently best-effort."""
    _rig(
        rig_store.Component(role="focuser", id="focus-1", device="Focuser Simulator"),
        rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"),
    )
    _script(
        "capture",
        steps=[{"step": "capture_frame", "role": "camera", "exposureSeconds": 5}],
    )

    def get_property_values(device: str, name: str) -> dict[str, str] | None:
        if device == "Focuser Simulator" and name == "ABS_FOCUS_POSITION":
            return {"FOCUS_ABSOLUTE_POSITION": "12345"}
        return _default_get_property_values(device, name)

    monkeypatch.setattr(indi_messaging, "get_property_values", get_property_values)
    _mock_capture_frame_success(monkeypatch)
    write_headers = _capture_frame_fields(monkeypatch)

    await script_engine.execute_script("capture", "test-rig", {})

    fields = write_headers.call_args.args[1]
    assert fields["FOCUSPOS"] == (12345, "Focuser position in steps")
    assert "FOCUSTEM" not in fields


async def test_execute_script_capture_frame_omits_focuser_fields_when_rig_has_no_focuser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "capture",
        steps=[{"step": "capture_frame", "role": "camera", "exposureSeconds": 5}],
    )
    _mock_capture_frame_success(monkeypatch)
    write_headers = _capture_frame_fields(monkeypatch)

    await script_engine.execute_script("capture", "test-rig", {})

    fields = write_headers.call_args.args[1]
    assert "FOCUSPOS" not in fields
    assert "FOCUSTEM" not in fields


async def test_execute_script_capture_frame_writes_site_lat_long_for_any_frame_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SITELAT/SITELONG only need a resolvable observatory, not a Light frame or a mount —
    should be present even for a Dark frame."""
    _observatory()
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "capture",
        steps=[
            {
                "step": "capture_frame",
                "role": "camera",
                "exposureSeconds": 5,
                "frameType": "Dark",
            }
        ],
    )
    _mock_capture_frame_success(monkeypatch)
    write_headers = _capture_frame_fields(monkeypatch)

    await script_engine.execute_script("capture", "test-rig", {}, location_id="test-observatory")

    fields = write_headers.call_args.args[1]
    assert fields["SITELAT"] == (60.369722, "[deg] Observatory latitude")
    assert fields["SITELONG"] == (11.363611, "[deg] Observatory longitude")


async def test_execute_script_capture_frame_omits_site_lat_long_when_no_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "capture",
        steps=[{"step": "capture_frame", "role": "camera", "exposureSeconds": 5}],
    )
    _mock_capture_frame_success(monkeypatch)
    write_headers = _capture_frame_fields(monkeypatch)

    await script_engine.execute_script("capture", "test-rig", {})

    fields = write_headers.call_args.args[1]
    assert "SITELAT" not in fields
    assert "SITELONG" not in fields


async def test_execute_script_capture_frame_writes_pier_side_independent_of_coordinates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PIERSIDE only needs `TELESCOPE_PIER_SIDE` — should be written even when
    `EQUATORIAL_EOD_COORD` is undefined, since it's a best-effort field of its own."""
    _rig(
        rig_store.Component(role="mount", id="mount-1", device="EQMod Mount"),
        rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"),
    )
    _script(
        "capture",
        steps=[{"step": "capture_frame", "role": "camera", "exposureSeconds": 5}],
    )

    def get_property_values(device: str, name: str) -> dict[str, str] | None:
        if device == "EQMod Mount" and name == "TELESCOPE_PIER_SIDE":
            return {"PIER_WEST": "On", "PIER_EAST": "Off"}
        return _default_get_property_values(device, name)

    monkeypatch.setattr(indi_messaging, "get_property_values", get_property_values)
    _mock_capture_frame_success(monkeypatch)
    write_headers = _capture_frame_fields(monkeypatch)

    await script_engine.execute_script("capture", "test-rig", {})

    fields = write_headers.call_args.args[1]
    assert fields["PIERSIDE"] == ("WEST", "Mount pier side")


async def test_execute_script_capture_frame_writes_pier_side_east(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(
        rig_store.Component(role="mount", id="mount-1", device="EQMod Mount"),
        rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"),
    )
    _script(
        "capture",
        steps=[{"step": "capture_frame", "role": "camera", "exposureSeconds": 5}],
    )

    def get_property_values(device: str, name: str) -> dict[str, str] | None:
        if device == "EQMod Mount" and name == "TELESCOPE_PIER_SIDE":
            return {"PIER_WEST": "Off", "PIER_EAST": "On"}
        return _default_get_property_values(device, name)

    monkeypatch.setattr(indi_messaging, "get_property_values", get_property_values)
    _mock_capture_frame_success(monkeypatch)
    write_headers = _capture_frame_fields(monkeypatch)

    await script_engine.execute_script("capture", "test-rig", {})

    fields = write_headers.call_args.args[1]
    assert fields["PIERSIDE"] == ("EAST", "Mount pier side")


async def test_execute_script_capture_frame_omits_pier_side_when_undefined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(
        rig_store.Component(role="mount", id="mount-1", device="EQMod Mount"),
        rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"),
    )
    _script(
        "capture",
        steps=[{"step": "capture_frame", "role": "camera", "exposureSeconds": 5}],
    )
    _mock_capture_frame_success(monkeypatch)
    write_headers = _capture_frame_fields(monkeypatch)

    await script_engine.execute_script("capture", "test-rig", {})

    assert "PIERSIDE" not in write_headers.call_args.args[1]


async def test_execute_script_capture_frame_writes_object_for_a_light_frame(
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
                "frameType": "Light",
                "objectName": "M31",
            }
        ],
    )
    _mock_capture_frame_success(monkeypatch)
    write_headers = _capture_frame_fields(monkeypatch)

    await script_engine.execute_script("capture", "test-rig", {})

    assert write_headers.call_args.args[1]["OBJECT"] == ("M31", "Object")


@pytest.mark.parametrize("frame_type", ["Dark", "Bias", "Flat"])
async def test_execute_script_capture_frame_omits_object_for_a_calibration_frame(
    monkeypatch: pytest.MonkeyPatch, frame_type: str
) -> None:
    """`objectName` is a caller-supplied label for what the light frame targets — it
    doesn't mean anything for a calibration frame even if supplied."""
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "capture",
        steps=[
            {
                "step": "capture_frame",
                "role": "camera",
                "exposureSeconds": 5,
                "frameType": frame_type,
                "objectName": "M31",
            }
        ],
    )
    _mock_capture_frame_success(monkeypatch)
    write_headers = _capture_frame_fields(monkeypatch)

    await script_engine.execute_script("capture", "test-rig", {})

    assert "OBJECT" not in write_headers.call_args.args[1]


async def test_execute_script_capture_frame_omits_object_when_object_name_not_given(
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
                "frameType": "Light",
            }
        ],
    )
    _mock_capture_frame_success(monkeypatch)
    write_headers = _capture_frame_fields(monkeypatch)

    await script_engine.execute_script("capture", "test-rig", {})

    assert "OBJECT" not in write_headers.call_args.args[1]


async def test_execute_script_capture_frame_writes_telescope_position_and_celestial_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observatory = _observatory()
    _rig(
        rig_store.Component(role="mount", id="mount-1", device="EQMod Mount"),
        rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"),
    )
    _script(
        "capture",
        steps=[{"step": "capture_frame", "role": "camera", "exposureSeconds": 5}],
    )

    def get_property_values(device: str, name: str) -> dict[str, str] | None:
        if device == "EQMod Mount" and name == "EQUATORIAL_EOD_COORD":
            return {"RA": "2.767", "DEC": "62.52"}
        return _default_get_property_values(device, name)

    monkeypatch.setattr(indi_messaging, "get_property_values", get_property_values)
    _, save_frame = _mock_capture_frame_success(monkeypatch)
    compute_position = MagicMock(return_value=_TARGET_POSITION)
    monkeypatch.setattr(fits_headers, "compute_target_position", compute_position)
    compute_context = MagicMock(return_value=_CELESTIAL_CONTEXT)
    monkeypatch.setattr(fits_headers, "compute_celestial_context", compute_context)
    write_headers = _capture_frame_fields(monkeypatch)

    await script_engine.execute_script("capture", "test-rig", {}, location_id="test-observatory")

    compute_position.assert_called_once()
    assert compute_position.call_args.kwargs["ra_hours"] == 2.767
    assert compute_position.call_args.kwargs["dec_deg"] == 62.52
    compute_context.assert_called_once()
    assert compute_context.call_args.kwargs["ra_hours"] == 2.767
    assert compute_context.call_args.kwargs["dec_deg"] == 62.52
    assert compute_context.call_args.kwargs["observatory"] == observatory
    assert isinstance(compute_context.call_args.kwargs["at"], datetime)
    fields = write_headers.call_args.args[1]
    assert fields["RA"] == (40.9799, "Object J2000 RA in Degrees")
    assert fields["DEC"] == (62.4523, "Object J2000 DEC in Degrees")
    assert fields["OBJCTRA"] == ("02 43 55.18", "Object J2000 RA in Hours")
    assert fields["OBJCTDEC"] == ("+62 27 08.28", "Object J2000 DEC in Degrees")
    assert fields["OBJCTALT"] == (45.0, "[deg] Target altitude at obs time")
    assert fields["SUNALT"] == (-30.0, "[deg] Sun altitude at obs time")
    assert fields["MOONSEP"] == (90.0, "[deg] Moon-target angular separation")
    assert fields["MOONPHSE"] == (0.5, "Moon illumination fraction [0-1]")
    assert fields["ELONGAT"] == (120.0, "[deg] Sun-target elongation")
    save_frame.assert_called_once_with(
        b"fits-bytes-with-fields", device="CCD Simulator", extension=".fits", run_id=None
    )


async def test_execute_script_capture_frame_writes_position_without_context_when_no_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Telescope position isn't tied to having a location at all — only the Sun/Moon
    context (which needs an observer location to compute Alt-Az from) is."""
    _rig(
        rig_store.Component(role="mount", id="mount-1", device="EQMod Mount"),
        rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"),
    )
    _script(
        "capture",
        steps=[{"step": "capture_frame", "role": "camera", "exposureSeconds": 5}],
    )

    def get_property_values(device: str, name: str) -> dict[str, str] | None:
        if device == "EQMod Mount" and name == "EQUATORIAL_EOD_COORD":
            return {"RA": "2.767", "DEC": "62.52"}
        return _default_get_property_values(device, name)

    monkeypatch.setattr(indi_messaging, "get_property_values", get_property_values)
    _mock_capture_frame_success(monkeypatch)
    compute_position = MagicMock(return_value=_TARGET_POSITION)
    monkeypatch.setattr(fits_headers, "compute_target_position", compute_position)
    compute_context = MagicMock()
    monkeypatch.setattr(fits_headers, "compute_celestial_context", compute_context)
    write_headers = _capture_frame_fields(monkeypatch)

    await script_engine.execute_script("capture", "test-rig", {})

    compute_position.assert_called_once()
    compute_context.assert_not_called()
    fields = write_headers.call_args.args[1]
    assert fields["RA"] == (40.9799, "Object J2000 RA in Degrees")
    assert fields["DEC"] == (62.4523, "Object J2000 DEC in Degrees")
    assert "SUNALT" not in fields


async def test_execute_script_capture_frame_skips_telescope_position_for_a_calibration_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Dark/Flat/Bias frame isn't captured "of" anything at the mount's current pointing
    in any meaningful sense — telescope position and celestial context should both be
    skipped, even when location, mount, and coordinates are all otherwise available."""
    _observatory()
    _rig(
        rig_store.Component(role="mount", id="mount-1", device="EQMod Mount"),
        rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"),
    )
    _script(
        "capture",
        steps=[
            {
                "step": "capture_frame",
                "role": "camera",
                "exposureSeconds": 5,
                "frameType": "Dark",
            }
        ],
    )

    def get_property_values(device: str, name: str) -> dict[str, str] | None:
        if device == "EQMod Mount" and name == "EQUATORIAL_EOD_COORD":
            return {"RA": "2.767", "DEC": "62.52"}
        return _default_get_property_values(device, name)

    monkeypatch.setattr(indi_messaging, "get_property_values", get_property_values)
    _, save_frame = _mock_capture_frame_success(monkeypatch)
    compute = MagicMock()
    monkeypatch.setattr(fits_headers, "compute_celestial_context", compute)
    write_headers = _capture_frame_fields(monkeypatch)

    await script_engine.execute_script("capture", "test-rig", {}, location_id="test-observatory")

    compute.assert_not_called()
    fields = write_headers.call_args.args[1]
    assert "RA" not in fields
    assert "DEC" not in fields
    assert "SUNALT" not in fields
    save_frame.assert_called_once_with(
        b"fits-bytes-with-fields", device="CCD Simulator", extension=".fits", run_id=None
    )


async def test_execute_script_capture_frame_skips_telescope_position_when_rig_has_no_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rig with no `"mount"` component (e.g. a camera-only test rig) simply has nothing
    to report a pointing for — not an error."""
    _observatory()
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "capture",
        steps=[{"step": "capture_frame", "role": "camera", "exposureSeconds": 5}],
    )
    _mock_capture_frame_success(monkeypatch)
    compute = MagicMock()
    monkeypatch.setattr(fits_headers, "compute_celestial_context", compute)
    write_headers = _capture_frame_fields(monkeypatch)

    await script_engine.execute_script("capture", "test-rig", {}, location_id="test-observatory")

    compute.assert_not_called()
    assert "RA" not in write_headers.call_args.args[1]


async def test_execute_script_capture_frame_skips_telescope_position_when_mount_coords_undefined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mount is resolvable but isn't reporting `EQUATORIAL_EOD_COORD` at all (e.g. not
    connected) — matches `_default_get_property_values`'s "undefined" default."""
    _observatory()
    _rig(
        rig_store.Component(role="mount", id="mount-1", device="EQMod Mount"),
        rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"),
    )
    _script(
        "capture",
        steps=[{"step": "capture_frame", "role": "camera", "exposureSeconds": 5}],
    )
    _mock_capture_frame_success(monkeypatch)
    compute = MagicMock()
    monkeypatch.setattr(fits_headers, "compute_celestial_context", compute)
    write_headers = _capture_frame_fields(monkeypatch)

    await script_engine.execute_script("capture", "test-rig", {}, location_id="test-observatory")

    compute.assert_not_called()
    assert "RA" not in write_headers.call_args.args[1]


async def test_execute_script_capture_frame_skips_telescope_position_when_mount_coords_unparseable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _observatory()
    _rig(
        rig_store.Component(role="mount", id="mount-1", device="EQMod Mount"),
        rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"),
    )
    _script(
        "capture",
        steps=[{"step": "capture_frame", "role": "camera", "exposureSeconds": 5}],
    )

    def get_property_values(device: str, name: str) -> dict[str, str] | None:
        if device == "EQMod Mount" and name == "EQUATORIAL_EOD_COORD":
            return {"RA": "not-a-number", "DEC": "62.52"}
        return _default_get_property_values(device, name)

    monkeypatch.setattr(indi_messaging, "get_property_values", get_property_values)
    _mock_capture_frame_success(monkeypatch)
    compute = MagicMock()
    monkeypatch.setattr(fits_headers, "compute_celestial_context", compute)
    write_headers = _capture_frame_fields(monkeypatch)

    await script_engine.execute_script("capture", "test-rig", {}, location_id="test-observatory")

    compute.assert_not_called()
    assert "RA" not in write_headers.call_args.args[1]


async def test_execute_script_capture_frame_saves_unmodified_data_when_not_a_fits_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`write_fits_headers` returning `None` (not a FITS file) falls back to the drained
    bytes unmodified — the same file that would have been saved before this feature existed."""
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "capture",
        steps=[{"step": "capture_frame", "role": "camera", "exposureSeconds": 5}],
    )
    _, save_frame = _mock_capture_frame_success(monkeypatch)
    _capture_frame_fields(monkeypatch, write_result=None)

    await script_engine.execute_script("capture", "test-rig", {})

    save_frame.assert_called_once_with(
        b"fits-bytes", device="CCD Simulator", extension=".fits", run_id=None
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
        rig_id="test-rig",
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


async def test_wait_for_property_state_without_require_transition_accepts_stale_ok_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default (`require_transition=False`) behavior is unchanged: a vector already sitting
    at `target_state` when polling starts is accepted right away — matching `slew`/
    `capture_frame`'s existing, already-correct-in-practice usage (INDIMCP-82).
    """
    calls = 0

    def get_property_state(device: str, name: str) -> str:
        nonlocal calls
        calls += 1
        return "Ok"

    monkeypatch.setattr(indi_messaging, "get_property_state", get_property_state)
    ctx = script_engine._ExecutionContext(
        rig_id="test-rig",
        role_to_device={},
        cancel_event=None,
        pause_event=None,
        on_progress=None,
        total_steps=None,
        scripts={},
        run_id=None,
    )

    await asyncio.wait_for(
        script_engine._wait_for_property_state(
            ctx, "CCD Simulator", "CCD_EXPOSURE", indi_messaging.PropertyState.OK, 60.0
        ),
        timeout=1.0,
    )

    assert calls == 1


async def test_wait_for_property_state_require_transition_waits_past_stale_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`require_transition=True` (used by `cool_camera`, INDIMCP-82) guards against the race
    where `send_property` returns before the driver has processed the command: a vector
    already sitting at `target_state` when polling starts must first be observed leaving it
    (e.g. to `Busy`) before a later `target_state` is accepted as genuine completion — rather
    than treating a stale pre-command reading as if the new command had already finished.
    """
    states = iter(["Ok", "Ok", "Busy", "Ok"])
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: next(states))
    monkeypatch.setattr(script_engine, "_WAIT_POLL_INTERVAL_SECONDS", 0.001)
    ctx = script_engine._ExecutionContext(
        rig_id="test-rig",
        role_to_device={},
        cancel_event=None,
        pause_event=None,
        on_progress=None,
        total_steps=None,
        scripts={},
        run_id=None,
    )

    await asyncio.wait_for(
        script_engine._wait_for_property_state(
            ctx,
            "CCD Simulator",
            "CCD_TEMPERATURE",
            indi_messaging.PropertyState.OK,
            60.0,
            require_transition=True,
        ),
        timeout=1.0,
    )

    with pytest.raises(StopIteration):
        next(states)


async def test_wait_for_property_state_require_transition_times_out_if_never_leaves_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the vector never leaves `target_state` at all — the driver never actually reacted to
    the command — `require_transition=True` times out rather than waiting forever.
    """
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Ok")
    monkeypatch.setattr(script_engine, "_WAIT_POLL_INTERVAL_SECONDS", 0.001)
    ctx = script_engine._ExecutionContext(
        rig_id="test-rig",
        role_to_device={},
        cancel_event=None,
        pause_event=None,
        on_progress=None,
        total_steps=None,
        scripts={},
        run_id=None,
    )

    with pytest.raises(script_engine.ScriptExecutionError, match="never left"):
        await asyncio.wait_for(
            script_engine._wait_for_property_state(
                ctx,
                "CCD Simulator",
                "CCD_TEMPERATURE",
                indi_messaging.PropertyState.OK,
                0.01,
                require_transition=True,
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


async def test_execute_script_cool_camera_turns_on_cooler_sets_temp_and_waits_for_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "cool_camera",
        steps=[{"step": "cool_camera", "role": "camera", "targetTempC": -10.0}],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)
    monkeypatch.setattr(
        indi_messaging,
        "get_property_values",
        lambda device, name: (
            {"COOLER_ON": "Off", "COOLER_OFF": "On"}
            if name == "CCD_COOLER"
            else _default_get_property_values(device, name)
        ),
    )
    states = iter(["Busy", "Busy", "Ok"])
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: next(states))
    monkeypatch.setattr(script_engine, "_WAIT_POLL_INTERVAL_SECONDS", 0.001)

    result = await script_engine.execute_script("cool_camera", "test-rig", {})

    assert send_property.await_args_list == [
        call("CCD Simulator", "CCD_COOLER", {"COOLER_ON": "On"}),
        call("CCD Simulator", "CCD_TEMPERATURE", {"CCD_TEMPERATURE_VALUE": "-10.0"}),
    ]
    assert result["stepsExecuted"] == 1


async def test_execute_script_cool_camera_substitutes_parameter_reference_in_target_temp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "cool_camera",
        parameters={"targetTempC": {"type": "number", "required": True}},
        steps=[{"step": "cool_camera", "role": "camera", "targetTempC": "{{ targetTempC }}"}],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)
    states = iter(["Ok", "Busy", "Ok"])
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: next(states))
    monkeypatch.setattr(script_engine, "_WAIT_POLL_INTERVAL_SECONDS", 0.001)

    await script_engine.execute_script("cool_camera", "test-rig", {"targetTempC": -15.0})

    send_property.assert_awaited_once_with(
        "CCD Simulator", "CCD_TEMPERATURE", {"CCD_TEMPERATURE_VALUE": "-15.0"}
    )


async def test_execute_script_cool_camera_skips_cooler_when_camera_has_no_such_property(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A camera driver with no CCD_COOLER support at all (no active cooling) is skipped."""
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "cool_camera",
        steps=[{"step": "cool_camera", "role": "camera", "targetTempC": -10.0}],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)
    states = iter(["Ok", "Busy", "Ok"])
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: next(states))
    monkeypatch.setattr(script_engine, "_WAIT_POLL_INTERVAL_SECONDS", 0.001)

    await script_engine.execute_script("cool_camera", "test-rig", {})

    send_property.assert_awaited_once_with(
        "CCD Simulator", "CCD_TEMPERATURE", {"CCD_TEMPERATURE_VALUE": "-10.0"}
    )


async def test_execute_script_cool_camera_times_out_waiting_for_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "cool_camera",
        steps=[
            {
                "step": "cool_camera",
                "role": "camera",
                "targetTempC": -10.0,
                "timeoutSeconds": 0.01,
            }
        ],
    )
    monkeypatch.setattr(indi_messaging, "send_property", AsyncMock())
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Busy")
    monkeypatch.setattr(script_engine, "_WAIT_POLL_INTERVAL_SECONDS", 0.001)

    with pytest.raises(script_engine.ScriptExecutionError, match="did not reach"):
        await script_engine.execute_script("cool_camera", "test-rig", {})


async def test_execute_script_cool_camera_fails_fast_on_temperature_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A driver-reported `Alert` (e.g. a cooler fault) shouldn't be waited out for the full
    timeout — it's not something more polling will resolve.
    """
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "cool_camera",
        steps=[{"step": "cool_camera", "role": "camera", "targetTempC": -10.0}],
    )
    monkeypatch.setattr(indi_messaging, "send_property", AsyncMock())
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Alert")
    monkeypatch.setattr(script_engine, "_WAIT_POLL_INTERVAL_SECONDS", 0.001)

    with pytest.raises(script_engine.ScriptExecutionError, match="went to Alert"):
        await asyncio.wait_for(
            script_engine.execute_script("cool_camera", "test-rig", {}), timeout=1.0
        )


async def test_execute_script_select_filter_by_slot_sets_filter_slot_and_waits_for_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="filterWheel", id="fw-1", device="Filter Wheel Simulator"))
    _script(
        "select_filter",
        steps=[{"step": "select_filter", "role": "filterWheel", "slot": 3}],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)
    states = iter(["Busy", "Ok"])
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: next(states))
    monkeypatch.setattr(script_engine, "_WAIT_POLL_INTERVAL_SECONDS", 0.001)

    result = await script_engine.execute_script("select_filter", "test-rig", {})

    send_property.assert_awaited_once_with(
        "Filter Wheel Simulator", "FILTER_SLOT", {"FILTER_SLOT_VALUE": "3"}
    )
    assert result["stepsExecuted"] == 1


async def test_execute_script_select_filter_by_name_resolves_slot_from_rig_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(
        rig_store.Component(
            role="filterWheel",
            id="fw-1",
            device="Filter Wheel Simulator",
            slots={1: "Luminance", 2: "Red", 3: "Green"},
        )
    )
    _script(
        "select_filter",
        steps=[{"step": "select_filter", "role": "filterWheel", "filterName": "Red"}],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Ok")

    await script_engine.execute_script("select_filter", "test-rig", {})

    send_property.assert_awaited_once_with(
        "Filter Wheel Simulator", "FILTER_SLOT", {"FILTER_SLOT_VALUE": "2"}
    )


async def test_execute_script_select_filter_substitutes_parameter_reference_in_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="filterWheel", id="fw-1", device="Filter Wheel Simulator"))
    _script(
        "select_filter",
        parameters={"slot": {"type": "integer", "required": True}},
        steps=[{"step": "select_filter", "role": "filterWheel", "slot": "{{ slot }}"}],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Ok")

    await script_engine.execute_script("select_filter", "test-rig", {"slot": 5})

    send_property.assert_awaited_once_with(
        "Filter Wheel Simulator", "FILTER_SLOT", {"FILTER_SLOT_VALUE": "5"}
    )


async def test_execute_script_select_filter_by_name_raises_for_an_unknown_filter_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(
        rig_store.Component(
            role="filterWheel", id="fw-1", device="Filter Wheel Simulator", slots={1: "Luminance"}
        )
    )
    _script(
        "select_filter",
        steps=[{"step": "select_filter", "role": "filterWheel", "filterName": "Ha"}],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)

    with pytest.raises(script_engine.ScriptExecutionError, match="no slot named 'Ha'"):
        await script_engine.execute_script("select_filter", "test-rig", {})

    send_property.assert_not_awaited()


async def test_execute_script_select_filter_by_name_raises_when_rig_has_no_slots_defined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A filter wheel component with no `slots` map at all (unset/unknown) is the same case as
    an unknown name, not a crash."""
    _rig(rig_store.Component(role="filterWheel", id="fw-1", device="Filter Wheel Simulator"))
    _script(
        "select_filter",
        steps=[{"step": "select_filter", "role": "filterWheel", "filterName": "Luminance"}],
    )
    monkeypatch.setattr(indi_messaging, "send_property", AsyncMock())

    with pytest.raises(script_engine.ScriptExecutionError, match="no slot named 'Luminance'"):
        await script_engine.execute_script("select_filter", "test-rig", {})


async def test_execute_script_select_filter_by_name_raises_for_an_ambiguous_filter_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A misconfigured rig with two slots sharing the same filter name is a hard error, not a
    silent pick of whichever slot happens to come first in iteration order."""
    _rig(
        rig_store.Component(
            role="filterWheel",
            id="fw-1",
            device="Filter Wheel Simulator",
            slots={1: "Luminance", 4: "Luminance"},
        )
    )
    _script(
        "select_filter",
        steps=[{"step": "select_filter", "role": "filterWheel", "filterName": "Luminance"}],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)

    with pytest.raises(
        script_engine.ScriptExecutionError, match="more than one slot named 'Luminance'"
    ):
        await script_engine.execute_script("select_filter", "test-rig", {})

    send_property.assert_not_awaited()


async def test_execute_script_select_filter_reports_no_warning_when_driver_matches_rig(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(
        rig_store.Component(
            role="filterWheel",
            id="fw-1",
            device="Filter Wheel Simulator",
            slots={1: "Luminance", 2: "Red"},
        )
    )
    _script(
        "select_filter",
        steps=[{"step": "select_filter", "role": "filterWheel", "filterName": "Red"}],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Ok")
    monkeypatch.setattr(
        indi_messaging,
        "get_property_values",
        lambda device, name: (
            {"FILTER_SLOT_NAME_1": "Luminance", "FILTER_SLOT_NAME_2": "Red"}
            if name == "FILTER_NAME"
            else _default_get_property_values(device, name)
        ),
    )

    result = await script_engine.execute_script("select_filter", "test-rig", {})

    assert result["warnings"] == []
    send_property.assert_awaited_once_with(
        "Filter Wheel Simulator", "FILTER_SLOT", {"FILTER_SLOT_VALUE": "2"}
    )


async def test_execute_script_select_filter_fails_fatally_when_driver_disagrees_with_rig(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The EFW driver's own live `FILTER_NAME` config can drift from the rig's configured
    `slots` map (e.g. someone reconfigured the wheel from a different client) — INDIMCP-64
    treats this as `FATAL` rather than a warning: selecting a filter under a config mismatch
    risks moving the wrong physical filter into the light path, so the step must not proceed
    (or touch the driver) until an operator makes the rig and the driver agree — whether by
    hand-editing the rig, reconfiguring the driver, or explicitly pushing the rig's config to
    the driver via the `sync_filter_names` tool/step."""
    _rig(
        rig_store.Component(
            role="filterWheel",
            id="fw-1",
            device="Filter Wheel Simulator",
            slots={1: "Luminance", 2: "Red"},
        )
    )
    _script(
        "select_filter",
        steps=[{"step": "select_filter", "role": "filterWheel", "filterName": "Red"}],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Ok")
    monkeypatch.setattr(
        indi_messaging,
        "get_property_values",
        lambda device, name: (
            {"FILTER_SLOT_NAME_1": "Luminance", "FILTER_SLOT_NAME_2": "Green"}
            if name == "FILTER_NAME"
            else _default_get_property_values(device, name)
        ),
    )

    with pytest.raises(script_engine.ScriptExecutionError) as exc_info:
        await script_engine.execute_script("select_filter", "test-rig", {})

    # Nothing was pushed to the driver, and the filter was never selected either.
    send_property.assert_not_awaited()
    assert len(exc_info.value.warnings) == 1
    fatal = exc_info.value.warnings[0]
    assert fatal["kind"] == "issue"
    assert fatal["severity"] == script_engine.Severity.FATAL
    assert fatal["code"] == "filterConfigMismatch"
    assert fatal["role"] == "filterWheel"
    assert fatal["device"] == "Filter Wheel Simulator"


async def test_execute_script_select_filter_skips_the_check_when_driver_lacks_filter_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not every EFW driver exposes `FILTER_NAME` — best-effort skip (no warning), matching
    every other optional-property check in this module (e.g. `_check_not_parked`)."""
    _rig(
        rig_store.Component(
            role="filterWheel", id="fw-1", device="Filter Wheel Simulator", slots={1: "Luminance"}
        )
    )
    _script(
        "select_filter",
        steps=[{"step": "select_filter", "role": "filterWheel", "slot": 1}],
    )
    monkeypatch.setattr(indi_messaging, "send_property", AsyncMock())
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Ok")

    result = await script_engine.execute_script("select_filter", "test-rig", {})

    assert result["warnings"] == []


async def test_execute_script_select_filter_copies_driver_slots_onto_rig_when_rig_has_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rig component with no `slots` map at all has no rig-authored intent to contradict —
    so when the driver's own `FILTER_NAME` is non-empty, INDIMCP-64 adopts the driver's slot
    names onto the rig (persisted via `rig_store.update_component_slots`, and into
    `ctx.role_to_slots` so a `filterName` lookup later in the *same* run already sees them)
    rather than treating this as a config mismatch, and reports an `INFO` issue."""
    _rig(rig_store.Component(role="filterWheel", id="fw-1", device="Filter Wheel Simulator"))
    _script(
        "select_filter",
        steps=[{"step": "select_filter", "role": "filterWheel", "filterName": "Red"}],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Ok")
    monkeypatch.setattr(
        indi_messaging,
        "get_property_values",
        lambda device, name: (
            {"FILTER_SLOT_NAME_1": "Luminance", "FILTER_SLOT_NAME_2": "Red"}
            if name == "FILTER_NAME"
            else _default_get_property_values(device, name)
        ),
    )
    update_component_slots = MagicMock()
    monkeypatch.setattr(rig_store, "update_component_slots", update_component_slots)

    result = await script_engine.execute_script("select_filter", "test-rig", {})

    update_component_slots.assert_called_once_with(
        "test-rig", "filterWheel", {1: "Luminance", 2: "Red"}
    )
    # filterName="Red" resolved against the freshly-copied slots, in the same run.
    send_property.assert_awaited_once_with(
        "Filter Wheel Simulator", "FILTER_SLOT", {"FILTER_SLOT_VALUE": "2"}
    )
    assert len(result["warnings"]) == 1
    issue = result["warnings"][0]
    assert issue["kind"] == "issue"
    assert issue["severity"] == script_engine.Severity.INFO
    assert issue["code"] == "filterSlotsCopiedFromDriver"
    assert issue["role"] == "filterWheel"
    assert issue["device"] == "Filter Wheel Simulator"


async def test_execute_script_select_filter_wraps_a_failed_slot_copy_as_script_execution_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure persisting the copied slots (disk full, permission denied, ...) must not leak
    a bare exception out of `execute_script` — this module's documented exception contract
    (`ScriptValidationError`/`ScriptPreconditionError`/`ScriptExecutionError`/`ScriptCancelled`,
    never anything else) applies here too. `ctx.role_to_slots` is left untouched since the
    persist never actually succeeded."""
    _rig(rig_store.Component(role="filterWheel", id="fw-1", device="Filter Wheel Simulator"))
    _script(
        "select_filter",
        steps=[{"step": "select_filter", "role": "filterWheel", "filterName": "Red"}],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Ok")
    monkeypatch.setattr(
        indi_messaging,
        "get_property_values",
        lambda device, name: (
            {"FILTER_SLOT_NAME_1": "Luminance", "FILTER_SLOT_NAME_2": "Red"}
            if name == "FILTER_NAME"
            else _default_get_property_values(device, name)
        ),
    )
    monkeypatch.setattr(
        rig_store,
        "update_component_slots",
        MagicMock(side_effect=OSError("disk full")),
    )

    with pytest.raises(script_engine.ScriptExecutionError, match="failed to persist filter slots"):
        await script_engine.execute_script("select_filter", "test-rig", {})

    send_property.assert_not_awaited()


async def test_execute_script_select_filter_skips_the_check_when_neither_side_has_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither the rig nor the driver declares any filter slots (the driver's `FILTER_NAME` is
    defined but has no recognized `FILTER_SLOT_NAME_<n>` members at all) — nothing to
    reconcile either way, so a plain numeric `step.slot` selection proceeds with no issue and
    no copy."""
    _rig(rig_store.Component(role="filterWheel", id="fw-1", device="Filter Wheel Simulator"))
    _script(
        "select_filter",
        steps=[{"step": "select_filter", "role": "filterWheel", "slot": 1}],
    )
    monkeypatch.setattr(indi_messaging, "send_property", AsyncMock())
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Ok")
    monkeypatch.setattr(
        indi_messaging,
        "get_property_values",
        lambda device, name: (
            {} if name == "FILTER_NAME" else _default_get_property_values(device, name)
        ),
    )
    update_component_slots = MagicMock()
    monkeypatch.setattr(rig_store, "update_component_slots", update_component_slots)

    result = await script_engine.execute_script("select_filter", "test-rig", {})

    assert result["warnings"] == []
    update_component_slots.assert_not_called()


def test_report_issue_collects_one_entry_per_call_not_deduplicated() -> None:
    """Issues are collected as a list, not deduplicated/overwritten — the same code reported
    multiple times (e.g. once per iteration of a `repeat` block) accumulates one entry per
    call (INDIMCP-73). Exercises `_report_issue` directly rather than through a real step,
    since no current non-fatal step condition is repeatable across iterations without
    resolving itself after the first (e.g. `select_filter`'s own drift check either fixes
    itself — INDIMCP-64's copy-from-driver case — or aborts the run outright on a genuine
    mismatch)."""
    ctx = script_engine._ExecutionContext(
        rig_id="test-rig",
        role_to_device={},
        cancel_event=None,
        pause_event=None,
        on_progress=None,
        total_steps=None,
        scripts={},
        run_id=None,
    )

    for _ in range(3):
        script_engine._report_issue(
            ctx,
            script_engine.Severity.WARNING,
            "filterConfigMismatch",
            "role 'filterWheel's rig config doesn't match the driver",
            role="filterWheel",
        )

    assert len(ctx.warnings) == 3
    assert all(w["code"] == "filterConfigMismatch" for w in ctx.warnings)


async def test_execute_script_run_script_forwards_a_nested_issue_to_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-fatal issue reported inside a `run_script` callee appears in the top-level
    caller's own `ScriptResult.warnings` — free forwarding via the shared `_ExecutionContext`
    (INDIMCP-73), no separate propagation code needed. Uses INDIMCP-64's copy-from-driver case
    as the non-fatal issue: the callee's rig component has no filter slots configured, so
    `select_filter` adopts the driver's own live `FILTER_NAME` and reports an `INFO` issue
    rather than failing."""
    _rig(rig_store.Component(role="filterWheel", id="fw-1", device="Filter Wheel Simulator"))
    _script(
        "callee",
        steps=[{"step": "select_filter", "role": "filterWheel", "filterName": "Red"}],
    )
    _script(
        "caller",
        steps=[{"step": "run_script", "script": "callee"}],
    )
    monkeypatch.setattr(indi_messaging, "send_property", AsyncMock())
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Ok")
    monkeypatch.setattr(
        indi_messaging,
        "get_property_values",
        lambda device, name: (
            {"FILTER_SLOT_NAME_1": "Luminance", "FILTER_SLOT_NAME_2": "Red"}
            if name == "FILTER_NAME"
            else _default_get_property_values(device, name)
        ),
    )
    monkeypatch.setattr(rig_store, "update_component_slots", MagicMock())

    result = await script_engine.execute_script("caller", "test-rig", {})

    assert len(result["warnings"]) == 1
    assert result["warnings"][0]["code"] == "filterSlotsCopiedFromDriver"


async def test_execute_script_fatal_failure_still_carries_issues_collected_earlier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `FATAL`-severity issue (or any other exception `execute_script` raises) still carries
    whatever non-fatal issues were collected earlier in the same run — nothing is lost just
    because the run ultimately aborts (INDIMCP-73). Step 1's rig component has no filter slots
    configured, so it adopts the driver's `FILTER_NAME` (an `INFO` issue, INDIMCP-64) before
    step 2 fails outright resolving an unknown filter name."""
    _rig(rig_store.Component(role="filterWheel", id="fw-1", device="Filter Wheel Simulator"))
    _script(
        "select_filter-then-fail",
        steps=[
            {"step": "select_filter", "role": "filterWheel", "filterName": "Red"},
            {"step": "select_filter", "role": "filterWheel", "filterName": "Nonexistent"},
        ],
    )
    monkeypatch.setattr(indi_messaging, "send_property", AsyncMock())
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Ok")
    monkeypatch.setattr(
        indi_messaging,
        "get_property_values",
        lambda device, name: (
            {"FILTER_SLOT_NAME_1": "Luminance", "FILTER_SLOT_NAME_2": "Red"}
            if name == "FILTER_NAME"
            else _default_get_property_values(device, name)
        ),
    )
    monkeypatch.setattr(rig_store, "update_component_slots", MagicMock())

    with pytest.raises(
        script_engine.ScriptExecutionError, match="no slot named 'Nonexistent'"
    ) as exc_info:
        await script_engine.execute_script("select_filter-then-fail", "test-rig", {})

    assert len(exc_info.value.warnings) == 1
    assert exc_info.value.warnings[0]["code"] == "filterSlotsCopiedFromDriver"


async def test_report_issue_fatal_raises_and_includes_itself_in_the_exceptions_warnings() -> None:
    """`_report_issue`'s `FATAL` branch must raise, and the fatal issue itself must survive
    onto the raised exception's `warnings` — not just its plain string `message` — since
    `execute_script`'s except handler copies `ctx.warnings` onto the exception wholesale
    (INDIMCP-73). Exercises `_report_issue` directly rather than through a real step — see
    `test_execute_script_select_filter_fails_fatally_when_driver_disagrees_with_rig` for the
    same mechanism exercised through `select_filter`'s own `FATAL` case (INDIMCP-64)."""
    ctx = script_engine._ExecutionContext(
        rig_id="test-rig",
        role_to_device={},
        cancel_event=None,
        pause_event=None,
        on_progress=None,
        total_steps=None,
        scripts={},
        run_id=None,
    )

    with pytest.raises(script_engine.ScriptExecutionError, match="mount is on fire") as exc_info:
        script_engine._report_issue(
            ctx, script_engine.Severity.FATAL, "mountOnFire", "mount is on fire", role="mount"
        )

    assert ctx.warnings == [
        {
            "kind": "issue",
            "severity": script_engine.Severity.FATAL,
            "code": "mountOnFire",
            "message": "mount is on fire",
            "role": "mount",
            "device": None,
        }
    ]
    assert exc_info.value.warnings == ctx.warnings


async def test_execute_script_select_filter_times_out_waiting_for_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="filterWheel", id="fw-1", device="Filter Wheel Simulator"))
    _script(
        "select_filter",
        steps=[
            {
                "step": "select_filter",
                "role": "filterWheel",
                "slot": 2,
                "timeoutSeconds": 0.01,
            }
        ],
    )
    monkeypatch.setattr(indi_messaging, "send_property", AsyncMock())
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Busy")
    monkeypatch.setattr(script_engine, "_WAIT_POLL_INTERVAL_SECONDS", 0.001)

    with pytest.raises(script_engine.ScriptExecutionError, match="did not reach"):
        await script_engine.execute_script("select_filter", "test-rig", {})


async def test_execute_script_select_filter_fails_fast_on_filter_slot_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A driver-reported `Alert` (e.g. a filter wheel jam) shouldn't be waited out for the full
    timeout — it's not something more polling will resolve.
    """
    _rig(rig_store.Component(role="filterWheel", id="fw-1", device="Filter Wheel Simulator"))
    _script(
        "select_filter",
        steps=[{"step": "select_filter", "role": "filterWheel", "slot": 2}],
    )
    monkeypatch.setattr(indi_messaging, "send_property", AsyncMock())
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Alert")
    monkeypatch.setattr(script_engine, "_WAIT_POLL_INTERVAL_SECONDS", 0.001)

    with pytest.raises(script_engine.ScriptExecutionError, match="went to Alert"):
        await asyncio.wait_for(
            script_engine.execute_script("select_filter", "test-rig", {}), timeout=1.0
        )


async def test_sync_filter_names_returns_matched_and_pushes_nothing_when_already_equal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)
    monkeypatch.setattr(
        indi_messaging,
        "get_property_values",
        lambda device, name: {"FILTER_SLOT_NAME_1": "Luminance", "FILTER_SLOT_NAME_2": "Red"},
    )

    outcome = await script_engine.sync_filter_names(
        "filterWheel", "Filter Wheel Simulator", {1: "Luminance", 2: "Red"}
    )

    assert outcome == {
        "status": "matched",
        "rigSlots": {1: "Luminance", 2: "Red"},
        "liveSlots": {1: "Luminance", 2: "Red"},
    }
    send_property.assert_not_awaited()


async def test_sync_filter_names_pushes_and_returns_synced_when_they_disagree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)
    monkeypatch.setattr(
        indi_messaging,
        "get_property_values",
        lambda device, name: {"FILTER_SLOT_NAME_1": "Luminance", "FILTER_SLOT_NAME_2": "Green"},
    )

    outcome = await script_engine.sync_filter_names(
        "filterWheel", "Filter Wheel Simulator", {1: "Luminance", 2: "Red"}
    )

    send_property.assert_awaited_once_with(
        "Filter Wheel Simulator",
        "FILTER_NAME",
        {"FILTER_SLOT_NAME_1": "Luminance", "FILTER_SLOT_NAME_2": "Red"},
    )
    assert outcome["status"] == "synced"


async def test_sync_filter_names_raises_when_rig_has_no_slots() -> None:
    with pytest.raises(ValueError, match="no filter slots"):
        await script_engine.sync_filter_names("filterWheel", "Filter Wheel Simulator", {})


async def test_sync_filter_names_raises_when_driver_lacks_filter_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(indi_messaging, "get_property_values", lambda device, name: None)

    with pytest.raises(ValueError, match="does not expose FILTER_NAME"):
        await script_engine.sync_filter_names(
            "filterWheel", "Filter Wheel Simulator", {1: "Luminance"}
        )


async def test_sync_filter_names_raises_when_slot_counts_differ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        indi_messaging,
        "get_property_values",
        lambda device, name: {"FILTER_SLOT_NAME_1": "Luminance"},
    )

    with pytest.raises(ValueError, match="differently-sized wheel"):
        await script_engine.sync_filter_names(
            "filterWheel", "Filter Wheel Simulator", {1: "Luminance", 2: "Red"}
        )


async def test_sync_filter_names_raises_when_the_push_itself_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        indi_messaging,
        "get_property_values",
        lambda device, name: {"FILTER_SLOT_NAME_1": "Luminance", "FILTER_SLOT_NAME_2": "Green"},
    )
    monkeypatch.setattr(
        indi_messaging, "send_property", AsyncMock(side_effect=RuntimeError("driver rejected it"))
    )

    with pytest.raises(ValueError, match="pushing filter names to driver .* failed"):
        await script_engine.sync_filter_names(
            "filterWheel", "Filter Wheel Simulator", {1: "Luminance", 2: "Red"}
        )


async def test_execute_script_sync_filter_names_reports_info_issue_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(
        rig_store.Component(
            role="filterWheel",
            id="fw-1",
            device="Filter Wheel Simulator",
            slots={1: "Luminance", 2: "Red"},
        )
    )
    _script(
        "sync_filter_names",
        steps=[{"step": "sync_filter_names", "role": "filterWheel"}],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)
    monkeypatch.setattr(
        indi_messaging,
        "get_property_values",
        lambda device, name: (
            {"FILTER_SLOT_NAME_1": "Luminance", "FILTER_SLOT_NAME_2": "Green"}
            if name == "FILTER_NAME"
            else _default_get_property_values(device, name)
        ),
    )

    result = await script_engine.execute_script("sync_filter_names", "test-rig", {})

    send_property.assert_awaited_once_with(
        "Filter Wheel Simulator",
        "FILTER_NAME",
        {"FILTER_SLOT_NAME_1": "Luminance", "FILTER_SLOT_NAME_2": "Red"},
    )
    assert len(result["warnings"]) == 1
    issue = result["warnings"][0]
    assert issue["kind"] == "issue"
    assert issue["severity"] == script_engine.Severity.INFO
    assert issue["code"] == "filterConfigSynced"
    assert issue["role"] == "filterWheel"
    assert issue["device"] == "Filter Wheel Simulator"


async def test_execute_script_sync_filter_names_reports_no_issue_when_already_matched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(
        rig_store.Component(
            role="filterWheel",
            id="fw-1",
            device="Filter Wheel Simulator",
            slots={1: "Luminance", 2: "Red"},
        )
    )
    _script(
        "sync_filter_names",
        steps=[{"step": "sync_filter_names", "role": "filterWheel"}],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)
    monkeypatch.setattr(
        indi_messaging,
        "get_property_values",
        lambda device, name: (
            {"FILTER_SLOT_NAME_1": "Luminance", "FILTER_SLOT_NAME_2": "Red"}
            if name == "FILTER_NAME"
            else _default_get_property_values(device, name)
        ),
    )

    result = await script_engine.execute_script("sync_filter_names", "test-rig", {})

    send_property.assert_not_awaited()
    assert result["warnings"] == []


async def test_execute_script_sync_filter_names_fails_when_slot_counts_differ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(
        rig_store.Component(
            role="filterWheel",
            id="fw-1",
            device="Filter Wheel Simulator",
            slots={1: "Luminance", 2: "Red"},
        )
    )
    _script(
        "sync_filter_names",
        steps=[{"step": "sync_filter_names", "role": "filterWheel"}],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)
    monkeypatch.setattr(
        indi_messaging,
        "get_property_values",
        lambda device, name: (
            {"FILTER_SLOT_NAME_1": "Luminance"}
            if name == "FILTER_NAME"
            else _default_get_property_values(device, name)
        ),
    )

    with pytest.raises(script_engine.ScriptExecutionError, match="differently-sized wheel"):
        await script_engine.execute_script("sync_filter_names", "test-rig", {})

    send_property.assert_not_awaited()


async def test_adopt_filter_names_from_driver_returns_matched_when_already_equal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        indi_messaging,
        "get_property_values",
        lambda device, name: {"FILTER_SLOT_NAME_1": "Luminance", "FILTER_SLOT_NAME_2": "Red"},
    )
    update_component_slots = MagicMock()
    monkeypatch.setattr(rig_store, "update_component_slots", update_component_slots)

    outcome = await script_engine.adopt_filter_names_from_driver(
        "test-rig", "filterWheel", "Filter Wheel Simulator", {1: "Luminance", 2: "Red"}
    )

    assert outcome == {
        "status": "matched",
        "rigSlots": {1: "Luminance", 2: "Red"},
        "liveSlots": {1: "Luminance", 2: "Red"},
    }
    update_component_slots.assert_not_called()


async def test_adopt_filter_names_from_driver_persists_and_returns_adopted_when_they_disagree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        indi_messaging,
        "get_property_values",
        lambda device, name: {"FILTER_SLOT_NAME_1": "Luminance", "FILTER_SLOT_NAME_2": "Green"},
    )
    update_component_slots = MagicMock()
    monkeypatch.setattr(rig_store, "update_component_slots", update_component_slots)

    outcome = await script_engine.adopt_filter_names_from_driver(
        "test-rig", "filterWheel", "Filter Wheel Simulator", {1: "Luminance", 2: "Red"}
    )

    update_component_slots.assert_called_once_with(
        "test-rig", "filterWheel", {1: "Luminance", 2: "Green"}
    )
    assert outcome == {
        "status": "adopted",
        "rigSlots": {1: "Luminance", 2: "Green"},
        "liveSlots": {1: "Luminance", 2: "Green"},
    }


async def test_adopt_filter_names_from_driver_adopts_even_when_slot_counts_differ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unlike `sync_filter_names` (which refuses to push a rig's config for what might be a
    differently-sized wheel), `adopt_filter_names_from_driver` has no slot-count guard: the
    driver is always authoritative about its own hardware when adopting *from* it, so there's
    no "wrong-sized wheel" risk the way there is when pushing a possibly-wrong rig config onto
    real hardware."""
    monkeypatch.setattr(
        indi_messaging,
        "get_property_values",
        lambda device, name: {"FILTER_SLOT_NAME_1": "Luminance"},
    )
    update_component_slots = MagicMock()
    monkeypatch.setattr(rig_store, "update_component_slots", update_component_slots)

    outcome = await script_engine.adopt_filter_names_from_driver(
        "test-rig", "filterWheel", "Filter Wheel Simulator", {1: "Luminance", 2: "Red"}
    )

    update_component_slots.assert_called_once_with("test-rig", "filterWheel", {1: "Luminance"})
    assert outcome == {
        "status": "adopted",
        "rigSlots": {1: "Luminance"},
        "liveSlots": {1: "Luminance"},
    }


async def test_adopt_filter_names_from_driver_raises_when_driver_lacks_filter_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(indi_messaging, "get_property_values", lambda device, name: None)

    with pytest.raises(ValueError, match="does not expose FILTER_NAME"):
        await script_engine.adopt_filter_names_from_driver(
            "test-rig", "filterWheel", "Filter Wheel Simulator", {1: "Luminance"}
        )


async def test_adopt_filter_names_from_driver_raises_when_driver_has_no_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(indi_messaging, "get_property_values", lambda device, name: {})

    with pytest.raises(ValueError, match="declares no filter slots"):
        await script_engine.adopt_filter_names_from_driver(
            "test-rig", "filterWheel", "Filter Wheel Simulator", {1: "Luminance"}
        )


async def test_adopt_filter_names_from_driver_raises_when_persisting_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        indi_messaging,
        "get_property_values",
        lambda device, name: {"FILTER_SLOT_NAME_1": "Luminance", "FILTER_SLOT_NAME_2": "Green"},
    )
    monkeypatch.setattr(
        rig_store, "update_component_slots", MagicMock(side_effect=OSError("disk full"))
    )

    with pytest.raises(ValueError, match="persisting filter names copied from driver"):
        await script_engine.adopt_filter_names_from_driver(
            "test-rig", "filterWheel", "Filter Wheel Simulator", {1: "Luminance", 2: "Red"}
        )


async def test_execute_script_adopt_filter_names_from_driver_reports_info_issue_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(
        rig_store.Component(
            role="filterWheel",
            id="fw-1",
            device="Filter Wheel Simulator",
            slots={1: "Luminance", 2: "Red"},
        )
    )
    _script(
        "adopt_filter_names_from_driver",
        steps=[{"step": "adopt_filter_names_from_driver", "role": "filterWheel"}],
    )
    monkeypatch.setattr(
        indi_messaging,
        "get_property_values",
        lambda device, name: (
            {"FILTER_SLOT_NAME_1": "Luminance", "FILTER_SLOT_NAME_2": "Green"}
            if name == "FILTER_NAME"
            else _default_get_property_values(device, name)
        ),
    )
    update_component_slots = MagicMock()
    monkeypatch.setattr(rig_store, "update_component_slots", update_component_slots)

    result = await script_engine.execute_script("adopt_filter_names_from_driver", "test-rig", {})

    update_component_slots.assert_called_once_with(
        "test-rig", "filterWheel", {1: "Luminance", 2: "Green"}
    )
    assert len(result["warnings"]) == 1
    issue = result["warnings"][0]
    assert issue["kind"] == "issue"
    assert issue["severity"] == script_engine.Severity.INFO
    assert issue["code"] == "filterConfigAdopted"
    assert issue["role"] == "filterWheel"
    assert issue["device"] == "Filter Wheel Simulator"


async def test_execute_script_adopt_filter_names_from_driver_reports_no_issue_when_already_matched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(
        rig_store.Component(
            role="filterWheel",
            id="fw-1",
            device="Filter Wheel Simulator",
            slots={1: "Luminance", 2: "Red"},
        )
    )
    _script(
        "adopt_filter_names_from_driver",
        steps=[{"step": "adopt_filter_names_from_driver", "role": "filterWheel"}],
    )
    monkeypatch.setattr(
        indi_messaging,
        "get_property_values",
        lambda device, name: (
            {"FILTER_SLOT_NAME_1": "Luminance", "FILTER_SLOT_NAME_2": "Red"}
            if name == "FILTER_NAME"
            else _default_get_property_values(device, name)
        ),
    )
    update_component_slots = MagicMock()
    monkeypatch.setattr(rig_store, "update_component_slots", update_component_slots)

    result = await script_engine.execute_script("adopt_filter_names_from_driver", "test-rig", {})

    update_component_slots.assert_not_called()
    assert result["warnings"] == []


async def test_execute_script_adopt_filter_names_from_driver_fails_when_driver_has_no_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(
        rig_store.Component(
            role="filterWheel",
            id="fw-1",
            device="Filter Wheel Simulator",
            slots={1: "Luminance", 2: "Red"},
        )
    )
    _script(
        "adopt_filter_names_from_driver",
        steps=[{"step": "adopt_filter_names_from_driver", "role": "filterWheel"}],
    )
    monkeypatch.setattr(
        indi_messaging,
        "get_property_values",
        lambda device, name: (
            {} if name == "FILTER_NAME" else _default_get_property_values(device, name)
        ),
    )
    update_component_slots = MagicMock()
    monkeypatch.setattr(rig_store, "update_component_slots", update_component_slots)

    with pytest.raises(script_engine.ScriptExecutionError, match="declares no filter slots"):
        await script_engine.execute_script("adopt_filter_names_from_driver", "test-rig", {})

    update_component_slots.assert_not_called()


async def test_execute_script_set_focus_position_sets_position_and_waits_for_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(
        rig_store.Component(
            role="focuser",
            id="focuser-1",
            device="Focuser Simulator",
            minPosition=0,
            maxPosition=15370,
        )
    )
    _script(
        "focus",
        steps=[{"step": "set_focus_position", "role": "focuser", "position": 7000}],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)
    states = iter(["Busy", "Ok"])
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: next(states))
    monkeypatch.setattr(script_engine, "_WAIT_POLL_INTERVAL_SECONDS", 0.001)

    result = await script_engine.execute_script("focus", "test-rig", {})

    send_property.assert_awaited_once_with(
        "Focuser Simulator", "ABS_FOCUS_POSITION", {"FOCUS_ABSOLUTE_POSITION": "7000"}
    )
    assert result["stepsExecuted"] == 1


async def test_execute_script_set_focus_position_substitutes_parameter_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="focuser", id="focuser-1", device="Focuser Simulator"))
    _script(
        "focus",
        parameters={"position": {"type": "integer", "required": True}},
        steps=[{"step": "set_focus_position", "role": "focuser", "position": "{{ position }}"}],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Ok")

    await script_engine.execute_script("focus", "test-rig", {"position": 5000})

    send_property.assert_awaited_once_with(
        "Focuser Simulator", "ABS_FOCUS_POSITION", {"FOCUS_ABSOLUTE_POSITION": "5000"}
    )


async def test_execute_script_set_focus_position_raises_when_outside_declared_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(
        rig_store.Component(
            role="focuser",
            id="focuser-1",
            device="Focuser Simulator",
            minPosition=0,
            maxPosition=15370,
        )
    )
    _script(
        "focus",
        steps=[{"step": "set_focus_position", "role": "focuser", "position": 20000}],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)

    with pytest.raises(script_engine.ScriptExecutionError, match=r"outside its declared range"):
        await script_engine.execute_script("focus", "test-rig", {})

    send_property.assert_not_awaited()


async def test_execute_script_set_focus_position_skips_range_check_when_rig_declares_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A focuser component with no minPosition/maxPosition at all (unset/unknown) has nothing
    to check against, not a crash."""
    _rig(rig_store.Component(role="focuser", id="focuser-1", device="Focuser Simulator"))
    _script(
        "focus",
        steps=[{"step": "set_focus_position", "role": "focuser", "position": 999999}],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Ok")

    await script_engine.execute_script("focus", "test-rig", {})

    send_property.assert_awaited_once_with(
        "Focuser Simulator", "ABS_FOCUS_POSITION", {"FOCUS_ABSOLUTE_POSITION": "999999"}
    )


async def test_execute_script_set_focus_position_skips_range_check_when_rig_declares_only_one_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A focuser component declaring only minPosition (or only maxPosition) has no *complete*
    range to check against — same as declaring neither, not a crash on a None bound."""
    _rig(
        rig_store.Component(
            role="focuser", id="focuser-1", device="Focuser Simulator", minPosition=0
        )
    )
    _script(
        "focus",
        steps=[{"step": "set_focus_position", "role": "focuser", "position": 999999}],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Ok")

    await script_engine.execute_script("focus", "test-rig", {})

    send_property.assert_awaited_once_with(
        "Focuser Simulator", "ABS_FOCUS_POSITION", {"FOCUS_ABSOLUTE_POSITION": "999999"}
    )


async def test_execute_script_set_focus_position_times_out_waiting_for_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="focuser", id="focuser-1", device="Focuser Simulator"))
    _script(
        "focus",
        steps=[
            {
                "step": "set_focus_position",
                "role": "focuser",
                "position": 7000,
                "timeoutSeconds": 0.01,
            }
        ],
    )
    monkeypatch.setattr(indi_messaging, "send_property", AsyncMock())
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Busy")
    monkeypatch.setattr(script_engine, "_WAIT_POLL_INTERVAL_SECONDS", 0.001)

    with pytest.raises(script_engine.ScriptExecutionError, match="did not reach"):
        await script_engine.execute_script("focus", "test-rig", {})


async def test_execute_script_set_focus_position_fails_fast_on_focus_position_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A driver-reported `Alert` (e.g. a focuser stall) shouldn't be waited out for the full
    timeout — it's not something more polling will resolve.
    """
    _rig(rig_store.Component(role="focuser", id="focuser-1", device="Focuser Simulator"))
    _script(
        "focus",
        steps=[{"step": "set_focus_position", "role": "focuser", "position": 7000}],
    )
    monkeypatch.setattr(indi_messaging, "send_property", AsyncMock())
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Alert")
    monkeypatch.setattr(script_engine, "_WAIT_POLL_INTERVAL_SECONDS", 0.001)

    with pytest.raises(script_engine.ScriptExecutionError, match="went to Alert"):
        await asyncio.wait_for(script_engine.execute_script("focus", "test-rig", {}), timeout=1.0)


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


async def test_execute_script_progress_reports_role_and_device_for_a_step_with_a_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script("single", steps=[_set_property("camera", "CCD_EXPOSURE", {"X": "1"})])
    monkeypatch.setattr(indi_messaging, "send_property", AsyncMock())
    progress: list[script_engine.ScriptProgress] = []

    await script_engine.execute_script("single", "test-rig", {}, on_progress=progress.append)

    assert progress[0]["role"] == "camera"
    assert progress[0]["device"] == "CCD Simulator"


async def test_execute_script_progress_role_and_device_are_none_for_a_roleless_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`repeat` (fixed `count`, no `until`) has no role of its own — distinct from the
    steps nested inside it, which each report their own role/device normally."""
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "repeat-count",
        steps=[
            {
                "step": "repeat",
                "count": 2,
                "steps": [_set_property("camera", "CCD_EXPOSURE", {"X": "1"})],
            }
        ],
    )
    monkeypatch.setattr(indi_messaging, "send_property", AsyncMock())
    progress: list[script_engine.ScriptProgress] = []

    await script_engine.execute_script("repeat-count", "test-rig", {}, on_progress=progress.append)

    assert progress[0]["role"] is None
    assert progress[0]["device"] is None
    assert progress[1]["role"] == "camera"
    assert progress[1]["device"] == "CCD Simulator"


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


async def test_execute_script_total_steps_counts_identical_run_script_calls_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling the same script with the same arguments twice must count its steps twice —
    unlike `_collect_role_usage`'s `_visited` set (a correct no-op on a role-usage revisit),
    a step count has no such dedup: each call site genuinely executes its own steps. This
    guards the `ResolvedCallArgsCache` shared between `_collect_role_usage` and
    `_count_total_steps` — it must only memoize the deterministic argument resolution, never
    skip re-counting a call site "already seen" elsewhere."""
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "callee",
        steps=[_set_property("camera", "CCD_EXPOSURE", {"X": "1"})],
    )
    _script(
        "caller",
        steps=[
            {"step": "run_script", "script": "callee"},
            {"step": "run_script", "script": "callee"},
        ],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)
    progress: list[script_engine.ScriptProgress] = []

    await script_engine.execute_script("caller", "test-rig", {}, on_progress=progress.append)

    # 2x (run_script step) + 2x (the callee's own set_property step) = 4
    assert progress[0]["totalSteps"] == 4
    assert send_property.await_count == 2


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


async def test_execute_script_capture_frame_aborts_exposure_when_cancelled_mid_exposure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cancel_script's own cancellation must also stop the camera physically exposing
    (INDIMCP-86), not just the MCP-side script/polling — see _abort_exposure_on_cancel."""
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "capture",
        steps=[
            {"step": "capture_frame", "role": "camera", "exposureSeconds": 30, "frameType": "Light"}
        ],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Busy")
    monkeypatch.setattr(script_engine, "_WAIT_POLL_INTERVAL_SECONDS", 0.001)
    cancel_event = asyncio.Event()

    async def cancel_soon() -> None:
        await asyncio.sleep(0.01)
        cancel_event.set()

    canceller = asyncio.create_task(cancel_soon())
    with pytest.raises(script_engine.ScriptCancelled):
        await script_engine.execute_script("capture", "test-rig", {}, cancel_event=cancel_event)
    await canceller

    assert (
        call("CCD Simulator", "CCD_ABORT_EXPOSURE", {"ABORT": "On"})
        in send_property.await_args_list
    )


async def test_execute_script_capture_frame_cancelled_after_exposure_completes_does_not_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """By the time CCD_EXPOSURE reaches Ok, the camera has already finished physically
    exposing -- a cancellation during the later BLOB wait has nothing left to abort."""
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "capture",
        steps=[
            {"step": "capture_frame", "role": "camera", "exposureSeconds": 30, "frameType": "Light"}
        ],
    )
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Ok")
    monkeypatch.setattr(indi_messaging, "get_latest_blob", lambda device, name: None)
    monkeypatch.setattr(script_engine, "_WAIT_POLL_INTERVAL_SECONDS", 0.001)
    cancel_event = asyncio.Event()

    async def cancel_soon() -> None:
        await asyncio.sleep(0.01)
        cancel_event.set()

    canceller = asyncio.create_task(cancel_soon())
    with pytest.raises(script_engine.ScriptCancelled):
        await script_engine.execute_script("capture", "test-rig", {}, cancel_event=cancel_event)
    await canceller

    assert call("CCD Simulator", "CCD_ABORT_EXPOSURE", {"ABORT": "On"}) not in (
        send_property.await_args_list
    )


async def test_execute_script_capture_frame_cancel_propagates_even_if_abort_send_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A camera whose driver doesn't define CCD_ABORT_EXPOSURE (send_property raises
    ValueError, e.g. an unknown property) must not turn a clean cancellation into
    something else -- ScriptCancelled still propagates, not the swallowed error."""
    _rig(rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"))
    _script(
        "capture",
        steps=[
            {"step": "capture_frame", "role": "camera", "exposureSeconds": 30, "frameType": "Light"}
        ],
    )

    async def failing_send_property(device: str, name: str, elements: dict[str, str]) -> None:
        if name == "CCD_ABORT_EXPOSURE":
            raise ValueError(f"Unknown property {name!r} on device {device!r}")

    monkeypatch.setattr(indi_messaging, "send_property", failing_send_property)
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Busy")
    monkeypatch.setattr(script_engine, "_WAIT_POLL_INTERVAL_SECONDS", 0.001)
    cancel_event = asyncio.Event()

    async def cancel_soon() -> None:
        await asyncio.sleep(0.01)
        cancel_event.set()

    canceller = asyncio.create_task(cancel_soon())
    with pytest.raises(script_engine.ScriptCancelled):
        await script_engine.execute_script("capture", "test-rig", {}, cancel_event=cancel_event)
    await canceller


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
        script_store.CoolCameraStep,
        script_store.SelectFilterStep,
        script_store.SyncFilterNamesStep,
        script_store.AdoptFilterNamesFromDriverStep,
        script_store.SetFocusPositionStep,
        script_store.PlateSolveStep,
        script_store.RunScriptStep,
        script_store.RepeatStep,
        script_store.IfStep,
    }

    assert set(script_engine.STEP_HANDLERS) == step_types


async def test_run_one_step_rejects_a_step_type_with_no_registered_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = script_engine._ExecutionContext(
        rig_id="test-rig",
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


def _plate_solve_rig(*extra_components: rig_store.Component, **camera_fields: Any) -> None:
    _rig(
        rig_store.Component(role="camera", id="cam-1", device="CCD Simulator", **camera_fields),
        rig_store.Component(role="mount", id="mount-1", device="Mount Simulator"),
        *extra_components,
    )


_PLATE_SOLVE_RESULT_UNSET = object()


def _plate_solve_step(**fields: Any) -> dict:
    return {"step": "plate_solve", "role": "camera", "mountRole": "mount", **fields}


def _mock_plate_solve(
    monkeypatch: pytest.MonkeyPatch,
    *,
    frames: list[dict[str, Any]] | None = None,
    frame_path: Any = None,
    result: Any = _PLATE_SOLVE_RESULT_UNSET,
) -> tuple[AsyncMock, MagicMock, MagicMock, MagicMock, AsyncMock]:
    """Wire up the mocks a `plate_solve` step needs, without an internal fresh capture.

    Returns `(send_property, list_frames, get_frame_path, update_frame_data, solve)`.
    """
    send_property = AsyncMock()
    monkeypatch.setattr(indi_messaging, "send_property", send_property)
    monkeypatch.setattr(indi_messaging, "get_property_state", lambda device, name: "Ok")
    list_frames = MagicMock(
        return_value=frames
        if frames is not None
        else [
            {
                "frameId": "frame-1",
                "runId": None,
                "device": "CCD Simulator",
                "sizeBytes": 10,
                "capturedAt": "2026-07-20T00:00:00.000000+00:00",
                "transferredAt": None,
            }
        ]
    )
    monkeypatch.setattr(frame_store, "list_frames", list_frames)
    get_frame_path = MagicMock(return_value=frame_path or MagicMock())
    monkeypatch.setattr(frame_store, "get_frame_path", get_frame_path)
    update_frame_data = MagicMock()
    monkeypatch.setattr(frame_store, "update_frame_data", update_frame_data)
    solve = AsyncMock(
        return_value=result
        if result is not _PLATE_SOLVE_RESULT_UNSET
        else script_engine.plate_solver.PlateSolveResult(
            raDegJ2000=150.0,
            decDegJ2000=20.0,
            crpix1=512.0,
            crpix2=512.0,
            ctype1="RA---TAN",
            ctype2="DEC--TAN",
            cd1_1=-0.0002,
            cd1_2=0.0,
            cd2_1=0.0,
            cd2_2=0.0002,
        )
    )
    monkeypatch.setattr(script_engine.plate_solver, "solve", solve)
    return send_property, list_frames, get_frame_path, update_frame_data, solve


async def test_execute_script_plate_solve_solves_the_most_recently_captured_frame(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _plate_solve_rig()
    _script("solve", steps=[_plate_solve_step()])
    frame_path = tmp_path / "frame-1.fits"
    frame_path.write_bytes(b"fits-bytes")
    send_property, list_frames, get_frame_path, update_frame_data, solve = _mock_plate_solve(
        monkeypatch, frame_path=frame_path
    )
    write_headers = MagicMock(return_value=b"updated-fits-bytes")
    monkeypatch.setattr(fits_headers, "write_fits_headers", write_headers)

    result = await script_engine.execute_script("solve", "test-rig", {})

    list_frames.assert_called_once_with(run_id=None, device="CCD Simulator")
    get_frame_path.assert_called_once_with("frame-1")
    solve.assert_awaited_once()
    call_kwargs = solve.call_args.kwargs
    assert call_kwargs["timeout_seconds"] == 60.0
    write_headers.assert_called_once()
    written_data, written_fields = write_headers.call_args.args
    assert written_data == b"fits-bytes"
    assert written_fields["CRVAL1"] == (150.0, "[deg] WCS reference point RA (J2000)")
    assert written_fields["CDELT1"][0] == pytest.approx(-0.0002)
    update_frame_data.assert_called_once_with("frame-1", b"updated-fits-bytes")
    assert result["stepsExecuted"] == 1


async def test_execute_script_plate_solve_survives_a_db_error_writing_wcs_headers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A solve that succeeds but hits a sqlite3 error persisting the WCS header (e.g. a
    locked/corrupt db file — not an OSError subclass) must not fail the whole step: this is
    documented best-effort enrichment, same as every other FITS header write in this module."""
    _plate_solve_rig()
    _script("solve", steps=[_plate_solve_step()])
    frame_path = tmp_path / "frame-1.fits"
    frame_path.write_bytes(b"fits-bytes")
    _, _, _, update_frame_data, _ = _mock_plate_solve(monkeypatch, frame_path=frame_path)
    update_frame_data.side_effect = sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(
        fits_headers, "write_fits_headers", MagicMock(return_value=b"updated-fits-bytes")
    )

    result = await script_engine.execute_script("solve", "test-rig", {})

    assert result["stepsExecuted"] == 1


async def test_execute_script_plate_solve_syncs_the_mount_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _plate_solve_rig()
    _script("solve", steps=[_plate_solve_step()])
    frame_path = tmp_path / "frame-1.fits"
    frame_path.write_bytes(b"fits-bytes")
    send_property, *_ = _mock_plate_solve(monkeypatch, frame_path=frame_path)
    monkeypatch.setattr(fits_headers, "write_fits_headers", MagicMock(return_value=None))

    await script_engine.execute_script("solve", "test-rig", {})

    send_property.assert_any_await("Mount Simulator", "ON_COORD_SET", {"SYNC": "On"})
    send_property.assert_any_await(
        "Mount Simulator", "EQUATORIAL_EOD_COORD", {"RA": str(150.0 / 15.0), "DEC": str(20.0)}
    )


async def test_execute_script_plate_solve_restores_on_coord_set_to_track_after_sync(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Leaving ON_COORD_SET at SYNC would silently turn a later EQUATORIAL_EOD_COORD command
    (e.g. a bare set_property step) into another no-motion sync instead of an actual move —
    the same hazard _ensure_track_on_slew exists to prevent for slew."""
    _plate_solve_rig()
    _script("solve", steps=[_plate_solve_step()])
    frame_path = tmp_path / "frame-1.fits"
    frame_path.write_bytes(b"fits-bytes")
    send_property, *_ = _mock_plate_solve(monkeypatch, frame_path=frame_path)
    monkeypatch.setattr(fits_headers, "write_fits_headers", MagicMock(return_value=None))

    await script_engine.execute_script("solve", "test-rig", {})

    on_coord_set_calls = [
        call for call in send_property.await_args_list if call.args[1] == "ON_COORD_SET"
    ]
    assert [call.args[2] for call in on_coord_set_calls] == [{"SYNC": "On"}, {"TRACK": "On"}]


async def test_execute_script_plate_solve_skips_mount_sync_when_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _plate_solve_rig()
    _script("solve", steps=[_plate_solve_step(syncMount=False)])
    frame_path = tmp_path / "frame-1.fits"
    frame_path.write_bytes(b"fits-bytes")
    send_property, *_ = _mock_plate_solve(monkeypatch, frame_path=frame_path)
    monkeypatch.setattr(fits_headers, "write_fits_headers", MagicMock(return_value=None))

    await script_engine.execute_script("solve", "test-rig", {})

    send_property.assert_not_awaited()


async def test_execute_script_plate_solve_uses_position_and_scale_hints(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _plate_solve_rig(
        rig_store.Component(role="telescope", id="scope-1", focalLengthMm=550),
        pixelSizeMicron=3.76,
    )
    _script("solve", steps=[_plate_solve_step()])
    frame_path = tmp_path / "frame-1.fits"
    frame_path.write_bytes(b"fits-bytes")

    def get_property_values(device: str, name: str) -> dict[str, str] | None:
        if name == "CONNECTION":
            return {"CONNECT": "On", "DISCONNECT": "Off"}
        if device == "Mount Simulator" and name == "EQUATORIAL_EOD_COORD":
            return {"RA": "10.0", "DEC": "20.0"}
        return None

    monkeypatch.setattr(indi_messaging, "get_property_values", get_property_values)
    _, _, _, _, solve = _mock_plate_solve(monkeypatch, frame_path=frame_path)
    monkeypatch.setattr(fits_headers, "write_fits_headers", MagicMock(return_value=None))

    await script_engine.execute_script("solve", "test-rig", {})

    call_kwargs = solve.call_args.kwargs
    assert call_kwargs["ra_hint_hours"] == 10.0
    assert call_kwargs["dec_hint_deg"] == 20.0
    plate_scale = 206.265 * 3.76 / 550
    assert call_kwargs["scale_low_arcsec"] == pytest.approx(plate_scale * 0.9)
    assert call_kwargs["scale_high_arcsec"] == pytest.approx(plate_scale * 1.1)


async def test_execute_script_plate_solve_omits_hints_when_unresolvable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _plate_solve_rig()
    _script("solve", steps=[_plate_solve_step()])
    frame_path = tmp_path / "frame-1.fits"
    frame_path.write_bytes(b"fits-bytes")
    _, _, _, _, solve = _mock_plate_solve(monkeypatch, frame_path=frame_path)
    monkeypatch.setattr(fits_headers, "write_fits_headers", MagicMock(return_value=None))

    await script_engine.execute_script("solve", "test-rig", {})

    call_kwargs = solve.call_args.kwargs
    assert call_kwargs["ra_hint_hours"] is None
    assert call_kwargs["dec_hint_deg"] is None
    assert call_kwargs["scale_low_arcsec"] is None
    assert call_kwargs["scale_high_arcsec"] is None


async def test_execute_script_plate_solve_captures_a_fresh_frame_when_exposure_given(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _plate_solve_rig()
    _script("solve", steps=[_plate_solve_step(exposureSeconds=5)])
    _mock_capture_frame_success(monkeypatch)
    frame_path = tmp_path / "frame-1.fits"
    frame_path.write_bytes(b"fits-bytes")
    _mock_plate_solve(monkeypatch, frame_path=frame_path)
    monkeypatch.setattr(fits_headers, "write_fits_headers", MagicMock(return_value=None))

    result = await script_engine.execute_script("solve", "test-rig", {})

    assert result["framesCaptured"] == 1


async def test_execute_script_plate_solve_fails_when_no_frame_has_been_captured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _plate_solve_rig()
    _script("solve", steps=[_plate_solve_step()])
    _mock_plate_solve(monkeypatch, frames=[])

    with pytest.raises(script_engine.ScriptExecutionError, match="no captured frame"):
        await script_engine.execute_script("solve", "test-rig", {})


async def test_execute_script_plate_solve_fails_when_solve_field_does_not_solve(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _plate_solve_rig()
    _script("solve", steps=[_plate_solve_step()])
    frame_path = tmp_path / "frame-1.fits"
    frame_path.write_bytes(b"fits-bytes")
    _mock_plate_solve(monkeypatch, frame_path=frame_path, result=None)

    with pytest.raises(script_engine.ScriptExecutionError, match="did not solve"):
        await script_engine.execute_script("solve", "test-rig", {})


async def test_execute_script_plate_solve_translates_a_cancelled_solve_into_script_cancelled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """`plate_solver.solve` races its own cancel_event and raises asyncio.CancelledError if
    it fires first (see plate_solver.py) — this step must translate that into ScriptCancelled
    rather than letting a bare CancelledError (a BaseException) escape execute_script, so a
    cancel mid-solve is honored the same way cancellation is everywhere else in this engine."""
    _plate_solve_rig()
    _script("solve", steps=[_plate_solve_step()])
    frame_path = tmp_path / "frame-1.fits"
    frame_path.write_bytes(b"fits-bytes")
    _, _, _, _, solve = _mock_plate_solve(monkeypatch, frame_path=frame_path)
    solve.side_effect = asyncio.CancelledError()

    with pytest.raises(script_engine.ScriptCancelled):
        await script_engine.execute_script("solve", "test-rig", {})


def _mock_plate_solve_target(
    monkeypatch: pytest.MonkeyPatch,
    *,
    target_ra_deg_j2000: float = 150.0,
    target_dec_deg_j2000: float = 20.0,
) -> None:
    """Wire up `TARGET_EOD_COORD` on `Mount Simulator` plus a `compute_target_position` mock
    that maps it (regardless of its EOD value) to a known, fixed J2000 position — decoupling
    the retry-loop tests below from real astropy precession math, while still exercising the
    actual conversion/comparison call this engine code makes."""

    def get_property_values(device: str, name: str) -> dict[str, str] | None:
        if name == "CONNECTION":
            return {"CONNECT": "On", "DISCONNECT": "Off"}
        if device == "Mount Simulator" and name == "TARGET_EOD_COORD":
            return {"RA": "10.0", "DEC": "20.0"}
        return None

    monkeypatch.setattr(indi_messaging, "get_property_values", get_property_values)
    monkeypatch.setattr(
        fits_headers,
        "compute_target_position",
        lambda **kwargs: {
            "raDegJ2000": target_ra_deg_j2000,
            "decDegJ2000": target_dec_deg_j2000,
            "raSexagesimalJ2000": "",
            "decSexagesimalJ2000": "",
        },
    )


def _plate_solve_result_at(
    ra_deg_j2000: float, dec_deg_j2000: float
) -> script_engine.plate_solver.PlateSolveResult:
    return script_engine.plate_solver.PlateSolveResult(
        raDegJ2000=ra_deg_j2000,
        decDegJ2000=dec_deg_j2000,
        crpix1=512.0,
        crpix2=512.0,
        ctype1="RA---TAN",
        ctype2="DEC--TAN",
        cd1_1=-0.0002,
        cd1_2=0.0,
        cd2_1=0.0,
        cd2_2=0.0002,
    )


async def test_execute_script_plate_solve_succeeds_within_tolerance_on_first_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _plate_solve_rig()
    _script(
        "solve", steps=[_plate_solve_step(exposureSeconds=5, toleranceArcsec=30, maxAttempts=3)]
    )
    _mock_capture_frame_success(monkeypatch)
    frame_path = tmp_path / "frame-1.fits"
    frame_path.write_bytes(b"fits-bytes")
    _mock_plate_solve_target(monkeypatch)
    send_property, _, _, _, solve = _mock_plate_solve(
        monkeypatch, frame_path=frame_path, result=_plate_solve_result_at(150.0, 20.0)
    )
    write_headers = MagicMock(return_value=None)
    monkeypatch.setattr(fits_headers, "write_fits_headers", write_headers)

    result = await script_engine.execute_script("solve", "test-rig", {})

    solve.assert_awaited_once()
    # once for capture_frame's own DATE-OBS/INSTRUME headers, once for the plate-solved WCS
    assert write_headers.call_count == 2
    assert result["framesCaptured"] == 1
    # exactly one EQUATORIAL_EOD_COORD send: the sync's own — no retry, so no re-slew
    eq_calls = [c for c in send_property.await_args_list if c.args[1] == "EQUATORIAL_EOD_COORD"]
    assert len(eq_calls) == 1


async def test_execute_script_plate_solve_retries_and_reslews_until_within_tolerance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _plate_solve_rig()
    _script(
        "solve", steps=[_plate_solve_step(exposureSeconds=5, toleranceArcsec=30, maxAttempts=3)]
    )
    _mock_capture_frame_success(monkeypatch)
    frame_path = tmp_path / "frame-1.fits"
    frame_path.write_bytes(b"fits-bytes")
    _mock_plate_solve_target(monkeypatch)
    send_property, _, _, _, solve = _mock_plate_solve(monkeypatch, frame_path=frame_path)
    solve.side_effect = [
        _plate_solve_result_at(155.0, 20.0),  # far from the target: won't converge
        _plate_solve_result_at(150.0, 20.0),  # matches the target exactly
    ]
    write_headers = MagicMock(return_value=None)
    monkeypatch.setattr(fits_headers, "write_fits_headers", write_headers)

    result = await script_engine.execute_script("solve", "test-rig", {})

    assert solve.await_count == 2
    assert result["framesCaptured"] == 2
    # every solved attempt gets its WCS written, not just the one that meets tolerance: 2
    # capture_frame header writes + 2 plate-solved WCS writes, one pair per attempt
    assert write_headers.call_count == 4
    # sync (attempt 1) + re-slew (before attempt 2) + sync (attempt 2)
    eq_calls = [c for c in send_property.await_args_list if c.args[1] == "EQUATORIAL_EOD_COORD"]
    assert len(eq_calls) == 3


async def test_execute_script_plate_solve_retries_after_a_failed_solve(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _plate_solve_rig()
    _script(
        "solve", steps=[_plate_solve_step(exposureSeconds=5, toleranceArcsec=30, maxAttempts=3)]
    )
    _mock_capture_frame_success(monkeypatch)
    frame_path = tmp_path / "frame-1.fits"
    frame_path.write_bytes(b"fits-bytes")
    _mock_plate_solve_target(monkeypatch)
    send_property, _, _, _, solve = _mock_plate_solve(monkeypatch, frame_path=frame_path)
    solve.side_effect = [None, _plate_solve_result_at(150.0, 20.0)]
    monkeypatch.setattr(fits_headers, "write_fits_headers", MagicMock(return_value=None))

    result = await script_engine.execute_script("solve", "test-rig", {})

    assert solve.await_count == 2
    assert result["framesCaptured"] == 2
    # no sync happened after the failed first attempt, so attempt 2 must not re-slew -- only
    # the second attempt's own sync sends EQUATORIAL_EOD_COORD
    eq_calls = [c for c in send_property.await_args_list if c.args[1] == "EQUATORIAL_EOD_COORD"]
    assert len(eq_calls) == 1


async def test_execute_script_plate_solve_fails_after_exhausting_max_attempts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _plate_solve_rig()
    _script("solve", steps=[_plate_solve_step(exposureSeconds=5, toleranceArcsec=1, maxAttempts=2)])
    _mock_capture_frame_success(monkeypatch)
    frame_path = tmp_path / "frame-1.fits"
    frame_path.write_bytes(b"fits-bytes")
    _mock_plate_solve_target(monkeypatch)
    _, _, _, _, solve = _mock_plate_solve(
        monkeypatch, frame_path=frame_path, result=_plate_solve_result_at(150.01, 20.0)
    )
    monkeypatch.setattr(fits_headers, "write_fits_headers", MagicMock(return_value=None))

    with pytest.raises(script_engine.ScriptExecutionError, match="did not reach"):
        await script_engine.execute_script("solve", "test-rig", {})

    assert solve.await_count == 2


async def test_execute_script_plate_solve_tolerance_requires_target_eod_coord(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _plate_solve_rig()
    _script("solve", steps=[_plate_solve_step(exposureSeconds=5, toleranceArcsec=10)])
    _mock_capture_frame_success(monkeypatch)
    frame_path = tmp_path / "frame-1.fits"
    frame_path.write_bytes(b"fits-bytes")
    _, _, _, _, solve = _mock_plate_solve(monkeypatch, frame_path=frame_path)
    # default get_property_values reports CONNECTION only -> no TARGET_EOD_COORD

    with pytest.raises(script_engine.ScriptExecutionError, match="TARGET_EOD_COORD"):
        await script_engine.execute_script("solve", "test-rig", {})

    solve.assert_not_awaited()


async def test_execute_script_plate_solve_tolerance_requires_sync_mount_at_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """`PlateSolveStep`'s own validator only catches a literal `syncMount: false` at load
    time — a `"{{ param }}"` reference resolving to `False` at runtime needs its own check,
    since a Condition/reference's actual value isn't known until execution."""
    _plate_solve_rig()
    _script(
        "solve",
        parameters={"sync": script_store.Parameter(type="boolean", required=True)},
        steps=[_plate_solve_step(exposureSeconds=5, toleranceArcsec=10, syncMount="{{ sync }}")],
    )
    _mock_capture_frame_success(monkeypatch)
    frame_path = tmp_path / "frame-1.fits"
    frame_path.write_bytes(b"fits-bytes")
    _, _, _, _, solve = _mock_plate_solve(monkeypatch, frame_path=frame_path)

    with pytest.raises(script_engine.ScriptExecutionError, match="requires syncMount"):
        await script_engine.execute_script("solve", "test-rig", {"sync": False})

    solve.assert_not_awaited()
