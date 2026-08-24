import asyncio
import base64
import inspect
import json
import threading
import time
from collections.abc import Iterator, MutableMapping
from datetime import timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import pydantic
import pytest
from mcp.server.lowlevel.server import NotificationOptions, request_ctx
from mcp.shared.context import RequestContext
from mcp.shared.exceptions import McpError
from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import FileResponse, Response

from indi_mcp import (
    astrometry_index,
    event_log,
    event_streams,
    flat_calibration_sweep,
    frame_store,
    indi_driver,
    indi_messaging,
    indi_server,
    observatory_store,
    plate_solver,
    rig_store,
    script_engine,
    script_runs,
    script_store,
    sensor_calibration_sweep,
    server,
)

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


@pytest.fixture(autouse=True)
def _reset_event_streams() -> None:
    event_streams._messages.clear()
    event_streams._scripts.clear()
    event_streams._connections.clear()
    event_streams._subscribers.clear()
    event_streams._background_tasks.clear()


@pytest.fixture(autouse=True)
def _reset_loaded_scripts() -> None:
    script_store._scripts = {}


@pytest.fixture
def _restore_transport_security() -> Iterator[None]:
    original = server.mcp.settings.transport_security
    yield
    server.mcp.settings.transport_security = original


async def test_manage_indi_infra_server_start_delegates_with_default_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    async def fake_start_server(port: int) -> dict:
        calls.append(port)
        return {"running": True, "port": port}

    monkeypatch.setattr(indi_server, "start_server", fake_start_server)

    result = await server.manage_indi_infra("server", "start")

    assert result == {"running": True, "port": indi_server.INDI_PORT}
    assert calls == [indi_server.INDI_PORT]


async def test_manage_indi_infra_server_start_passes_explicit_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    async def fake_start_server(port: int) -> dict:
        calls.append(port)
        return {"running": True, "port": port}

    monkeypatch.setattr(indi_server, "start_server", fake_start_server)

    await server.manage_indi_infra("server", "start", port=7777)

    assert calls == [7777]


async def test_manage_indi_infra_server_stop_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []

    async def fake_stop_server() -> dict:
        calls.append(True)
        return {"running": False, "port": indi_server.INDI_PORT}

    monkeypatch.setattr(indi_server, "stop_server", fake_stop_server)

    result = await server.manage_indi_infra("server", "stop")

    assert result == {"running": False, "port": indi_server.INDI_PORT}
    assert calls == [True]


async def test_manage_indi_infra_server_restart_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int | None] = []

    async def fake_restart_server(port: int | None) -> dict:
        calls.append(port)
        return {"running": True, "port": port or indi_server.INDI_PORT}

    monkeypatch.setattr(indi_server, "restart_server", fake_restart_server)

    await server.manage_indi_infra("server", "restart", port=9999)

    assert calls == [9999]


async def test_manage_indi_infra_server_rejects_label() -> None:
    with pytest.raises(ValueError, match="label"):
        await server.manage_indi_infra("server", "start", label="CCD Simulator")


async def test_manage_indi_infra_server_stop_rejects_port() -> None:
    with pytest.raises(ValueError, match="port"):
        await server.manage_indi_infra("server", "stop", port=8000)


async def test_manage_indi_infra_driver_start_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_start_driver(label: str) -> dict:
        calls.append(label)
        return {"label": label, "running": True}

    monkeypatch.setattr(indi_driver, "start_driver", fake_start_driver)

    result = await server.manage_indi_infra("driver", "start", label="CCD Simulator")

    assert result == {"label": "CCD Simulator", "running": True}
    assert calls == ["CCD Simulator"]


async def test_manage_indi_infra_driver_stop_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_stop_driver(label: str) -> dict:
        calls.append(label)
        return {"label": label, "running": False}

    monkeypatch.setattr(indi_driver, "stop_driver", fake_stop_driver)

    await server.manage_indi_infra("driver", "stop", label="CCD Simulator")

    assert calls == ["CCD Simulator"]


async def test_manage_indi_infra_driver_requires_label() -> None:
    with pytest.raises(ValueError, match="label"):
        await server.manage_indi_infra("driver", "start")


async def test_manage_indi_infra_driver_rejects_restart() -> None:
    with pytest.raises(ValueError, match="restart"):
        await server.manage_indi_infra("driver", "restart", label="CCD Simulator")


async def test_manage_indi_infra_driver_rejects_host_or_port() -> None:
    with pytest.raises(ValueError, match="host/port"):
        await server.manage_indi_infra("driver", "start", label="CCD Simulator", port=8000)


async def test_manage_indi_infra_messaging_start_delegates_with_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int]] = []

    async def fake_start_messaging(host: str, port: int) -> dict:
        calls.append((host, port))
        return {"running": True, "host": host, "port": port}

    monkeypatch.setattr(indi_messaging, "start_messaging", fake_start_messaging)

    result = await server.manage_indi_infra("messaging", "start")

    assert result == {"running": True, "host": "localhost", "port": indi_server.INDI_PORT}
    assert calls == [("localhost", indi_server.INDI_PORT)]


async def test_manage_indi_infra_messaging_start_passes_explicit_host_and_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int]] = []

    async def fake_start_messaging(host: str, port: int) -> dict:
        calls.append((host, port))
        return {"running": True, "host": host, "port": port}

    monkeypatch.setattr(indi_messaging, "start_messaging", fake_start_messaging)

    await server.manage_indi_infra("messaging", "start", host="10.0.0.5", port=7624)

    assert calls == [("10.0.0.5", 7624)]


async def test_manage_indi_infra_messaging_stop_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []

    async def fake_stop_messaging() -> dict:
        calls.append(True)
        return {"running": False, "host": "localhost", "port": indi_server.INDI_PORT}

    monkeypatch.setattr(indi_messaging, "stop_messaging", fake_stop_messaging)

    await server.manage_indi_infra("messaging", "stop")

    assert calls == [True]


async def test_manage_indi_infra_messaging_rejects_restart() -> None:
    with pytest.raises(ValueError, match="restart"):
        await server.manage_indi_infra("messaging", "restart")


async def test_manage_indi_infra_messaging_rejects_label() -> None:
    with pytest.raises(ValueError, match="label"):
        await server.manage_indi_infra("messaging", "start", label="CCD Simulator")


async def test_get_indi_status_server_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get_status() -> dict:
        return {"running": True, "port": indi_server.INDI_PORT}

    monkeypatch.setattr(indi_server, "get_status", fake_get_status)

    result = await server.get_indi_status("server")

    assert result == {"running": True, "port": indi_server.INDI_PORT}


async def test_get_indi_status_messaging_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get_status() -> dict:
        return {"running": True, "host": "localhost", "port": indi_server.INDI_PORT}

    monkeypatch.setattr(indi_messaging, "get_status", fake_get_status)

    result = await server.get_indi_status("messaging")

    assert result == {"running": True, "host": "localhost", "port": indi_server.INDI_PORT}


async def test_list_indi_drivers_catalog_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get_driver_catalog() -> list[dict]:
        return [{"label": "CCD Simulator", "running": False}]

    monkeypatch.setattr(indi_driver, "get_driver_catalog", fake_get_driver_catalog)

    result = await server.list_indi_drivers("catalog")

    assert result == [{"label": "CCD Simulator", "running": False}]


async def test_list_indi_drivers_running_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_list_running_drivers() -> list[dict]:
        return [{"label": "CCD Simulator", "running": True}]

    monkeypatch.setattr(indi_driver, "list_running_drivers", fake_list_running_drivers)

    result = await server.list_indi_drivers("running")

    assert result == [{"label": "CCD Simulator", "running": True}]


async def test_indi_property_get_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_get_device_properties(device: str) -> dict:
        calls.append(device)
        return {"device": device, "properties": [], "refreshed": True}

    monkeypatch.setattr(indi_messaging, "get_device_properties", fake_get_device_properties)

    result = await server.indi_property("get", "CCD Simulator")

    assert result == {"device": "CCD Simulator", "properties": [], "refreshed": True}
    assert calls == ["CCD Simulator"]


async def test_indi_property_get_rejects_name_or_elements() -> None:
    with pytest.raises(ValueError, match="name/elements"):
        await server.indi_property("get", "CCD Simulator", name="CONNECTION")


async def test_indi_property_set_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, dict]] = []

    async def fake_send_property(device: str, name: str, elements: dict) -> dict:
        calls.append((device, name, elements))
        return {"kind": "propertyCommand", "device": device, "name": name}

    monkeypatch.setattr(indi_messaging, "send_property", fake_send_property)

    result = await server.indi_property(
        "set", "CCD Simulator", name="CONNECTION", elements={"CONNECT": "On"}
    )

    assert result == {"kind": "propertyCommand", "device": "CCD Simulator", "name": "CONNECTION"}
    assert calls == [("CCD Simulator", "CONNECTION", {"CONNECT": "On"})]


async def test_indi_property_set_requires_name_and_elements() -> None:
    with pytest.raises(ValueError, match="requires both name and elements"):
        await server.indi_property("set", "CCD Simulator", name="CONNECTION")


async def test_indi_property_set_requires_name_and_elements_missing_name() -> None:
    with pytest.raises(ValueError, match="requires both name and elements"):
        await server.indi_property("set", "CCD Simulator", elements={"CONNECT": "On"})


