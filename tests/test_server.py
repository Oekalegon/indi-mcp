import asyncio
import base64
import inspect
import threading
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from mcp.server.lowlevel.server import NotificationOptions, request_ctx
from mcp.shared.context import RequestContext
from mcp.shared.exceptions import McpError
from pydantic import AnyUrl

from indi_mcp import (
    event_log,
    event_streams,
    frame_store,
    indi_driver,
    indi_messaging,
    observatory_store,
    plate_solver,
    rig_store,
    script_engine,
    script_runs,
    script_store,
    server,
)

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


@pytest.fixture(autouse=True)
def _reset_event_streams() -> None:
    event_streams._messages.clear()
    event_streams._scripts.clear()
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

    result = await server.draft_rig()

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


async def test_save_rig_delegates_to_rig_store_with_the_overwrite_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = rig_store.Rig(id="minimal", name="Minimal rig", components=[])
    calls: list[tuple[rig_store.Rig, bool]] = []

    def fake_save_rig(rig: rig_store.Rig, *, overwrite: bool = False) -> rig_store.Rig:
        calls.append((rig, overwrite))
        return rig

    monkeypatch.setattr(rig_store, "save_rig", fake_save_rig)

    result = await server.save_rig(rig, overwrite=True)

    assert result == rig
    assert calls == [(rig, True)]


