"""The INDI MCP server instance and its entrypoint."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, Literal, cast
from urllib.parse import unquote

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import McpError
from mcp.types import INVALID_PARAMS, ErrorData
from pydantic import AnyUrl

from indi_mcp import (
    event_log,
    event_streams,
    frame_store,
    indi_driver,
    indi_messaging,
    indi_server,
    observatory_store,
    rig_store,
    script_runs,
    script_store,
)
from indi_mcp.frame_store import FrameMetadata
from indi_mcp.indi_driver import DriverInfo, DriverStatus
from indi_mcp.indi_messaging import IndiEvent, MessagingStatus
from indi_mcp.indi_server import INDI_PORT, IndiServerStatus
from indi_mcp.observatory_store import Observatory, ObservatorySummary
from indi_mcp.rig_store import DraftDeviceInfo, Rig, RigCheck, RigDraft, RigSuggestion, RigSummary
from indi_mcp.script_runs import (
    ScriptRunPaused,
    ScriptRunPauseRejected,
    ScriptRunResumed,
    ScriptRunStarted,
    ScriptRunStatus,
)
from indi_mcp.script_store import Script, ScriptSummary

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastMCP) -> AsyncIterator[None]:
    """Run the event log's periodic purge for as long as the server does.

    FastMCP invokes this once per process regardless of transport (stdio,
    sse, streamable-http) — the one place to start a background task tied
    to the actual running event loop, rather than trying to from the
    synchronous `run()` function below. Cancelled and awaited on shutdown
    so the task doesn't outlive the server. See `event_log.run_purge_loop`.

    Also drains `event_streams`'s in-flight background tasks (live
    notifications and durable event-log writes) before returning — without
    this, an event published right before shutdown could have its durable
    write abandoned mid-flight, silently losing exactly what a reconnecting
    client depends on the event log to still have. See `event_streams.drain`.
    """
    purge_task = asyncio.create_task(event_log.run_purge_loop())
    try:
        yield
    finally:
        purge_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await purge_task
        await event_streams.drain()


mcp = FastMCP(
    name="indi-mcp",
    instructions=(
        "Controls astrophotography equipment via INDI: manage the INDI server "
        "and its drivers, send and receive INDI messages, and run capture scripts."
    ),
    lifespan=_lifespan,
)

Transport = Literal["stdio", "sse", "streamable-http"]


_original_get_capabilities = mcp._mcp_server.get_capabilities


def _get_capabilities_with_resource_subscriptions(*args: Any, **kwargs: Any) -> Any:
    """Advertise resource subscription support for `indi://messages`/`indi://scripts`.

    The installed MCP SDK hardcodes `ResourcesCapability(subscribe=False,
    ...)` in `Server.get_capabilities` regardless of whether a
    `subscribe_resource`/`unsubscribe_resource` handler is registered (see
    `mcp.server.lowlevel.server.Server.get_capabilities`) — there's no
    documented way to opt into `subscribe=True` short of overriding this
    method, so it's patched here after construction, once, rather than
    every server instance silently under-advertising a capability it
    actually supports (see the `subscribe_resource`/`unsubscribe_resource`
    handlers below, and `docs/Design.md#event-streams`).
    """
    capabilities = _original_get_capabilities(*args, **kwargs)
    if capabilities.resources is not None:
        capabilities.resources.subscribe = True
    return capabilities


# `get_capabilities` is a bound method with a fixed signature on the `Server` class; assigning a
# replacement is inherently a type hole a static checker can't see through, so it's routed
# through an `Any`-typed reference rather than pretending otherwise.
cast(Any, mcp._mcp_server).get_capabilities = _get_capabilities_with_resource_subscriptions


def _require_subscribable_uri(uri: AnyUrl) -> str:
    """Return `uri` as a string, rejecting anything that isn't a real event-stream resource.

    Without this, `resources/subscribe` for a typo'd URI (`indi://message`) or an unrelated
    resource (`frame://foo`) would silently "succeed" — `event_streams` would register the
    subscription but never publish to it, so the client would just never get a notification
    with no indication anything was wrong. Raising here instead gives a buggy client immediate,
    actionable feedback.
    """
    uri_str = str(uri)
    if not event_streams.is_subscribable_uri(uri_str):
        raise McpError(
            ErrorData(
                code=INVALID_PARAMS,
                message=(
                    f"{uri_str!r} is not a subscribable resource; expected "
                    "indi://messages(/{device}) or indi://scripts(/{runId})"
                ),
            )
        )
    return uri_str


@mcp._mcp_server.subscribe_resource()
async def _subscribe_to_event_stream(uri: AnyUrl) -> None:
    """Handle `resources/subscribe` for `indi://messages`/`indi://scripts` (and their scoped forms).

    FastMCP itself has no subscription mechanism, so this is registered
    directly on the underlying low-level `Server` rather than via
    `@mcp.resource(...)`. The current request's session comes from the
    low-level server's own request context, which FastMCP shares — it's set
    for every request regardless of which layer dispatched it.
    """
    session = mcp._mcp_server.request_context.session
    event_streams.subscribe(_require_subscribable_uri(uri), session)


@mcp._mcp_server.unsubscribe_resource()
async def _unsubscribe_from_event_stream(uri: AnyUrl) -> None:
    """Handle `resources/unsubscribe`, undoing a prior `_subscribe_to_event_stream` call."""
    session = mcp._mcp_server.request_context.session
    event_streams.unsubscribe(_require_subscribable_uri(uri), session)


@mcp.tool()
async def start_indi_server(port: int = INDI_PORT) -> IndiServerStatus:
    """Start the INDI server (`indiserver`) on the given port.

    Restarts it if it is already running.
    """
    return await indi_server.start_server(port)


@mcp.tool()
async def stop_indi_server() -> IndiServerStatus:
    """Stop the running INDI server (`indiserver`)."""
    return await indi_server.stop_server()


@mcp.tool()
async def restart_indi_server(port: int | None = None) -> IndiServerStatus:
    """Restart the INDI server (`indiserver`), optionally switching to a new port."""
    return await indi_server.restart_server(port)


@mcp.tool()
async def get_indi_server_status() -> IndiServerStatus:
    """Report whether the INDI server (`indiserver`) is running, and on which port."""
    return await indi_server.get_status()


@mcp.tool()
async def list_indi_driver_catalog() -> list[DriverInfo]:
    """List every INDI driver installed on this device, whether or not it is running."""
    return await indi_driver.get_driver_catalog()


@mcp.tool()
async def start_indi_driver(label: str) -> DriverStatus:
    """Start the INDI driver identified by its catalog label (e.g. "CCD Simulator")."""
    return await indi_driver.start_driver(label)


@mcp.tool()
async def stop_indi_driver(label: str) -> DriverStatus:
    """Stop the running INDI driver identified by its catalog label."""
    return await indi_driver.stop_driver(label)


@mcp.tool()
async def list_running_indi_drivers() -> list[DriverStatus]:
    """List all currently running INDI drivers."""
    return await indi_driver.list_running_drivers()


@mcp.tool()
async def start_indi_messaging(host: str = "localhost", port: int = INDI_PORT) -> MessagingStatus:
    """Connect to the INDI server and start streaming its property/message events."""
    return await indi_messaging.start_messaging(host, port)


@mcp.tool()
async def stop_indi_messaging() -> MessagingStatus:
    """Disconnect from the INDI server and stop streaming its events."""
    return await indi_messaging.stop_messaging()


@mcp.tool()
async def get_indi_messaging_status() -> MessagingStatus:
    """Report whether the INDI messaging stream is running, and its host/port."""
    return await indi_messaging.get_status()


@mcp.tool()
async def list_indi_messages(device: str | None = None, limit: int = 50) -> list[IndiEvent]:
    """List the most recently seen INDI events, newest first, optionally filtered to one device."""
    return indi_messaging.list_messages(device, limit)


@mcp.tool()
async def send_indi_property(device: str, name: str, elements: dict[str, str]) -> IndiEvent:
    """Send a command to an INDI device, setting `elements` on its property `name`."""
    return await indi_messaging.send_property(device, name, elements)


@mcp.resource("indi://messages", mime_type="application/json")
def read_indi_message_stream() -> dict[str, list[IndiEvent]]:
    """The rolling window of recent INDI messaging-layer events, newest first.

    Subscribable via `resources/subscribe`, per `docs/Design.md#event-
    streams`: a subscriber is sent `notifications/resources/updated`
    whenever a new event is published, and re-reads this resource to fetch
    it. This is a best-effort, live-only channel — a client that was
    disconnected should use `list_indi_messages` or `get_events` (the
    durable event log, INDIMCP-15) to catch up, not assume it saw everything.
    """
    return cast(dict[str, list[IndiEvent]], event_streams.read_messages())


@mcp.resource("indi://messages/{device}", mime_type="application/json")
def read_indi_message_stream_for_device(device: str) -> dict[str, list[IndiEvent]]:
    """Same as `read_indi_message_stream`, scoped to events from one `device`.

    `device` arrives as the raw, still-percent-encoded path segment — FastMCP
    matches resource templates against the literal URI text and doesn't
    decode it — so it's unquoted here to recover the real device name before
    filtering, mirroring the encoding `event_streams.messages_uri` applies
    when building this same URI for subscription/notification purposes.
    """
    return cast(dict[str, list[IndiEvent]], event_streams.read_messages(unquote(device)))


@mcp.tool()
def list_rigs() -> list[RigSummary]:
    """List the id/name of every configured imaging rig (see `docs/RigSchema.md`)."""
    return rig_store.list_rigs()


@mcp.tool()
def get_rig(rig_id: str) -> Rig:
    """Return the full definition of the imaging rig identified by `rig_id`."""
    return rig_store.get_rig(rig_id)


@mcp.tool()
async def save_rig(rig: Rig, overwrite: bool = False) -> Rig:
    """Save a rig definition — hand-authored, or completed from a `draft_rig` result.

    Writes `rig` to `rigs/<rig.id>.yaml` and reloads it so it's immediately
    available by `id` to `get_rig`/`suggest_rig`/`check_rig`. Refuses to
    replace an existing rig file unless `overwrite` is set, since reusing an
    `id` could otherwise silently destroy a previously saved rig. The actual
    file I/O runs in a worker thread so it doesn't block the event loop.
    """
    return await asyncio.to_thread(rig_store.save_rig, rig, overwrite=overwrite)


@mcp.tool()
def suggest_rig() -> list[RigSuggestion]:
    """Propose which configured rig is likely mounted, by matching connected INDI devices.

    Never auto-selects a rig; candidates are sorted best match first for the
    operator or client to choose from.
    """
    return rig_store.suggest_rig(indi_messaging.list_devices())


@mcp.tool()
def check_rig(rig_id: str) -> RigCheck:
    """Warn on any of the given rig's devices that aren't currently connected.

    This is a warning, not a hard failure: a rig might be intentionally
    used without one of its devices (e.g. imaging without a guide camera).
    """
    return rig_store.check_rig(rig_id, indi_messaging.list_devices())


@mcp.tool()
async def draft_rig() -> RigDraft:
    """Pre-fill a draft rig skeleton from currently connected INDI devices.

    Combines each device's driver family (camera/filter wheel/focuser/mount)
    with whatever live properties it exposes (CCD_INFO, FILTER_NAME, focuser
    range) into a starting point. Never auto-finalizes a rig: fields INDI
    can't supply and any ambiguous role assignments are left for the
    operator to complete and save themselves.
    """
    devices: list[DraftDeviceInfo] = []
    for name in indi_messaging.list_devices():
        family = await indi_driver.classify_device(name)
        devices.append(
            {
                "name": name,
                "family": family,
                "ccdInfo": (
                    indi_messaging.get_property_values(name, "CCD_INFO")
                    if family == "CCDs"
                    else None
                ),
                "filterNames": (
                    indi_messaging.get_property_values(name, "FILTER_NAME")
                    if family == "Filter Wheels"
                    else None
                ),
                "focusRange": (
                    indi_messaging.get_property_range(
                        name, "ABS_FOCUS_POSITION", "FOCUS_ABSOLUTE_POSITION"
                    )
                    if family == "Focusers"
                    else None
                ),
            }
        )
    return rig_store.draft_rig(devices)


@mcp.tool()
def list_observatories() -> list[ObservatorySummary]:
    """List the id/name of every configured observatory location (see `ObservatorySchema.md`)."""
    return observatory_store.list_observatories()


@mcp.tool()
def get_observatory(observatory_id: str) -> Observatory:
    """Return the full definition of the observatory location identified by `observatory_id`."""
    return observatory_store.get_observatory(observatory_id)


@mcp.tool()
async def save_observatory(observatory: Observatory, overwrite: bool = False) -> Observatory:
    """Save an observatory location definition.

    Writes `observatory` to `observatories/<observatory.id>.yaml` and reloads
    it so it's immediately available by `id` to `get_observatory`. Refuses to
    replace an existing file unless `overwrite` is set, since reusing an `id`
    could otherwise silently destroy a previously saved location. The actual
    file I/O runs in a worker thread so it doesn't block the event loop.
    """
    return await asyncio.to_thread(
        observatory_store.save_observatory, observatory, overwrite=overwrite
    )


@mcp.tool()
def list_scripts() -> list[ScriptSummary]:
    """List the id/name/description of every loaded script (see `docs/ScriptSchema.md`)."""
    return script_store.list_scripts()


@mcp.tool()
def get_script(script_id: str) -> Script:
    """Return the full definition of the script identified by `script_id`."""
    return script_store.get_script(script_id)


@mcp.tool()
async def save_script(script: Script, overwrite: bool = False) -> Script:
    """Upload and save a script written on the Client Computer.

    Writes `script` to `user_scripts/<script.id>.yaml` — a separate
    directory from the built-in scripts shipped in `scripts/`, so an
    upload can never be clobbered by a redeploy of the built-in checkout,
    or silently shadow a built-in script's id — and reloads the merged
    library so it's immediately available by `id` to `get_script` and to
    `run_script`. Only ever validates and stores declarative step data
    (`yaml.safe_load`, no executable code), per the safety approach in
    `docs/Design.md`. Rejected outright, before anything is written, if
    `script` doesn't fit the rest of the library — an unresolved
    `run_script` reference, a mismatched argument type, a call cycle, or an
    id already used by a built-in script. Refuses to replace an existing
    uploaded script file unless `overwrite` is set, since reusing an `id`
    could otherwise silently destroy a previously saved script. The actual
    file I/O runs in a worker thread so it doesn't block the event loop.
    """
    return await asyncio.to_thread(script_store.save_script, script, overwrite=overwrite)


@mcp.tool()
async def run_script(
    script_id: str,
    rig_id: str,
    parameters: dict[str, Any] | None = None,
    location_id: str | None = None,
) -> ScriptRunStarted:
    """Start `script_id` running against `rig_id`, returning immediately with a `runId`.

    Scripts run long sequences against physical hardware and are meant to
    keep going even if the caller disconnects, so this never blocks until
    the script finishes (see `docs/Design.md#calling-scripts-and-script-
    results`) — poll `get_script_status(runId)` for progress and the
    eventual `scriptCompleted`/`scriptFailed` outcome, or use
    `cancel_script`/`pause_script`/`resume_script` to control the run.

    `location_id`, if given, identifies a saved `Observatory` (see `save_observatory`) this
    run should use — currently only consumed by `capture_frame`'s celestial-context FITS
    headers (INDIMCP-60), best-effort even when given (see `script_engine.execute_script`).
    An unknown `location_id` fails the run (`scriptFailed`), matching a bad `rig_id`.
    """
    return await script_runs.start_script(script_id, rig_id, parameters, location_id=location_id)


@mcp.tool()
def get_script_status(run_id: str) -> ScriptRunStatus:
    """Return the most recently known status for a run started by `run_script`."""
    return script_runs.get_script_status(run_id)


@mcp.tool()
async def cancel_script(run_id: str) -> ScriptRunStatus:
    """Cancel a run started by `run_script`, waiting for it to actually stop.

    Always applies, regardless of whether the run is pausable — unlike
    `pause_script`/`resume_script`.
    """
    return await script_runs.cancel_script(run_id)


@mcp.tool()
def pause_script(run_id: str) -> ScriptRunPaused | ScriptRunPauseRejected:
    """Pause a run at its next safe point — only if its script declared itself `pausable`.

    Rejected (not queued or silently ignored) if the script has no safe
    point to suspend at.
    """
    return script_runs.pause_script(run_id)


@mcp.tool()
def resume_script(run_id: str) -> ScriptRunResumed | ScriptRunPauseRejected:
    """Resume a run previously paused with `pause_script`."""
    return script_runs.resume_script(run_id)


@mcp.resource("indi://scripts", mime_type="application/json")
def read_script_event_stream() -> dict[str, list[ScriptRunStatus]]:
    """The rolling window of recent scripting-layer events, newest first.

    Same subscription mechanism and best-effort caveat as
    `read_indi_message_stream` — see `docs/Design.md#event-streams`.
    """
    return cast(dict[str, list[ScriptRunStatus]], event_streams.read_scripts())


@mcp.resource("indi://scripts/{runId}", mime_type="application/json")
def read_script_event_stream_for_run(runId: str) -> dict[str, list[ScriptRunStatus]]:
    """Same as `read_script_event_stream`, scoped to events from one `runId`.

    `runId` is unquoted before filtering — see `read_indi_message_stream_for_device`.
    """
    return cast(dict[str, list[ScriptRunStatus]], event_streams.read_scripts(unquote(runId)))


@mcp.tool()
async def get_events(
    stream: event_log.Stream,
    device: str | None = None,
    run_id: str | None = None,
    since: str | None = None,
) -> list[event_log.EventRecord]:
    """Catch up on missed `indi://messages`/`indi://scripts` events from the durable event log.

    Unlike the live `resources/subscribe` channel (best-effort, live-only —
    see `docs/Design.md#event-streams`), this queries the durable SQLite
    log every event is also written to, so a client that was disconnected
    can reliably fetch what it missed rather than assuming the live
    subscription caught everything. `since` should be the `occurredAt` of
    the last event the caller actually saw — but the filter is inclusive,
    so that same event is returned again rather than excluded (see
    `event_log.get_events` for why); dedupe by `id` if polling repeatedly.
    Events older than a day are purged (see `event_log.purge_old_events`),
    so this isn't a substitute for permanent history. Returned oldest
    first — the natural order for replaying missed events.
    """
    return await asyncio.to_thread(
        event_log.get_events, stream, device=device, run_id=run_id, since=since
    )


@mcp.tool()
async def list_frames(
    run_id: str | None = None,
    device: str | None = None,
    since: str | None = None,
    transferred: bool | None = None,
) -> list[FrameMetadata]:
    """List captured frame metadata, most recently captured first, with optional filters.

    `transferred` is a tri-state: omitted/`None` returns every frame,
    `true` only ones this call has already confirmed received
    (`confirm_frame_transfer`), `false` only ones still waiting to be
    retrieved — useful for checking what's left to download before
    running `purge_transferred_frames`. Never returns a frame's on-disk
    path; read its actual bytes via the `frame://{frameId}` resource.
    """
    return await asyncio.to_thread(
        frame_store.list_frames, run_id=run_id, device=device, since=since, transferred=transferred
    )


@mcp.tool()
async def get_frame_metadata(frame_id: str) -> FrameMetadata:
    """Return the metadata for a single captured frame identified by `frame_id`."""
    return await asyncio.to_thread(frame_store.get_frame_metadata, frame_id)


@mcp.tool()
async def confirm_frame_transfer(frame_id: str) -> FrameMetadata:
    """Confirm the Client Computer has safely saved a copy of `frame_id`.

    Sets `transferredAt`. Call this only after actually verifying the
    bytes read from `frame://{frameId}` were received intact — this is
    what makes a frame eligible for `delete_frame`/`purge_transferred_frames`
    later, so confirming a transfer that didn't really complete risks
    losing the only copy of that frame.
    """
    return await asyncio.to_thread(frame_store.confirm_frame_transfer, frame_id)


@mcp.tool()
async def delete_frame(frame_id: str, require_transferred: bool = True) -> FrameMetadata:
    """Delete a single captured frame's file and metadata, returning its metadata as it was.

    Refuses to delete a frame that hasn't been confirmed transferred yet
    (via `confirm_frame_transfer`) unless `require_transferred` is
    explicitly set to `false` — this is a destructive action on the
    actual science data this server exists to capture, so it's safe by
    default rather than trusting every caller to check first.
    """
    return await asyncio.to_thread(
        frame_store.delete_frame, frame_id, require_transferred=require_transferred
    )


@mcp.tool()
async def purge_transferred_frames(older_than_days: float) -> list[FrameMetadata]:
    """Bulk-delete every already-transferred frame captured more than `older_than_days` ago.

    Never runs automatically — this is the only way old frames get
    cleaned up, since the INDI Device's own storage is limited. Only ever
    considers frames already confirmed transferred (see
    `confirm_frame_transfer`), regardless of age; a frame the Client
    Computer hasn't confirmed receiving yet is never deleted by this call.
    Returns the metadata of every frame actually deleted.
    """
    return await asyncio.to_thread(
        frame_store.purge_transferred_frames, older_than=timedelta(days=older_than_days)
    )


# `frameId` below (not `frame_id`): FastMCP requires the parameter name to
# match the `{frameId}` placeholder in the URI template exactly.
@mcp.resource("frame://{frameId}", mime_type="application/octet-stream")
async def read_frame(frameId: str) -> bytes:
    """Read a captured frame's raw bytes (e.g. FITS data), identified by its `frameId`.

    Returned as a base64 `blob` resource content, per
    `docs/Design.md#retrieving-frames` — reuses MCP's standard binary
    resource handling rather than a bespoke download tool. The whole frame
    is read into memory and returned in one response: this SDK's resource
    mechanism has no native chunked/range read, so a very large frame is
    fully buffered here. Left as-is per Design.md's own open question on
    this ("deferred until real frame sizes are known" rather than solved
    speculatively) — not an oversight.
    """
    path = await asyncio.to_thread(frame_store.get_frame_path, frameId)
    return await asyncio.to_thread(path.read_bytes)


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def run(transport: Transport = "stdio", host: str = "127.0.0.1", port: int = 8000) -> None:
    """Start serving the MCP server over the given transport.

    `host`/`port` only apply to the `sse` and `streamable-http` transports.
    """
    logging.basicConfig(level=logging.INFO)
    rig_store.load_rigs()
    observatory_store.load_observatories()
    script_store.load_scripts()
    if transport != "stdio":
        mcp.settings.host = host
        mcp.settings.port = port
        if host not in _LOOPBACK_HOSTS:
            # FastMCP's constructor auto-enables DNS-rebinding protection, but only
            # when it sees a loopback `host` — and only at construction time, so
            # mutating `mcp.settings.host` above doesn't retrigger it. Left alone,
            # that protection's `allowed_hosts` stays locked to 127.0.0.1/localhost,
            # rejecting every request from a non-loopback `Host` header — which is
            # every request once `streamable-http` is bound to a LAN-reachable host
            # (INDIMCP-54), the documented production setup (see docs/Deployment.md).
            mcp.settings.transport_security = TransportSecuritySettings(
                enable_dns_rebinding_protection=False
            )
            logger.warning(
                "DNS-rebinding protection disabled: host=%s is not loopback. This trades "
                "off protection against a DNS-rebinding attacker on the same LAN in "
                "exchange for LAN clients being able to reach this server at all — see "
                "docs/Deployment.md's Hardening notes.",
                host,
            )
        logger.info(
            "Starting indi-mcp server (transport=%s, host=%s, port=%d)", transport, host, port
        )
    else:
        logger.info("Starting indi-mcp server (transport=%s)", transport)
    mcp.run(transport=transport)