async def test_draft_rig_only_fetches_properties_relevant_to_each_devices_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    devices = [
        "CCD Simulator",
        "Filter Wheel Simulator",
        "Focuser Simulator",
        "Telescope Simulator",
        "Unknown Device",
    ]
    families = {
        "CCD Simulator": "CCDs",
        "Filter Wheel Simulator": "Filter Wheels",
        "Focuser Simulator": "Focusers",
        "Telescope Simulator": "Telescopes",
        "Unknown Device": None,
    }
    monkeypatch.setattr(indi_messaging, "list_devices", lambda: devices)
    monkeypatch.setattr(
        indi_driver, "classify_device", AsyncMock(side_effect=lambda name: families[name])
    )

    calls: dict[str, list[tuple]] = {"values": [], "range": []}

    def fake_get_property_values(device: str, name: str) -> dict[str, str] | None:
        calls["values"].append((device, name))
        return {"member": "value"}

    def fake_get_property_range(device: str, name: str, member: str) -> tuple[float, float] | None:
        calls["range"].append((device, name, member))
        return (0.0, 100.0)

    monkeypatch.setattr(indi_messaging, "get_property_values", fake_get_property_values)
    monkeypatch.setattr(indi_messaging, "get_property_range", fake_get_property_range)

    captured: list[rig_store.DraftDeviceInfo] = []

    def fake_draft_rig(devices: list[rig_store.DraftDeviceInfo]) -> rig_store.RigDraft:
        captured.extend(devices)
        return {"kind": "rigDraft", "components": [], "notes": []}

    monkeypatch.setattr(rig_store, "draft_rig", fake_draft_rig)

    result = await server.configuration("draft", "rig")

    assert result == {"kind": "rigDraft", "components": [], "notes": []}
    # Only cameras get CCD_INFO, only filter wheels get FILTER_NAME, only focusers get a range.
    assert calls["values"] == [
        ("CCD Simulator", "CCD_INFO"),
        ("Filter Wheel Simulator", "FILTER_NAME"),
    ]
    assert calls["range"] == [
        ("Focuser Simulator", "ABS_FOCUS_POSITION", "FOCUS_ABSOLUTE_POSITION")
    ]

    by_name = {device["name"]: device for device in captured}
    assert by_name["CCD Simulator"]["family"] == "CCDs"
    assert by_name["CCD Simulator"]["ccdInfo"] == {"member": "value"}
    assert by_name["CCD Simulator"]["filterNames"] is None
    assert by_name["CCD Simulator"]["focusRange"] is None

    assert by_name["Filter Wheel Simulator"]["filterNames"] == {"member": "value"}
    assert by_name["Filter Wheel Simulator"]["ccdInfo"] is None

    assert by_name["Focuser Simulator"]["focusRange"] == (0.0, 100.0)
    assert by_name["Focuser Simulator"]["ccdInfo"] is None

    assert by_name["Telescope Simulator"]["family"] == "Telescopes"
    assert by_name["Telescope Simulator"]["ccdInfo"] is None
    assert by_name["Telescope Simulator"]["filterNames"] is None
    assert by_name["Telescope Simulator"]["focusRange"] is None

    assert by_name["Unknown Device"]["family"] is None
    assert by_name["Unknown Device"]["ccdInfo"] is None
    assert by_name["Unknown Device"]["filterNames"] is None
    assert by_name["Unknown Device"]["focusRange"] is None


async def test_configuration_save_rig_delegates_to_rig_store_with_the_overwrite_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = rig_store.Rig(id="minimal", name="Minimal rig", components=[])
    calls: list[tuple[rig_store.Rig, bool]] = []

    def fake_save_rig(rig: rig_store.Rig, *, overwrite: bool = False) -> rig_store.Rig:
        calls.append((rig, overwrite))
        return rig

    monkeypatch.setattr(rig_store, "save_rig", fake_save_rig)

    result = await server.configuration(
        "save", "rig", config=rig.model_dump(mode="json"), overwrite=True
    )

    assert result == rig
    assert calls == [(rig, True)]


async def test_configuration_get_rejects_config_id_missing() -> None:
    with pytest.raises(ValueError, match="requires config_id"):
        await server.configuration("get", "rig")


async def test_configuration_get_rejects_config_and_overwrite() -> None:
    with pytest.raises(ValueError, match="only valid with"):
        await server.configuration("get", "rig", config_id="minimal", config={})


async def test_configuration_save_rejects_config_id() -> None:
    with pytest.raises(ValueError, match="config_id is not valid"):
        await server.configuration("save", "rig", config_id="minimal", config={})


async def test_configuration_save_requires_config() -> None:
    with pytest.raises(ValueError, match="requires config"):
        await server.configuration("save", "rig")


async def test_configuration_save_rejects_malformed_rig_config() -> None:
    with pytest.raises(pydantic.ValidationError):
        await server.configuration("save", "rig", config={"not": "a valid rig"})


async def test_configuration_save_rejects_malformed_observatory_config() -> None:
    with pytest.raises(pydantic.ValidationError):
        await server.configuration("save", "observatory", config={"not": "a valid observatory"})


async def test_configuration_save_rejects_malformed_script_config() -> None:
    with pytest.raises(pydantic.ValidationError):
        await server.configuration("save", "script", config={"not": "a valid script"})


async def test_configuration_draft_rejects_config_id_config_or_overwrite() -> None:
    with pytest.raises(ValueError, match="not valid with"):
        await server.configuration("draft", "rig", config_id="minimal")


async def test_configuration_draft_rejects_kind_script() -> None:
    with pytest.raises(ValueError, match='not supported for kind="script"'):
        await server.configuration("draft", "script")


async def test_configuration_get_rig_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = rig_store.Rig(id="minimal", name="Minimal rig", components=[])
    monkeypatch.setattr(rig_store, "get_rig", lambda rig_id: rig)

    result = await server.configuration("get", "rig", config_id="minimal")

    assert result == rig


async def test_configuration_get_observatory_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    observatory = observatory_store.Observatory(
        id="minimal", name="Minimal site", latitudeDeg=0, longitudeDeg=0
    )
    monkeypatch.setattr(observatory_store, "get_observatory", lambda observatory_id: observatory)

    result = await server.configuration("get", "observatory", config_id="minimal")

    assert result == observatory


async def test_configuration_get_script_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    script = script_store.Script(
        id="minimal",
        name="Minimal script",
        description="d",
        steps=[],
        parameters={},
        pausable=False,
    )
    monkeypatch.setattr(script_store, "get_script", lambda script_id: script)

    result = await server.configuration("get", "script", config_id="minimal")

    assert result == script


async def test_configuration_save_observatory_delegates_with_overwrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observatory = observatory_store.Observatory(
        id="minimal", name="Minimal site", latitudeDeg=0, longitudeDeg=0
    )
    calls: list[tuple[observatory_store.Observatory, bool]] = []

    def fake_save_observatory(
        observatory: observatory_store.Observatory, *, overwrite: bool = False
    ) -> observatory_store.Observatory:
        calls.append((observatory, overwrite))
        return observatory

    monkeypatch.setattr(observatory_store, "save_observatory", fake_save_observatory)

    result = await server.configuration(
        "save", "observatory", config=observatory.model_dump(mode="json"), overwrite=True
    )

    assert result == observatory
    assert calls == [(observatory, True)]


async def test_configuration_save_script_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    script = script_store.Script(
        id="minimal",
        name="Minimal script",
        description="d",
        steps=[],
        parameters={},
        pausable=False,
    )
    calls: list[tuple[script_store.Script, bool]] = []

    def fake_save_script(
        script: script_store.Script, *, overwrite: bool = False
    ) -> script_store.Script:
        calls.append((script, overwrite))
        return script

    monkeypatch.setattr(script_store, "save_script", fake_save_script)

    result = await server.configuration("save", "script", config=script.model_dump(mode="json"))

    assert result == script
    assert calls == [(script, False)]


async def test_list_config_rig_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rig_store, "list_rigs", lambda: [{"id": "minimal", "name": "Minimal"}])

    assert server.list_config("rig") == [{"id": "minimal", "name": "Minimal"}]


async def test_list_config_observatory_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        observatory_store, "list_observatories", lambda: [{"id": "home", "name": "Home"}]
    )

    assert server.list_config("observatory") == [{"id": "home", "name": "Home"}]


async def test_list_config_script_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        script_store, "list_scripts", lambda: [{"id": "park", "name": "Park", "description": "d"}]
    )

    assert server.list_config("script") == [{"id": "park", "name": "Park", "description": "d"}]


async def test_rig_diagnostics_suggest_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(indi_messaging, "list_devices", lambda: ["CCD Simulator"])
    monkeypatch.setattr(
        rig_store, "suggest_rig", lambda devices: [{"rigId": "minimal", "score": 1.0}]
    )

    result = await server.rig_diagnostics("suggest")

    assert result == [{"rigId": "minimal", "score": 1.0}]


async def test_rig_diagnostics_suggest_rejects_rig_id_role_or_direction() -> None:
    with pytest.raises(ValueError, match="not valid with"):
        await server.rig_diagnostics("suggest", rig_id="minimal")


async def test_rig_diagnostics_check_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(indi_messaging, "list_devices", lambda: ["CCD Simulator"])
    monkeypatch.setattr(
        rig_store, "check_rig", lambda rig_id, devices: {"kind": "rigCheck", "ok": True}
    )

    result = await server.rig_diagnostics("check", rig_id="minimal")

    assert result == {"kind": "rigCheck", "ok": True}


async def test_rig_diagnostics_check_rejects_role_or_direction() -> None:
    with pytest.raises(ValueError, match='only valid with action="sync"'):
        await server.rig_diagnostics("check", rig_id="minimal", role="filterWheel")


async def test_rig_diagnostics_requires_rig_id_for_check_and_sync() -> None:
    with pytest.raises(ValueError, match="requires rig_id"):
        await server.rig_diagnostics("check")


async def test_rig_diagnostics_sync_to_driver_delegates_to_script_engine_with_resolved_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = rig_store.Rig(
        id="test-rig",
        name="Test rig",
        components=[
            rig_store.Component(
                role="filterWheel",
                id="fw-1",
                device="Filter Wheel Simulator",
                slots={1: "Luminance", 2: "Red"},
            )
        ],
    )
    monkeypatch.setattr(rig_store, "get_rig", lambda rig_id: rig)

    calls: list[tuple[str, str, dict[int, str]]] = []

    async def fake_sync_filter_names(
        role: str, device: str, rig_slots: dict[int, str]
    ) -> script_engine.FilterSyncOutcome:
        calls.append((role, device, rig_slots))
        return {"status": "matched", "rigSlots": rig_slots, "liveSlots": rig_slots}

    monkeypatch.setattr(script_engine, "sync_filter_names", fake_sync_filter_names)

    result = await server.rig_diagnostics(
        "sync", rig_id="test-rig", role="filterWheel", direction="to_driver"
    )

    assert calls == [("filterWheel", "Filter Wheel Simulator", {1: "Luminance", 2: "Red"})]
    assert result == {
        "status": "matched",
        "rigSlots": {1: "Luminance", 2: "Red"},
        "liveSlots": {1: "Luminance", 2: "Red"},
    }