async def test_sync_filter_names_delegates_to_script_engine_with_resolved_device(
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

    result = await server.sync_filter_names("test-rig", "filterWheel")

    assert calls == [("filterWheel", "Filter Wheel Simulator", {1: "Luminance", 2: "Red"})]
    assert result == {
        "status": "matched",
        "rigSlots": {1: "Luminance", 2: "Red"},
        "liveSlots": {1: "Luminance", 2: "Red"},
    }


async def test_sync_filter_names_raises_when_role_has_no_connected_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = rig_store.Rig(id="test-rig", name="Test rig", components=[])
    monkeypatch.setattr(rig_store, "get_rig", lambda rig_id: rig)

    with pytest.raises(ValueError, match="expected exactly one"):
        await server.sync_filter_names("test-rig", "filterWheel")


async def test_sync_filter_names_raises_when_role_matches_more_than_one_component(
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
        await server.sync_filter_names("test-rig", "filterWheel")


async def test_adopt_filter_names_from_driver_delegates_to_script_engine_with_resolved_device(
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

    result = await server.adopt_filter_names_from_driver("test-rig", "filterWheel")

    assert calls == [
        ("test-rig", "filterWheel", "Filter Wheel Simulator", {1: "Luminance", 2: "Red"})
    ]
    assert result == {
        "status": "matched",
        "rigSlots": {1: "Luminance", 2: "Red"},
        "liveSlots": {1: "Luminance", 2: "Red"},
    }


async def test_adopt_filter_names_from_driver_raises_when_role_has_no_connected_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = rig_store.Rig(id="test-rig", name="Test rig", components=[])
    monkeypatch.setattr(rig_store, "get_rig", lambda rig_id: rig)

    with pytest.raises(ValueError, match="expected exactly one"):
        await server.adopt_filter_names_from_driver("test-rig", "filterWheel")


async def test_adopt_filter_names_from_driver_raises_when_role_matches_more_than_one_component(
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
        await server.adopt_filter_names_from_driver("test-rig", "filterWheel")


async def test_save_observatory_delegates_to_observatory_store_with_the_overwrite_flag(
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

    result = await server.save_observatory(observatory, overwrite=True)

    assert result == observatory
    assert calls == [(observatory, True)]


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


async def test_park_delegates_to_start_script(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_start_script(monkeypatch)

    result = await server.park("test-rig")

    assert result == {"kind": "scriptStarted", "runId": "abc"}
    assert calls == [("park", "test-rig", {}, None)]


async def test_unpark_delegates_to_start_script(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_start_script(monkeypatch)

    await server.unpark("test-rig")

    assert calls == [("unpark", "test-rig", {}, None)]


async def test_slew_delegates_to_start_script(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_start_script(monkeypatch)

    await server.slew("test-rig", ra=10.5, dec=41.2)

    assert calls == [("slew", "test-rig", {"ra": 10.5, "dec": 41.2}, None)]


async def test_cool_camera_delegates_to_start_script_with_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_start_script(monkeypatch)

    await server.cool_camera("test-rig")

    assert calls == [("cool_camera", "test-rig", {"targetTempC": -10, "timeoutSeconds": 300}, None)]


async def test_select_filter_delegates_to_start_script(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_start_script(monkeypatch)

    await server.select_filter("test-rig", filterName="Ha")

    assert calls == [("select_filter", "test-rig", {"filterName": "Ha"}, None)]


async def test_set_focus_position_delegates_to_start_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_start_script(monkeypatch)

    await server.set_focus_position("test-rig", position=15000)

    assert calls == [("set_focus_position", "test-rig", {"position": 15000}, None)]


async def test_connect_delegates_to_start_script(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_start_script(monkeypatch)

    await server.connect("test-rig", role="mount")

    assert calls == [("connect", "test-rig", {"role": "mount"}, None)]


async def test_disconnect_delegates_to_start_script(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_start_script(monkeypatch)

    await server.disconnect("test-rig", role="mount")

    assert calls == [("disconnect", "test-rig", {"role": "mount"}, None)]


async def test_capture_frame_delegates_to_start_script_with_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_start_script(monkeypatch)

    await server.capture_frame("test-rig", exposureSeconds=300)

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


async def test_capture_frame_passes_location_id_through_to_start_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_start_script(monkeypatch)

    await server.capture_frame("test-rig", exposureSeconds=300, location_id="home-backyard")

    assert calls[0][3] == "home-backyard"


async def test_track_off_delegates_to_start_script(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_start_script(monkeypatch)

    await server.track_off("test-rig")

    assert calls == [("track_off", "test-rig", {}, None)]


async def test_set_track_mode_delegates_to_start_script(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_start_script(monkeypatch)

    await server.set_track_mode("test-rig", modeSwitchElement="TRACK_SIDEREAL")

    assert calls == [("set_track_mode", "test-rig", {"modeSwitchElement": "TRACK_SIDEREAL"}, None)]


async def test_set_custom_tracking_rate_delegates_to_start_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_start_script(monkeypatch)

    await server.set_custom_tracking_rate(
        "test-rig", raRateArcsecPerSec=15.0, decRateArcsecPerSec=-0.5
    )

    assert calls == [
        (
            "set_custom_tracking_rate",
            "test-rig",
            {"raRateArcsecPerSec": 15.0, "decRateArcsecPerSec": -0.5},
            None,
        )
    ]


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
    "park": server.park,
    "unpark": server.unpark,
    "slew": server.slew,
    "cool_camera": server.cool_camera,
    "select_filter": server.select_filter,
    "set_focus_position": server.set_focus_position,
    "connect": server.connect,
    "disconnect": server.disconnect,
    "capture_frame": server.capture_frame,
    "track_off": server.track_off,
    "set_track_mode": server.set_track_mode,
    "set_custom_tracking_rate": server.set_custom_tracking_rate,
    "plate_solve": server.plate_solve,
}


@pytest.mark.parametrize("script_id", sorted(_WRAPPER_TOOLS_BY_SCRIPT_ID))
def test_wrapper_tool_signature_matches_the_scripts_own_parameters(script_id: str) -> None:
    """Each convenience wrapper (INDIMCP-49) hand-encodes its script's own `parameters:`
    block as Python parameter names/required-ness/defaults — nothing else keeps the two in
    sync, so this cross-checks the wrapper's actual signature against the loaded script's
    declared `Parameter`s directly, rather than against a third hardcoded expectations list,
    turning a future YAML/Python desync into a fast, specific test failure instead of a
    confusing `scriptFailed` at call time."""
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


def test_get_script_status_delegates_to_script_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_get_script_status(run_id: str) -> dict:
        calls.append(run_id)
        return {"kind": "scriptProgress", "runId": run_id}

    monkeypatch.setattr(script_runs, "get_script_status", fake_get_script_status)

    result = server.get_script_status("abc")

    assert result == {"kind": "scriptProgress", "runId": "abc"}
    assert calls == ["abc"]


async def test_cancel_script_delegates_to_script_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_cancel_script(run_id: str) -> dict:
        calls.append(run_id)
        return {"kind": "scriptCancelled", "runId": run_id}

    monkeypatch.setattr(script_runs, "cancel_script", fake_cancel_script)

    result = await server.cancel_script("abc")

    assert result == {"kind": "scriptCancelled", "runId": "abc"}
    assert calls == ["abc"]


def test_pause_script_delegates_to_script_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_pause_script(run_id: str) -> dict:
        calls.append(run_id)
        return {"kind": "scriptPaused", "runId": run_id}

    monkeypatch.setattr(script_runs, "pause_script", fake_pause_script)

    result = server.pause_script("abc")

    assert result == {"kind": "scriptPaused", "runId": "abc"}
    assert calls == ["abc"]


def test_resume_script_delegates_to_script_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_resume_script(run_id: str) -> dict:
        calls.append(run_id)
        return {"kind": "scriptResumed", "runId": run_id}

    monkeypatch.setattr(script_runs, "resume_script", fake_resume_script)

    result = server.resume_script("abc")

    assert result == {"kind": "scriptResumed", "runId": "abc"}
    assert calls == ["abc"]


_FRAME_METADATA: frame_store.FrameMetadata = {
    "frameId": "frame-1",
    "runId": "run-1",
    "device": "cam",
    "sizeBytes": 10,
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

    assert result == [_FRAME_METADATA]
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

    assert result == _FRAME_METADATA
    assert calls == ["frame-1"]


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
        "occurredAt": "2026-07-21T00:00:00.000000+00:00",
        "payload": {"kind": "message"},
    }

    def fake_get_events(
        stream: event_log.Stream,
        *,
        device: str | None,
        run_id: str | None,
        since: str | None,
        db_path: Path | None = None,
    ) -> list[event_log.EventRecord]:
        calls.append((stream, device, run_id, since))
        return [record]

    monkeypatch.setattr(event_log, "get_events", fake_get_events)

    result = await server.get_events(
        "messages", device="CCD Simulator", run_id=None, since="2026-07-20T00:00:00Z"
    )

    assert result == [record]
    assert calls == [("messages", "CCD Simulator", None, "2026-07-20T00:00:00Z")]


async def test_read_frame_returns_the_frames_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    frame_path = tmp_path / "frame-1.fits"
    frame_path.write_bytes(b"fits-bytes")
    calls: list[str] = []

    def fake_get_frame_path(frame_id: str) -> Path:
        calls.append(frame_id)
        return frame_path

    monkeypatch.setattr(frame_store, "get_frame_path", fake_get_frame_path)

    result = await server.read_frame("frame-1")

    assert result == b"fits-bytes"
    assert calls == ["frame-1"]


async def test_read_frame_propagates_frame_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get_frame_path(frame_id: str) -> Path:
        raise frame_store.FrameNotFoundError(f"no frame found for frameId {frame_id!r}")

    monkeypatch.setattr(frame_store, "get_frame_path", fake_get_frame_path)

    with pytest.raises(frame_store.FrameNotFoundError):
        await server.read_frame("does-not-exist")


async def test_frame_resource_is_readable_through_the_real_mcp_protocol(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Exercises the actual `frame://{frameId}` URI-template registration and binary content
    handling via `mcp.read_resource`, not just the bare `read_frame` function body — this is
    what catches a broken `{frameId}`/`frameId` name match or an accidental non-`bytes` return
    that a direct call to `server.read_frame(...)` wouldn't."""
    frame_path = tmp_path / "frame-1.fits"
    frame_path.write_bytes(b"fits-bytes")

    def fake_get_frame_path(frame_id: str) -> Path:
        assert frame_id == "frame-1"
        return frame_path

    monkeypatch.setattr(frame_store, "get_frame_path", fake_get_frame_path)

    contents = list(await server.mcp.read_resource("frame://frame-1"))

    assert len(contents) == 1
    assert contents[0].content == b"fits-bytes"
    assert contents[0].mime_type == "application/octet-stream"


async def test_frame_resource_uri_template_is_registered() -> None:
    templates = await server.mcp.list_resource_templates()

    matching = [t for t in templates if t.uriTemplate == "frame://{frameId}"]
    assert len(matching) == 1
    assert matching[0].mimeType == "application/octet-stream"


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

    contents = list(await server.mcp.read_resource("indi://scripts"))

    assert "run-1" in cast(str, contents[0].content)


async def test_script_event_stream_resource_is_scoped_to_one_run() -> None:
    event_streams.publish_script_event({"kind": "scriptStarted", "runId": "run-1"})
    event_streams.publish_script_event({"kind": "scriptStarted", "runId": "run-2"})

    contents = list(await server.mcp.read_resource("indi://scripts/run-1"))
    content = cast(str, contents[0].content)

    assert "run-1" in content
    assert "run-2" not in content


async def test_event_stream_resources_are_registered() -> None:
    resources = await server.mcp.list_resources()
    templates = await server.mcp.list_resource_templates()

    static_uris = {str(r.uri) for r in resources}
    template_uris = {t.uriTemplate for t in templates}
    assert "indi://messages" in static_uris
    assert "indi://scripts" in static_uris
    assert "indi://messages/{device}" in template_uris
    assert "indi://scripts/{runId}" in template_uris


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
