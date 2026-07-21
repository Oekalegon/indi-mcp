import asyncio
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
    rig_store,
    script_runs,
    server,
)


@pytest.fixture(autouse=True)
def _reset_event_streams() -> None:
    event_streams._messages.clear()
    event_streams._scripts.clear()
    event_streams._subscribers.clear()
    event_streams._background_tasks.clear()


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
    calls: list[tuple[str, str, dict]] = []

    async def fake_start_script(script_id: str, rig_id: str, parameters: dict) -> dict:
        calls.append((script_id, rig_id, parameters))
        return {"kind": "scriptStarted", "runId": "abc"}

    monkeypatch.setattr(script_runs, "start_script", fake_start_script)

    result = await server.run_script("capture_sequence", "test-rig", {"count": 10})

    assert result == {"kind": "scriptStarted", "runId": "abc"}
    assert calls == [("capture_sequence", "test-rig", {"count": 10})]


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