async def test_rig_diagnostics_sync_raises_when_role_has_no_connected_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = rig_store.Rig(id="test-rig", name="Test rig", components=[])
    monkeypatch.setattr(rig_store, "get_rig", lambda rig_id: rig)

    with pytest.raises(ValueError, match="expected exactly one"):
        await server.rig_diagnostics(
            "sync", rig_id="test-rig", role="filterWheel", direction="to_driver"
        )


async def test_rig_diagnostics_sync_raises_when_role_matches_more_than_one_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = rig_store.Rig(
        id="test-rig",
        name="Test rig",
        components=[
            rig_store.Component(role="filterWheel", id="fw-1", device="Filter Wheel Simulator"),
            rig_store.Component(role="filterWheel", id="fw-2", device="Filter Wheel Simulator 2"),
        ],
    )
    monkeypatch.setattr(rig_store, "get_rig", lambda rig_id: rig)

    with pytest.raises(ValueError, match="expected exactly one"):
        await server.rig_diagnostics(
            "sync", rig_id="test-rig", role="filterWheel", direction="to_driver"
        )


async def test_rig_diagnostics_sync_from_driver_delegates_to_script_engine_with_resolved_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = rig_store.Rig(
        id="test-rig",
        name="Test rig",
        components=[
            rig_store.Component(
                role="filterWheel",
                id="fw-1",
                device="Filter Wheel Simulator",
                slots={1: "Luminance", 2: "Red"},
            )
        ],
    )
    monkeypatch.setattr(rig_store, "get_rig", lambda rig_id: rig)

    calls: list[tuple[str, str, str, dict[int, str]]] = []

    async def fake_adopt_filter_names_from_driver(
        rig_id: str, role: str, device: str, rig_slots: dict[int, str]
    ) -> script_engine.FilterAdoptOutcome:
        calls.append((rig_id, role, device, rig_slots))
        return {"status": "matched", "rigSlots": rig_slots, "liveSlots": rig_slots}

    monkeypatch.setattr(
        script_engine, "adopt_filter_names_from_driver", fake_adopt_filter_names_from_driver
    )

    result = await server.rig_diagnostics(
        "sync", rig_id="test-rig", role="filterWheel", direction="from_driver"
    )

    assert calls == [
        ("test-rig", "filterWheel", "Filter Wheel Simulator", {1: "Luminance", 2: "Red"})
    ]
    assert result == {
        "status": "matched",
        "rigSlots": {1: "Luminance", 2: "Red"},
        "liveSlots": {1: "Luminance", 2: "Red"},
    }


async def test_rig_diagnostics_sync_from_driver_raises_when_role_has_no_connected_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = rig_store.Rig(id="test-rig", name="Test rig", components=[])
    monkeypatch.setattr(rig_store, "get_rig", lambda rig_id: rig)

    with pytest.raises(ValueError, match="expected exactly one"):
        await server.rig_diagnostics(
            "sync", rig_id="test-rig", role="filterWheel", direction="from_driver"
        )


async def test_rig_diagnostics_sync_from_driver_raises_when_role_matches_more_than_one_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = rig_store.Rig(
        id="test-rig",
        name="Test rig",
        components=[
            rig_store.Component(role="filterWheel", id="fw-1", device="Filter Wheel Simulator"),
            rig_store.Component(role="filterWheel", id="fw-2", device="Filter Wheel Simulator 2"),
        ],
    )
    monkeypatch.setattr(rig_store, "get_rig", lambda rig_id: rig)

    with pytest.raises(ValueError, match="expected exactly one"):
        await server.rig_diagnostics(
            "sync", rig_id="test-rig", role="filterWheel", direction="from_driver"
        )


async def test_rig_diagnostics_sync_requires_role_and_direction() -> None:
    with pytest.raises(ValueError, match="requires both role and direction"):
        await server.rig_diagnostics("sync", rig_id="test-rig")


async def test_rig_diagnostics_sync_requires_direction_when_role_given() -> None:
    with pytest.raises(ValueError, match="requires both role and direction"):
        await server.rig_diagnostics("sync", rig_id="test-rig", role="filterWheel")


async def test_draft_observatory_only_fetches_state_for_devices_reporting_the_coord(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    devices = ["Telescope Simulator", "GPS Simulator", "CCD Simulator"]
    monkeypatch.setattr(indi_messaging, "list_devices", lambda: devices)

    values = {
        "Telescope Simulator": {"LAT": "52.3676", "LONG": "4.9041", "ELEV": "4"},
        "GPS Simulator": None,
        "CCD Simulator": None,
    }
    value_calls: list[tuple[str, str]] = []

    def fake_get_property_values(device: str, name: str) -> dict[str, str] | None:
        value_calls.append((device, name))
        return values[device]

    state_calls: list[tuple[str, str]] = []

    def fake_get_property_state(device: str, name: str) -> str | None:
        state_calls.append((device, name))
        return "Ok"

    monkeypatch.setattr(indi_messaging, "get_property_values", fake_get_property_values)
    monkeypatch.setattr(indi_messaging, "get_property_state", fake_get_property_state)

    captured: list[observatory_store.DraftLocationDeviceInfo] = []

    def fake_draft_observatory(
        devices: list[observatory_store.DraftLocationDeviceInfo],
    ) -> observatory_store.ObservatoryDraft:
        captured.extend(devices)
        return {
            "kind": "observatoryDraft",
            "id": None,
            "name": None,
            "latitudeDeg": None,
            "longitudeDeg": None,
            "elevationMeters": None,
            "sourceDevice": None,
            "notes": [],
        }

    monkeypatch.setattr(observatory_store, "draft_observatory", fake_draft_observatory)

    result = await server.configuration("draft", "observatory")

    assert result["kind"] == "observatoryDraft"
    # GEOGRAPHIC_COORD is queried for every device; state is only queried when it's present.
    assert value_calls == [
        ("Telescope Simulator", "GEOGRAPHIC_COORD"),
        ("GPS Simulator", "GEOGRAPHIC_COORD"),
        ("CCD Simulator", "GEOGRAPHIC_COORD"),
    ]
    assert state_calls == [("Telescope Simulator", "GEOGRAPHIC_COORD")]

    by_name = {device["name"]: device for device in captured}
    assert by_name["Telescope Simulator"]["geographicCoord"] == {
        "LAT": "52.3676",
        "LONG": "4.9041",
        "ELEV": "4",
    }
    assert by_name["Telescope Simulator"]["state"] == "Ok"
    assert by_name["GPS Simulator"]["geographicCoord"] is None
    assert by_name["GPS Simulator"]["state"] is None


async def test_run_script_delegates_to_script_runs_start_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, dict, str | None]] = []

    async def fake_start_script(
        script_id: str, rig_id: str, parameters: dict, *, location_id: str | None = None
    ) -> dict:
        calls.append((script_id, rig_id, parameters, location_id))
        return {"kind": "scriptStarted", "runId": "abc"}

    monkeypatch.setattr(script_runs, "start_script", fake_start_script)

    result = await server.run_script("capture_sequence", "test-rig", {"count": 10})

    assert result == {"kind": "scriptStarted", "runId": "abc"}
    assert calls == [("capture_sequence", "test-rig", {"count": 10}, None)]


async def test_run_script_passes_location_id_through_to_start_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str | None] = []

    async def fake_start_script(
        script_id: str, rig_id: str, parameters: dict, *, location_id: str | None = None
    ) -> dict:
        calls.append(location_id)
        return {"kind": "scriptStarted", "runId": "abc"}

    monkeypatch.setattr(script_runs, "start_script", fake_start_script)

    await server.run_script("capture_sequence", "test-rig", {}, "home-backyard")

    assert calls == ["home-backyard"]


def _fake_start_script(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, str, dict, str | None]]:
    """Patch `script_runs.start_script` and return the list its calls get recorded into —
    shared by every "convenience wrapper delegates to start_script" test below."""
    calls: list[tuple[str, str, dict, str | None]] = []

    async def fake_start_script(
        script_id: str, rig_id: str, parameters: dict, *, location_id: str | None = None
    ) -> dict:
        calls.append((script_id, rig_id, parameters, location_id))
        return {"kind": "scriptStarted", "runId": "abc"}

    monkeypatch.setattr(script_runs, "start_script", fake_start_script)
    return calls


async def test_mount_action_park_delegates_to_start_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_start_script(monkeypatch)

    result = await server.mount_action("test-rig", "park")

    assert result == {"kind": "scriptStarted", "runId": "abc"}
    assert calls == [("park", "test-rig", {}, None)]


async def test_mount_action_unpark_delegates_to_start_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_start_script(monkeypatch)

    await server.mount_action("test-rig", "unpark")

    assert calls == [("unpark", "test-rig", {}, None)]


async def test_mount_action_track_off_delegates_to_start_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_start_script(monkeypatch)

    await server.mount_action("test-rig", "track_off")

    assert calls == [("track_off", "test-rig", {}, None)]


@pytest.mark.parametrize("action", ["park", "unpark", "track_off"])
async def test_mount_action_no_param_actions_reject_any_parameter(action: str) -> None:
    with pytest.raises(ValueError, match="requires exactly"):
        await server.mount_action("test-rig", action, ra=10.5)  # type: ignore[arg-type]


async def test_mount_action_slew_delegates_to_start_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_start_script(monkeypatch)

    await server.mount_action("test-rig", "slew", ra=10.5, dec=41.2)

    assert calls == [("slew", "test-rig", {"ra": 10.5, "dec": 41.2}, None)]


async def test_mount_action_slew_requires_ra_and_dec() -> None:
    with pytest.raises(ValueError, match="requires exactly"):
        await server.mount_action("test-rig", "slew", ra=10.5)


async def test_mount_action_set_track_mode_delegates_to_start_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_start_script(monkeypatch)

    await server.mount_action("test-rig", "set_track_mode", modeSwitchElement="TRACK_SIDEREAL")

    assert calls == [("set_track_mode", "test-rig", {"modeSwitchElement": "TRACK_SIDEREAL"}, None)]


async def test_mount_action_set_custom_tracking_rate_delegates_to_start_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_start_script(monkeypatch)

    await server.mount_action(
        "test-rig", "set_custom_tracking_rate", raRateArcsecPerSec=15.0, decRateArcsecPerSec=-0.5
    )

    assert calls == [
        (
            "set_custom_tracking_rate",
            "test-rig",
            {"raRateArcsecPerSec": 15.0, "decRateArcsecPerSec": -0.5},
            None,
        )
    ]


async def test_mount_action_set_custom_tracking_rate_requires_both_rates() -> None:
    with pytest.raises(ValueError, match="requires exactly"):
        await server.mount_action("test-rig", "set_custom_tracking_rate", raRateArcsecPerSec=15.0)


async def test_camera_action_cool_delegates_to_start_script_with_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_start_script(monkeypatch)

    await server.camera_action("test-rig", "cool")

    assert calls == [("cool_camera", "test-rig", {"targetTempC": -10, "timeoutSeconds": 300}, None)]


async def test_camera_action_cool_passes_explicit_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_start_script(monkeypatch)

    await server.camera_action("test-rig", "cool", targetTempC=-20, timeoutSeconds=60)

    assert calls == [("cool_camera", "test-rig", {"targetTempC": -20, "timeoutSeconds": 60}, None)]


async def test_camera_action_cool_passes_partial_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only targetTempC given, timeoutSeconds omitted -- each parameter's None-sentinel
    resolution must be independent, not accidentally coupled to whether the other was given.
    """
    calls = _fake_start_script(monkeypatch)

    await server.camera_action("test-rig", "cool", targetTempC=-20)

    assert calls == [("cool_camera", "test-rig", {"targetTempC": -20, "timeoutSeconds": 300}, None)]


async def test_camera_action_cooler_on_delegates_to_start_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_start_script(monkeypatch)

    await server.camera_action("test-rig", "cooler_on")

    assert calls == [("cooler_on", "test-rig", {}, None)]


async def test_camera_action_cooler_off_delegates_to_start_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_start_script(monkeypatch)

    await server.camera_action("test-rig", "cooler_off")

    assert calls == [("cooler_off", "test-rig", {}, None)]


async def test_camera_action_abort_exposure_delegates_to_start_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_start_script(monkeypatch)

    await server.camera_action("test-rig", "abort_exposure")

    assert calls == [("abort_exposure", "test-rig", {}, None)]


@pytest.mark.parametrize("action", ["cooler_on", "cooler_off", "abort_exposure"])
async def test_camera_action_no_param_actions_reject_any_parameter(action: str) -> None:
    with pytest.raises(ValueError, match="doesn't accept"):
        await server.camera_action("test-rig", action, targetTempC=-20)  # type: ignore[arg-type]


async def test_camera_action_capture_frame_delegates_to_start_script_with_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_start_script(monkeypatch)

    await server.camera_action("test-rig", "capture_frame", exposureSeconds=300)

    assert calls == [
        (
            "capture_frame",
            "test-rig",
            {
                "exposureSeconds": 300,
                "frameType": "Light",
                "binningX": 1,
                "binningY": 1,
                "gain": None,
                "offset": None,
                "frameX": None,
                "frameY": None,
                "frameWidth": None,
                "frameHeight": None,
            },
            None,
        )
    ]


async def test_camera_action_capture_frame_passes_location_id_through_to_start_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_start_script(monkeypatch)

    await server.camera_action(
        "test-rig", "capture_frame", exposureSeconds=300, location_id="home-backyard"
    )

    assert calls[0][3] == "home-backyard"


async def test_camera_action_capture_frame_requires_exposure_seconds() -> None:
    with pytest.raises(ValueError, match="requires"):
        await server.camera_action("test-rig", "capture_frame")


async def test_camera_action_cool_rejects_capture_frame_parameters() -> None:
    with pytest.raises(ValueError, match="doesn't accept"):
        await server.camera_action("test-rig", "cool", exposureSeconds=300)


async def test_filter_wheel_action_select_delegates_to_start_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_start_script(monkeypatch)

    await server.filter_wheel_action("test-rig", "select", filterName="Ha")

    assert calls == [("select_filter", "test-rig", {"filterName": "Ha"}, None)]


async def test_focuser_action_set_position_delegates_to_start_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_start_script(monkeypatch)

    await server.focuser_action("test-rig", "set_position", position=15000)

    assert calls == [("set_focus_position", "test-rig", {"position": 15000}, None)]


async def test_set_connection_true_delegates_to_connect_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_start_script(monkeypatch)

    await server.set_connection("test-rig", role="mount", connected=True)

    assert calls == [("connect", "test-rig", {"role": "mount"}, None)]


async def test_set_connection_false_delegates_to_disconnect_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_start_script(monkeypatch)

    await server.set_connection("test-rig", role="mount", connected=False)

    assert calls == [("disconnect", "test-rig", {"role": "mount"}, None)]


async def test_plate_solve_delegates_to_start_script(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_start_script(monkeypatch)

    await server.plate_solve("test-rig", exposureSeconds=5.0, syncMount=False, timeoutSeconds=30)

    assert calls == [
        (
            "plate_solve",
            "test-rig",
            {"exposureSeconds": 5.0, "syncMount": False, "timeoutSeconds": 30},
            None,
        )
    ]


async def test_plate_solve_until_precision_delegates_to_start_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_start_script(monkeypatch)

    await server.plate_solve_until_precision(
        "test-rig", exposureSeconds=5.0, toleranceArcsec=15, maxAttempts=5, timeoutSeconds=30
    )

    assert calls == [
        (
            "plate_solve_until_precision",
            "test-rig",
            {
                "exposureSeconds": 5.0,
                "toleranceArcsec": 15,
                "maxAttempts": 5,
                "timeoutSeconds": 30,
            },
            None,
        )
    ]


async def test_list_astrometry_index_files_delegates_without_a_rig(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, rig_store.Rig | None, float | None, float | None]] = []

    def fake_list_index_files(
        *, catalog="tycho2", directory=None, rig=None, min_arcmin=None, max_arcmin=None
    ):
        calls.append((catalog, rig, min_arcmin, max_arcmin))
        return [{"indexNumber": 7, "installed": True}]

    monkeypatch.setattr(astrometry_index, "list_index_files", fake_list_index_files)

    result = await server.list_astrometry_index_files()

    assert result == [{"indexNumber": 7, "installed": True}]
    assert calls == [("tycho2", None, None, None)]


async def test_list_astrometry_index_files_passes_catalog_and_rig(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = rig_store.Rig(id="test-rig", name="Test rig", components=[])
    monkeypatch.setattr(rig_store, "get_rig", lambda rig_id: rig)
    calls: list[tuple[str, rig_store.Rig | None, float | None, float | None]] = []
    monkeypatch.setattr(
        astrometry_index,
        "list_index_files",
        lambda *, catalog="tycho2", directory=None, rig=None, min_arcmin=None, max_arcmin=None: (
            calls.append((catalog, rig, min_arcmin, max_arcmin)) or []
        ),
    )

    await server.list_astrometry_index_files(catalog="2mass", rig_id="test-rig")

    assert calls == [("2mass", rig, None, None)]


async def test_list_astrometry_index_files_passes_explicit_arcmin_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, rig_store.Rig | None, float | None, float | None]] = []
    monkeypatch.setattr(
        astrometry_index,
        "list_index_files",
        lambda *, catalog="tycho2", directory=None, rig=None, min_arcmin=None, max_arcmin=None: (
            calls.append((catalog, rig, min_arcmin, max_arcmin)) or []
        ),
    )

    await server.list_astrometry_index_files(minArcmin=23.0, maxArcmin=29.0)

    assert calls == [("tycho2", None, 23.0, 29.0)]


async def test_list_astrometry_index_files_rejects_rig_and_arcmin_range_together() -> None:
    with pytest.raises(ValueError, match="not both"):
        await server.list_astrometry_index_files(rig_id="test-rig", minArcmin=23.0, maxArcmin=29.0)


async def test_list_astrometry_index_files_rejects_partial_arcmin_range() -> None:
    with pytest.raises(ValueError, match="pass both minArcmin and maxArcmin"):
        await server.list_astrometry_index_files(minArcmin=23.0)


async def test_download_astrometry_index_files_with_explicit_index_numbers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[int], str]] = []

    async def fake_download(
        index_numbers: list[int], *, catalog="tycho2", directory=None
    ) -> list[int]:
        calls.append((index_numbers, catalog))
        return index_numbers

    monkeypatch.setattr(astrometry_index, "download_index_files", fake_download)

    result = await server.download_astrometry_index_files(indexNumbers=[7, 8], catalog="2mass")

    assert result == [7, 8]
    assert calls == [([7, 8], "2mass")]


async def test_download_astrometry_index_files_with_explicit_arcmin_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[int]] = []

    async def fake_download(
        index_numbers: list[int], *, catalog="tycho2", directory=None
    ) -> list[int]:
        calls.append(index_numbers)
        return index_numbers

    monkeypatch.setattr(astrometry_index, "download_index_files", fake_download)

    result = await server.download_astrometry_index_files(minArcmin=23.0, maxArcmin=29.0)

    assert result == [7]
    assert calls == [[7]]


async def test_download_astrometry_index_files_with_rig_id_computes_the_range_with_margin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = rig_store.Rig(
        id="test-rig",
        name="Test rig",
        components=[
            rig_store.Component(role="telescope", id="scope-1", focalLengthMm=750.0),
            rig_store.Component(
                role="camera",
                id="cam-1",
                device="CCD Simulator",
                pixelSizeMicron=3.76,
                pixelsX=6248,
                pixelsY=4176,
            ),
        ],
    )
    monkeypatch.setattr(rig_store, "get_rig", lambda rig_id: rig)
    calls: list[list[int]] = []

    async def fake_download(
        index_numbers: list[int], *, catalog="tycho2", directory=None
    ) -> list[int]:
        calls.append(index_numbers)
        return index_numbers

    monkeypatch.setattr(astrometry_index, "download_index_files", fake_download)

    await server.download_astrometry_index_files(rig_id="test-rig")

    min_arcmin, max_arcmin = astrometry_index.field_of_view_arcmin_for_rig(rig)
    expected = astrometry_index.index_numbers_for_field_of_view(
        min_arcmin, max_arcmin, margin_scales=astrometry_index.DEFAULT_RIG_MARGIN_SCALES
    )
    assert calls == [expected]
    assert len(expected) > 1  # confirms the margin actually widened the exact bracket


async def test_download_astrometry_index_files_requires_one_selector() -> None:
    with pytest.raises(ValueError, match="pass exactly one of"):
        await server.download_astrometry_index_files()


async def test_download_astrometry_index_files_rejects_more_than_one_selector() -> None:
    with pytest.raises(ValueError, match="pass exactly one of"):
        await server.download_astrometry_index_files(indexNumbers=[7], rig_id="test-rig")


async def test_download_astrometry_index_files_rejects_partial_arcmin_range() -> None:
    with pytest.raises(ValueError, match="pass both minArcmin and maxArcmin"):
        await server.download_astrometry_index_files(minArcmin=23.0)


async def test_download_astrometry_index_files_rejects_a_range_no_scale_covers() -> None:
    # tycho2 only publishes scales 7-19 (22 arcmin+) -- a narrower range matches nothing
    with pytest.raises(ValueError, match="no known 'tycho2' scale covers"):
        await server.download_astrometry_index_files(minArcmin=1.0, maxArcmin=2.0)


async def test_plate_solve_uploaded_frame_decodes_base64_and_delegates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[bytes, dict[str, float | None]]] = []

    async def fake_solve_uploaded_frame(data: bytes, **kwargs: float | None) -> dict:
        calls.append((data, kwargs))
        return {"frameId": "frame-1", "raDegJ2000": 150.0, "decDegJ2000": 20.0}

    monkeypatch.setattr(plate_solver, "solve_uploaded_frame", fake_solve_uploaded_frame)

    result = await server.plate_solve_uploaded_frame(
        base64.b64encode(b"fits-bytes").decode(),
        raHintHours=10.0,
        decHintDeg=20.0,
        scaleLowArcsecPerPixel=1.0,
        scaleHighArcsecPerPixel=2.0,
        timeoutSeconds=30,
    )

    assert result == {"frameId": "frame-1", "raDegJ2000": 150.0, "decDegJ2000": 20.0}
    assert len(calls) == 1
    data, kwargs = calls[0]
    assert data == b"fits-bytes"
    assert kwargs == {
        "ra_hint_hours": 10.0,
        "dec_hint_deg": 20.0,
        "scale_low_arcsec": 1.0,
        "scale_high_arcsec": 2.0,
        "timeout_seconds": 30,
    }


_WRAPPER_TOOLS_BY_SCRIPT_ID = {
    "plate_solve": server.plate_solve,
    "plate_solve_until_precision": server.plate_solve_until_precision,
}


@pytest.mark.parametrize("script_id", sorted(_WRAPPER_TOOLS_BY_SCRIPT_ID))
def test_wrapper_tool_signature_matches_the_scripts_own_parameters(script_id: str) -> None:
    """Each remaining one-tool-per-script convenience wrapper hand-encodes its script's own
    `parameters:` block as Python parameter names/required-ness/defaults — nothing else keeps
    the two in sync, so this cross-checks the wrapper's actual signature against the loaded
    script's declared `Parameter`s directly, rather than against a third hardcoded expectations
    list, turning a future YAML/Python desync into a fast, specific test failure instead of a
    confusing `scriptFailed` at call time.

    `park`/`unpark`/`slew`/`cool_camera`/`select_filter`/`set_focus_position`/`connect`/
    `disconnect`/`capture_frame`/`track_off`/`set_track_mode`/`set_custom_tracking_rate` used
    to be covered here too (INDIMCP-49), before INDIMCP-116 grouped them into `mount_action`/
    `camera_action`/`filter_wheel_action`/`focuser_action`/`set_connection` — see
    `test_merged_action_tool_params_match_the_scripts_own_parameters` below for their
    replacement coverage; a single tool's signature spanning several scripts' worth of
    parameters can't be introspected this same way (every parameter is optional at the Python
    level regardless of which action actually requires it, so `inspect.signature` alone can no
    longer tell required from optional per action).
    """
    script_store.load_scripts(SCRIPTS_DIR)
    script = script_store.get_script(script_id)
    signature = inspect.signature(_WRAPPER_TOOLS_BY_SCRIPT_ID[script_id])

    # rig_id and (capture_frame's) location_id are wrapper-only, not script parameters.
    wrapper_param_names = {
        name for name in signature.parameters if name not in ("rig_id", "location_id")
    }
    assert wrapper_param_names == set(script.parameters), (
        f"{script_id}'s wrapper parameters {sorted(wrapper_param_names)} don't match its "
        f"script's declared parameters {sorted(script.parameters)}"
    )
    for name, parameter in script.parameters.items():
        wrapper_default = signature.parameters[name].default
        wrapper_has_default = wrapper_default is not inspect.Parameter.empty
        assert wrapper_has_default != parameter.required, (
            f"{script_id}.{name}: script declares required={parameter.required}, but the "
            f"wrapper {'has' if wrapper_has_default else 'has no'} a default"
        )
        if wrapper_has_default:
            assert wrapper_default == parameter.default, (
                f"{script_id}.{name}: wrapper default {wrapper_default!r} != script "
                f"default {parameter.default!r}"
            )


_MERGED_ACTION_SCRIPT_PARAMS: dict[str, set[str]] = {
    "park": server._MOUNT_ACTION_PARAMS["park"],
    "unpark": server._MOUNT_ACTION_PARAMS["unpark"],
    "slew": server._MOUNT_ACTION_PARAMS["slew"],
    "track_off": server._MOUNT_ACTION_PARAMS["track_off"],
    "set_track_mode": server._MOUNT_ACTION_PARAMS["set_track_mode"],
    "set_custom_tracking_rate": server._MOUNT_ACTION_PARAMS["set_custom_tracking_rate"],
    "cool_camera": server._CAMERA_ACTION_ALLOWED_PARAMS["cool"],
    "cooler_on": server._CAMERA_ACTION_ALLOWED_PARAMS["cooler_on"],
    "cooler_off": server._CAMERA_ACTION_ALLOWED_PARAMS["cooler_off"],
    "abort_exposure": server._CAMERA_ACTION_ALLOWED_PARAMS["abort_exposure"],
    "capture_frame": server._CAMERA_ACTION_ALLOWED_PARAMS["capture_frame"] - {"location_id"},
    "select_filter": {"filterName"},
    "set_focus_position": {"position"},
    "connect": {"role"},
    "disconnect": {"role"},
}
_MERGED_ACTION_SCRIPT_REQUIRED_PARAMS: dict[str, set[str]] = {
    "park": set(),
    "unpark": set(),
    "slew": {"ra", "dec"},
    "track_off": set(),
    "set_track_mode": {"modeSwitchElement"},
    "set_custom_tracking_rate": {"raRateArcsecPerSec", "decRateArcsecPerSec"},
    "cool_camera": set(),
    "cooler_on": set(),
    "cooler_off": set(),
    "abort_exposure": set(),
    "capture_frame": {"exposureSeconds"},
    "select_filter": {"filterName"},
    "set_focus_position": {"position"},
    "connect": {"role"},
    "disconnect": {"role"},
}
_MERGED_ACTION_SCRIPT_RESOLVED_DEFAULTS: dict[str, dict[str, object]] = {
    "cool_camera": {"targetTempC": -10, "timeoutSeconds": 300},
    "capture_frame": {"frameType": "Light", "binningX": 1, "binningY": 1},
}


@pytest.mark.parametrize("script_id", sorted(_MERGED_ACTION_SCRIPT_PARAMS))
def test_merged_action_tool_params_match_the_scripts_own_parameters(script_id: str) -> None:
    """Same cross-check as `test_wrapper_tool_signature_matches_the_scripts_own_parameters`,
    adapted for the `*_action` tools (INDIMCP-116) that replaced the old one-tool-per-script
    wrappers for mount/camera/filter-wheel/focuser/connection scripts. Their Python signature
    spans every action the tool supports, not just one script's parameters, and every
    parameter defaults to `None` in the signature regardless of whether the particular action
    actually requires it — so `inspect.signature` alone can no longer distinguish required
    from optional per action, or reveal a resolved default that isn't `None`. This checks
    against the tool's own per-action allowed/required/resolved-default dicts
    (`_MOUNT_ACTION_PARAMS` etc.) instead — the same single source of truth the dispatch code
    itself uses, so a future YAML/Python desync still fails here rather than only surfacing as
    a confusing `scriptFailed` at call time.
    """
    script_store.load_scripts(SCRIPTS_DIR)
    script = script_store.get_script(script_id)

    expected_params = _MERGED_ACTION_SCRIPT_PARAMS[script_id]
    assert expected_params == set(script.parameters), (
        f"{script_id}'s expected parameters {sorted(expected_params)} don't match its "
        f"script's declared parameters {sorted(script.parameters)}"
    )

    expected_required = _MERGED_ACTION_SCRIPT_REQUIRED_PARAMS[script_id]
    resolved_defaults = _MERGED_ACTION_SCRIPT_RESOLVED_DEFAULTS.get(script_id, {})
    for name, parameter in script.parameters.items():
        assert (name in expected_required) == parameter.required, (
            f"{script_id}.{name}: script declares required={parameter.required}, but the "
            f"tool's own required-params set does"
            f"{'' if name in expected_required else ' not'} include it"
        )
        if name in resolved_defaults:
            assert resolved_defaults[name] == parameter.default, (
                f"{script_id}.{name}: tool resolves the omitted default to "
                f"{resolved_defaults[name]!r}, but the script declares default "
                f"{parameter.default!r}"
            )


async def test_manage_script_run_status_delegates_to_script_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_get_script_status(run_id: str) -> dict:
        calls.append(run_id)
        return {"kind": "scriptProgress", "runId": run_id}

    monkeypatch.setattr(script_runs, "get_script_status", fake_get_script_status)

    result = await server.manage_script_run("abc", "status")

    assert result == {"kind": "scriptProgress", "runId": "abc"}
    assert calls == ["abc"]


async def test_manage_script_run_cancel_delegates_to_script_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_cancel_script(run_id: str) -> dict:
        calls.append(run_id)
        return {"kind": "scriptCancelled", "runId": run_id}

    monkeypatch.setattr(script_runs, "cancel_script", fake_cancel_script)

    result = await server.manage_script_run("abc", "cancel")

    assert result == {"kind": "scriptCancelled", "runId": "abc"}
    assert calls == ["abc"]


async def test_manage_script_run_pause_delegates_to_script_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_pause_script(run_id: str) -> dict:
        calls.append(run_id)
        return {"kind": "scriptPaused", "runId": run_id}

    monkeypatch.setattr(script_runs, "pause_script", fake_pause_script)

    result = await server.manage_script_run("abc", "pause")

    assert result == {"kind": "scriptPaused", "runId": "abc"}
    assert calls == ["abc"]


async def test_manage_script_run_resume_delegates_to_script_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_resume_script(run_id: str) -> dict:
        calls.append(run_id)
        return {"kind": "scriptResumed", "runId": run_id}

    monkeypatch.setattr(script_runs, "resume_script", fake_resume_script)

    result = await server.manage_script_run("abc", "resume")

    assert result == {"kind": "scriptResumed", "runId": "abc"}
    assert calls == ["abc"]


async def test_run_calibration_sweep_sensor_delegates_to_sensor_calibration_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple] = []

    async def fake_start_sweep(
        rig_id: str,
        gains: list[float],
        offsets: list[float],
        flat_exposure_seconds_list: list[float],
        bias_count: int,
        dark_count: int,
        *,
        bias_exposure_seconds: float = 0.0,
        location_id: str | None = None,
    ) -> dict:
        calls.append(
            (
                rig_id,
                gains,
                offsets,
                flat_exposure_seconds_list,
                bias_count,
                dark_count,
                bias_exposure_seconds,
                location_id,
            )
        )
        return {"kind": "sensorCalibrationSweepStarted", "sweepId": "sweep-1"}

    monkeypatch.setattr(sensor_calibration_sweep, "start_sweep", fake_start_sweep)

    result = await server.run_calibration_sweep(
        "sensor",
        "test-rig",
        gains=[50, 100],
        offsets=[10, 20],
        flatExposureSecondsList=[1.0, 2.0],
        biasCount=3,
        darkCount=2,
        biasExposureSeconds=0.5,
        location_id="home-backyard",
    )

    assert result == {"kind": "sensorCalibrationSweepStarted", "sweepId": "sweep-1"}
    # Positional args land in the right slots — biasCount/darkCount are both plain ints, so a
    # swapped argument order here would silently pass every type check.
    assert calls == [("test-rig", [50, 100], [10, 20], [1.0, 2.0], 3, 2, 0.5, "home-backyard")]


async def test_run_calibration_sweep_sensor_defaults_bias_exposure_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[float] = []

    async def fake_start_sweep(
        rig_id: str,
        gains: list[float],
        offsets: list[float],
        flat_exposure_seconds_list: list[float],
        bias_count: int,
        dark_count: int,
        *,
        bias_exposure_seconds: float = 0.0,
        location_id: str | None = None,
    ) -> dict:
        calls.append(bias_exposure_seconds)
        return {"kind": "sensorCalibrationSweepStarted", "sweepId": "sweep-1"}

    monkeypatch.setattr(sensor_calibration_sweep, "start_sweep", fake_start_sweep)

    await server.run_calibration_sweep(
        "sensor",
        "test-rig",
        gains=[50],
        offsets=[10],
        flatExposureSecondsList=[1.0],
        biasCount=3,
        darkCount=2,
    )

    assert calls == [0.0]


async def test_run_calibration_sweep_sensor_requires_bias_and_dark_count() -> None:
    with pytest.raises(ValueError, match="requires"):
        await server.run_calibration_sweep(
            "sensor", "test-rig", gains=[50], offsets=[10], flatExposureSecondsList=[1.0]
        )


async def test_run_calibration_sweep_sensor_rejects_flat_only_parameters() -> None:
    with pytest.raises(ValueError, match="doesn't accept"):
        await server.run_calibration_sweep(
            "sensor",
            "test-rig",
            gains=[50],
            offsets=[10],
            flatExposureSecondsList=[1.0],
            biasCount=3,
            darkCount=2,
            filterName="Luminance",
        )


async def test_run_calibration_sweep_flat_delegates_to_flat_calibration_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple] = []

    async def fake_start_sweep(
        rig_id: str,
        gains: list[float],
        offsets: list[float],
        exposure_seconds_list: list[float],
        filter_name: str,
        focus_position: int,
        count: int,
        *,
        location_id: str | None = None,
    ) -> dict:
        calls.append(
            (
                rig_id,
                gains,
                offsets,
                exposure_seconds_list,
                filter_name,
                focus_position,
                count,
                location_id,
            )
        )
        return {"kind": "flatCalibrationSweepStarted", "sweepId": "sweep-1"}

    monkeypatch.setattr(flat_calibration_sweep, "start_sweep", fake_start_sweep)

    result = await server.run_calibration_sweep(
        "flat",
        "test-rig",
        gains=[50, 100],
        offsets=[10, 20],
        exposureSecondsList=[1.0, 2.0],
        filterName="Luminance",
        focusPosition=5000,
        count=5,
        location_id="home-backyard",
    )

    assert result == {"kind": "flatCalibrationSweepStarted", "sweepId": "sweep-1"}
    # Positional args land in the right slots — focusPosition/count are both plain ints, so a
    # swapped argument order here would silently pass every type check.
    assert calls == [
        ("test-rig", [50, 100], [10, 20], [1.0, 2.0], "Luminance", 5000, 5, "home-backyard")
    ]


async def test_run_calibration_sweep_flat_requires_filter_focus_and_count() -> None:
    with pytest.raises(ValueError, match="requires"):
        await server.run_calibration_sweep(
            "flat", "test-rig", gains=[50], offsets=[10], exposureSecondsList=[1.0]
        )


async def test_run_calibration_sweep_flat_rejects_sensor_only_parameters() -> None:
    with pytest.raises(ValueError, match="doesn't accept"):
        await server.run_calibration_sweep(
            "flat",
            "test-rig",
            gains=[50],
            offsets=[10],
            exposureSecondsList=[1.0],
            filterName="Luminance",
            focusPosition=5000,
            count=5,
            biasCount=3,
        )


async def test_manage_calibration_sweep_status_tries_sensor_then_flat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sensor_calibration_sweep, "sweep_exists", lambda sweep_id: True)

    def fake_get_sweep_status(sweep_id: str) -> dict:
        return {"kind": "sensorCalibrationSweepProgress", "sweepId": sweep_id}

    monkeypatch.setattr(sensor_calibration_sweep, "get_sweep_status", fake_get_sweep_status)

    result = await server.manage_calibration_sweep("sweep-1", "status")

    assert result == {"kind": "sensorCalibrationSweepProgress", "sweepId": "sweep-1"}


async def test_manage_calibration_sweep_status_falls_back_to_flat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No sensor sweep with this id exists (sweep_exists genuinely False, unmocked), so
    # manage_calibration_sweep should fall through to the flat tracker.
    monkeypatch.setattr(flat_calibration_sweep, "sweep_exists", lambda sweep_id: True)

    def fake_get_sweep_status(sweep_id: str) -> dict:
        return {"kind": "flatCalibrationSweepProgress", "sweepId": sweep_id}

    monkeypatch.setattr(flat_calibration_sweep, "get_sweep_status", fake_get_sweep_status)

    result = await server.manage_calibration_sweep("sweep-1", "status")

    assert result == {"kind": "flatCalibrationSweepProgress", "sweepId": "sweep-1"}


async def test_manage_calibration_sweep_status_raises_if_neither_tracker_knows_it() -> None:
    with pytest.raises(ValueError, match="no calibration sweep found"):
        await server.manage_calibration_sweep("nonexistent-sweep", "status")


async def test_manage_calibration_sweep_cancel_tries_sensor_then_flat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sensor_calibration_sweep, "sweep_exists", lambda sweep_id: True)

    async def fake_cancel_sweep(sweep_id: str) -> dict:
        return {"kind": "sensorCalibrationSweepCancelled", "sweepId": sweep_id}

    monkeypatch.setattr(sensor_calibration_sweep, "cancel_sweep", fake_cancel_sweep)

    result = await server.manage_calibration_sweep("sweep-1", "cancel")

    assert result == {"kind": "sensorCalibrationSweepCancelled", "sweepId": "sweep-1"}


async def test_manage_calibration_sweep_cancel_falls_back_to_flat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No sensor sweep with this id exists (sweep_exists genuinely False, unmocked), so
    # manage_calibration_sweep should fall through to the flat tracker.
    monkeypatch.setattr(flat_calibration_sweep, "sweep_exists", lambda sweep_id: True)

    async def fake_cancel_sweep(sweep_id: str) -> dict:
        return {"kind": "flatCalibrationSweepCancelled", "sweepId": sweep_id}

    monkeypatch.setattr(flat_calibration_sweep, "cancel_sweep", fake_cancel_sweep)

    result = await server.manage_calibration_sweep("sweep-1", "cancel")

    assert result == {"kind": "flatCalibrationSweepCancelled", "sweepId": "sweep-1"}


_FRAME_METADATA: frame_store.FrameMetadata = {
    "frameId": "frame-1",
    "runId": "run-1",
    "device": "cam",
    "sizeBytes": 10,
    "checksumSha256": "0" * 64,
    "capturedAt": "2026-07-20T00:00:00.000000+00:00",
    "transferredAt": None,
}


async def test_list_frames_delegates_to_frame_store_with_all_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple] = []

    def fake_list_frames(
        *, run_id: str | None, device: str | None, since: str | None, transferred: bool | None
    ) -> list[frame_store.FrameMetadata]:
        calls.append((run_id, device, since, transferred))
        return [_FRAME_METADATA]

    monkeypatch.setattr(frame_store, "list_frames", fake_list_frames)

    result = await server.list_frames(
        run_id="run-1", device="cam", since="2026-07-19T00:00:00+00:00", transferred=False
    )

    assert result == [{**_FRAME_METADATA, "downloadUrl": None, "issues": []}]
    assert calls == [("run-1", "cam", "2026-07-19T00:00:00+00:00", False)]


async def test_get_frame_metadata_delegates_to_frame_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_get_frame_metadata(frame_id: str) -> frame_store.FrameMetadata:
        calls.append(frame_id)
        return _FRAME_METADATA

    monkeypatch.setattr(frame_store, "get_frame_metadata", fake_get_frame_metadata)

    result = await server.get_frame_metadata("frame-1")

    assert result == {**_FRAME_METADATA, "downloadUrl": None, "issues": []}
    assert calls == ["frame-1"]


async def test_get_frame_metadata_warns_when_checksum_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `None` `checksumSha256` (INDIMCP-95 predates the frame) isn't an error — the frame
    itself is fine — but a client should still be told it can't be integrity-checked."""
    legacy_frame = {**_FRAME_METADATA, "checksumSha256": None}
    monkeypatch.setattr(frame_store, "get_frame_metadata", lambda frame_id: legacy_frame)

    result = await server.get_frame_metadata("frame-1")

    assert result["issues"] == [
        {
            "kind": "issue",
            "severity": server.Severity.WARNING,
            "code": "frameChecksumMissing",
            "message": (
                "frame 'frame-1' has no checksumSha256 — it was captured before checksum "
                "support existed and cannot be integrity-checked"
            ),
            "role": None,
            "device": "cam",
        }
    ]


async def test_list_frames_does_not_warn_when_checksum_is_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(frame_store, "list_frames", lambda **_kwargs: [_FRAME_METADATA])

    result = await server.list_frames()

    assert result[0]["issues"] == []


async def test_list_frames_warns_per_frame_independently(monkeypatch: pytest.MonkeyPatch) -> None:
    """A directory spanning the INDIMCP-95 migration boundary has both kinds of frame at once —
    `issues` must be computed per frame, not decided once for the whole batch."""
    legacy_frame = {**_FRAME_METADATA, "frameId": "frame-legacy", "checksumSha256": None}
    monkeypatch.setattr(
        frame_store, "list_frames", lambda **_kwargs: [_FRAME_METADATA, legacy_frame]
    )

    result = await server.list_frames()

    assert result[0]["issues"] == []
    assert [issue["code"] for issue in result[1]["issues"]] == ["frameChecksumMissing"]


async def test_get_frame_metadata_includes_download_url_when_an_http_listener_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(frame_store, "get_frame_metadata", lambda frame_id: _FRAME_METADATA)
    monkeypatch.setattr(server, "_current_transport", "streamable-http")
    monkeypatch.setattr(server.socket, "gethostname", lambda: "indi-mcp-pi")
    monkeypatch.setattr(server.mcp.settings, "port", 8000)

    result = await server.get_frame_metadata("frame-1")

    assert result["downloadUrl"] == "http://indi-mcp-pi:8000/frames/frame-1"


def test_frame_download_url_is_none_without_an_http_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`stdio` has no HTTP server at all to point a download URL at -- same for the state
    before `run()` has ever set `_current_transport`."""
    monkeypatch.setattr(server, "_current_transport", "stdio")
    assert server._frame_download_url("frame-1") is None

    monkeypatch.setattr(server, "_current_transport", None)
    assert server._frame_download_url("frame-1") is None


def test_frame_download_url_uses_hostname_and_port_under_streamable_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server, "_current_transport", "streamable-http")
    monkeypatch.setattr(server.socket, "gethostname", lambda: "indi-mcp-pi")
    monkeypatch.setattr(server.mcp.settings, "port", 9000)

    assert server._frame_download_url("frame-1") == "http://indi-mcp-pi:9000/frames/frame-1"


def test_frame_download_url_percent_encodes_the_frame_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_current_transport", "streamable-http")
    monkeypatch.setattr(server.socket, "gethostname", lambda: "indi-mcp-pi")
    monkeypatch.setattr(server.mcp.settings, "port", 8000)

    url = server._frame_download_url("frame/with slash")

    assert url == "http://indi-mcp-pi:8000/frames/frame%2Fwith%20slash"


async def test_list_frames_includes_download_url_when_an_http_listener_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(frame_store, "list_frames", lambda **_kwargs: [_FRAME_METADATA])
    monkeypatch.setattr(server, "_current_transport", "streamable-http")
    monkeypatch.setattr(server.socket, "gethostname", lambda: "indi-mcp-pi")
    monkeypatch.setattr(server.mcp.settings, "port", 8000)

    result = await server.list_frames()

    assert result == [
        {**_FRAME_METADATA, "downloadUrl": "http://indi-mcp-pi:8000/frames/frame-1", "issues": []}
    ]


async def test_confirm_frame_transfer_delegates_to_frame_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_confirm_frame_transfer(frame_id: str) -> frame_store.FrameMetadata:
        calls.append(frame_id)
        return {**_FRAME_METADATA, "transferredAt": "2026-07-20T00:05:00.000000+00:00"}

    monkeypatch.setattr(frame_store, "confirm_frame_transfer", fake_confirm_frame_transfer)

    result = await server.confirm_frame_transfer("frame-1")

    assert result["transferredAt"] == "2026-07-20T00:05:00.000000+00:00"
    assert calls == ["frame-1"]


async def test_delete_frame_delegates_to_frame_store_with_the_require_transferred_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple] = []

    def fake_delete_frame(
        frame_id: str, *, require_transferred: bool = True
    ) -> frame_store.FrameMetadata:
        calls.append((frame_id, require_transferred))
        return _FRAME_METADATA

    monkeypatch.setattr(frame_store, "delete_frame", fake_delete_frame)

    result = await server.delete_frame("frame-1", require_transferred=False)

    assert result == _FRAME_METADATA
    assert calls == [("frame-1", False)]


async def test_purge_transferred_frames_delegates_to_frame_store_with_a_timedelta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[timedelta] = []

    def fake_purge_transferred_frames(*, older_than: timedelta) -> list[frame_store.FrameMetadata]:
        calls.append(older_than)
        return [_FRAME_METADATA]

    monkeypatch.setattr(frame_store, "purge_transferred_frames", fake_purge_transferred_frames)

    result = await server.purge_transferred_frames(older_than_days=7)

    assert result == [_FRAME_METADATA]
    assert calls == [timedelta(days=7)]


async def test_get_events_delegates_to_event_log_with_all_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple] = []
    record: event_log.EventRecord = {
        "id": 1,
        "stream": "messages",
        "device": "CCD Simulator",
        "runId": None,
        "target": None,
        "occurredAt": "2026-07-21T00:00:00.000000+00:00",
        "payload": {"kind": "message"},
    }

    def fake_get_events(
        stream: event_log.Stream,
        *,
        device: str | None,
        run_id: str | None,
        target: str | None,
        since: str | None,
        db_path: Path | None = None,
    ) -> list[event_log.EventRecord]:
        calls.append((stream, device, run_id, target, since))
        return [record]

    monkeypatch.setattr(event_log, "get_events", fake_get_events)

    result = await server.get_events(
        "messages", device="CCD Simulator", run_id=None, since="2026-07-20T00:00:00Z"
    )

    assert result == [record]
    assert calls == [("messages", "CCD Simulator", None, None, "2026-07-20T00:00:00Z")]


def _frame_download_request(frame_id: str) -> Request:
    """A minimal Starlette `Request` matching what routing would build for a real `GET
    /frames/{frameId}` request — enough for `download_frame` to read `path_params` off, without
    needing a full ASGI app/TestClient just to reach a handler that's plainly callable already
    (`custom_route` returns the function unchanged, same as every other directly-tested handler
    in this file, e.g. `_subscribe_to_event_stream`)."""
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": f"/frames/{frame_id}",
            "path_params": {"frameId": frame_id},
            "headers": [],
            "query_string": b"",
        }
    )


async def _send_asgi_response(response: Response) -> tuple[int, bytes]:
    """Drive `response` through its real ASGI `__call__`, returning `(status, body)`.

    Checking a `FileResponse`'s `.path`/`.media_type` attributes only confirms the right
    object was *constructed* -- it never exercises `FileResponse`'s own async stat/read path,
    so a real wiring break (wrong file, corrupted stream) wouldn't be caught. This drives the
    actual ASGI protocol instead, the same way a real HTTP server would, without needing a
    TestClient/httpx dependency just for one handler.
    """
    sent: list[MutableMapping[str, Any]] = []

    async def receive() -> MutableMapping[str, Any]:
        return {"type": "http.request"}

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    await response({"type": "http", "method": "GET", "headers": []}, receive, send)

    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m["body"] for m in sent if m["type"] == "http.response.body")
    return status, body


async def test_download_frame_streams_the_frames_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    frame_path = tmp_path / "frame-1.fits"
    frame_path.write_bytes(b"fits-bytes")
    calls: list[str] = []

    def fake_get_frame_path(frame_id: str) -> Path:
        calls.append(frame_id)
        return frame_path

    monkeypatch.setattr(frame_store, "get_frame_path", fake_get_frame_path)

    response = await server.download_frame(_frame_download_request("frame-1"))

    assert calls == ["frame-1"]
    assert isinstance(response, FileResponse)
    assert response.path == frame_path
    assert response.media_type == "application/octet-stream"

    status, body = await _send_asgi_response(response)
    assert status == 200
    assert body == b"fits-bytes"


async def test_download_frame_returns_404_for_an_unknown_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get_frame_path(frame_id: str) -> Path:
        raise frame_store.FrameNotFoundError(f"no frame found for frameId {frame_id!r}")

    monkeypatch.setattr(frame_store, "get_frame_path", fake_get_frame_path)

    response = await server.download_frame(_frame_download_request("does-not-exist"))

    assert response.status_code == 404

    status, body = await _send_asgi_response(response)
    assert status == 404
    assert body == b"Frame not found"


async def test_download_frame_route_is_registered() -> None:
    """`frame://{frameId}` (INDIMCP-11) was removed in favor of this plain HTTP route
    (INDIMCP-89) — confirms it's actually mounted on the MCP server's own Starlette app, not
    just that the bare `download_frame` function exists."""
    matching = [r for r in server.mcp._custom_starlette_routes if r.path == "/frames/{frameId}"]

    assert len(matching) == 1
    methods = matching[0].methods
    assert methods is not None
    assert "GET" in methods


async def test_frame_resource_no_longer_registered() -> None:
    templates = await server.mcp.list_resource_templates()

    assert all(t.uriTemplate != "frame://{frameId}" for t in templates)


class _FakeSession:
    """A minimal stand-in for `mcp.server.session.ServerSession`."""

    def __init__(self) -> None:
        self.updated: list[str] = []

    async def send_resource_updated(self, uri: AnyUrl) -> None:
        self.updated.append(str(uri))


async def test_indi_message_stream_resource_is_readable_through_the_real_mcp_protocol() -> None:
    event_streams.publish_message_event({"kind": "message", "device": "CCD Simulator"})

    contents = list(await server.mcp.read_resource("indi://messages"))

    assert len(contents) == 1
    assert contents[0].mime_type == "application/json"
    assert "CCD Simulator" in cast(str, contents[0].content)


async def test_indi_message_stream_resource_is_scoped_to_one_device() -> None:
    event_streams.publish_message_event({"kind": "message", "device": "CCD Simulator"})
    event_streams.publish_message_event({"kind": "message", "device": "Telescope Simulator"})

    contents = list(await server.mcp.read_resource("indi://messages/CCD Simulator"))
    content = cast(str, contents[0].content)

    assert "CCD Simulator" in content
    assert "Telescope Simulator" not in content


async def test_indi_message_stream_resource_is_reachable_for_a_device_name_with_a_slash() -> None:
    """A device name containing `/` used to add an extra path segment that the single-segment
    `indi://messages/{device}` resource template could never match, making that device's scoped
    stream permanently unreachable. `event_streams.messages_uri` now percent-encodes it."""
    event_streams.publish_message_event({"kind": "message", "device": "CCD/Sub"})

    contents = list(await server.mcp.read_resource(event_streams.messages_uri("CCD/Sub")))

    assert "CCD/Sub" in cast(str, contents[0].content)


async def test_script_event_stream_resource_is_readable_through_the_real_mcp_protocol() -> None:
    event_streams.publish_script_event({"kind": "scriptStarted", "runId": "run-1"})

    contents = list(await server.mcp.read_resource("indi://mcp-server/scripts"))

    assert "run-1" in cast(str, contents[0].content)


async def test_script_event_stream_resource_is_scoped_to_one_run() -> None:
    event_streams.publish_script_event({"kind": "scriptStarted", "runId": "run-1"})
    event_streams.publish_script_event({"kind": "scriptStarted", "runId": "run-2"})

    contents = list(await server.mcp.read_resource("indi://mcp-server/scripts/run-1"))
    content = cast(str, contents[0].content)

    assert "run-1" in content
    assert "run-2" not in content


async def test_connection_event_stream_resource_is_readable_through_the_real_mcp_protocol() -> None:
    event_streams.publish_connection_event(
        {"kind": "connectionMade", "target": "indiserver", "message": None, "timestamp": "t1"}
    )

    contents = list(await server.mcp.read_resource("indi://mcp-server/connection"))

    assert "indiserver" in cast(str, contents[0].content)


async def test_connection_event_stream_resource_is_scoped_to_one_target() -> None:
    event_streams.publish_connection_event(
        {"kind": "connectionMade", "target": "indiserver", "message": None, "timestamp": "t1"}
    )
    event_streams.publish_connection_event(
        {"kind": "connectionMade", "target": "server", "message": None, "timestamp": "t2"}
    )

    contents = list(await server.mcp.read_resource("indi://mcp-server/connection/indiserver"))
    events = json.loads(cast(str, contents[0].content))["events"]

    assert [e["target"] for e in events] == ["indiserver"]


async def test_event_stream_resources_are_registered() -> None:
    resources = await server.mcp.list_resources()
    templates = await server.mcp.list_resource_templates()

    static_uris = {str(r.uri) for r in resources}
    template_uris = {t.uriTemplate for t in templates}
    assert "indi://messages" in static_uris
    assert "indi://mcp-server/scripts" in static_uris
    assert "indi://mcp-server/connection" in static_uris
    assert "indi://messages/{device}" in template_uris
    assert "indi://mcp-server/scripts/{runId}" in template_uris
    assert "indi://mcp-server/connection/{target}" in template_uris


async def test_resource_subscription_capability_is_advertised() -> None:
    """The installed MCP SDK hardcodes `subscribe=False`; `server.py` patches this after
    construction so real MCP clients know they can `resources/subscribe` to the event streams."""
    capabilities = server.mcp._mcp_server.get_capabilities(NotificationOptions(), {})

    assert capabilities.resources is not None
    assert capabilities.resources.subscribe is True


async def test_subscribe_and_unsubscribe_resource_handlers_use_event_streams() -> None:
    session = _FakeSession()
    context: RequestContext = RequestContext(
        request_id=1, meta=None, session=cast(Any, session), lifespan_context=None
    )
    token = request_ctx.set(context)
    try:
        await server._subscribe_to_event_stream(AnyUrl("indi://messages"))
        assert session in event_streams._subscribers["indi://messages"]

        event_streams.publish_message_event({"kind": "message", "device": None})
        await asyncio.sleep(0)
        assert session.updated == ["indi://messages"]

        await server._unsubscribe_from_event_stream(AnyUrl("indi://messages"))
        assert "indi://messages" not in event_streams._subscribers
    finally:
        request_ctx.reset(token)


async def test_lifespan_starts_and_cleanly_cancels_the_purge_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_lifespan` is the one place a background task tied to the real running event loop can be
    started (see its docstring) — this exercises the actual context manager FastMCP invokes, not
    just `event_log.run_purge_loop` in isolation."""
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def fake_run_purge_loop() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(event_log, "run_purge_loop", fake_run_purge_loop)

    async with server._lifespan(server.mcp):
        await asyncio.wait_for(started.wait(), timeout=1)

    await asyncio.wait_for(cancelled.wait(), timeout=1)


async def test_lifespan_drains_pending_event_streams_tasks_before_exiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A durable event-log write still in flight when the server shuts down must be waited
    for, not abandoned mid-write — otherwise a reconnecting client could find the event log
    missing exactly the event it most needs to catch up on."""

    async def fake_run_purge_loop() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr(event_log, "run_purge_loop", fake_run_purge_loop)

    finished = threading.Event()

    def slow_record_event(*args, **kwargs) -> None:
        time.sleep(0.02)
        finished.set()

    monkeypatch.setattr(event_log, "record_event", slow_record_event)

    async with server._lifespan(server.mcp):
        event_streams.publish_message_event({"kind": "message", "device": None})

    assert finished.is_set()


async def test_subscribe_resource_handler_rejects_a_uri_that_is_not_an_event_stream() -> None:
    """A typo'd or unrelated URI (e.g. `frame://foo`) must not silently register a subscription
    that will never fire — the client should find out immediately instead."""
    session = _FakeSession()
    context: RequestContext = RequestContext(
        request_id=1, meta=None, session=cast(Any, session), lifespan_context=None
    )
    token = request_ctx.set(context)
    try:
        with pytest.raises(McpError):
            await server._subscribe_to_event_stream(AnyUrl("frame://foo"))
        assert event_streams._subscribers == {}
    finally:
        request_ctx.reset(token)


async def test_unsubscribe_resource_handler_rejects_a_uri_that_is_not_an_event_stream() -> None:
    session = _FakeSession()
    context: RequestContext = RequestContext(
        request_id=1, meta=None, session=cast(Any, session), lifespan_context=None
    )
    token = request_ctx.set(context)
    try:
        with pytest.raises(McpError):
            await server._unsubscribe_from_event_stream(AnyUrl("indi://message"))
    finally:
        request_ctx.reset(token)


def test_run_disables_rebinding_protection_for_a_non_loopback_host(
    monkeypatch: pytest.MonkeyPatch, _restore_transport_security: None
) -> None:
    monkeypatch.setattr(server.mcp, "run", lambda **kwargs: None)
    monkeypatch.setattr(rig_store, "load_rigs", lambda: None)
    monkeypatch.setattr(observatory_store, "load_observatories", lambda: None)
    monkeypatch.setattr(script_store, "load_scripts", lambda: None)

    server.run(transport="streamable-http", host="0.0.0.0", port=8000)

    assert server.mcp.settings.transport_security is not None
    assert server.mcp.settings.transport_security.enable_dns_rebinding_protection is False


def test_run_keeps_rebinding_protection_for_the_default_loopback_host(
    monkeypatch: pytest.MonkeyPatch, _restore_transport_security: None
) -> None:
    monkeypatch.setattr(server.mcp, "run", lambda **kwargs: None)
    monkeypatch.setattr(rig_store, "load_rigs", lambda: None)
    monkeypatch.setattr(observatory_store, "load_observatories", lambda: None)
    monkeypatch.setattr(script_store, "load_scripts", lambda: None)

    server.run(transport="streamable-http", host="127.0.0.1", port=8000)

    assert server.mcp.settings.transport_security is not None
    assert server.mcp.settings.transport_security.enable_dns_rebinding_protection is True
