"""The INDI MCP server instance and its entrypoint."""

import asyncio
import base64
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
    astrometry_index,
    event_log,
    event_streams,
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
)
from indi_mcp.frame_store import FrameMetadata
from indi_mcp.indi_driver import DriverInfo, DriverStatus
from indi_mcp.indi_messaging import IndiEvent, MessagingStatus
from indi_mcp.indi_server import INDI_PORT, IndiServerStatus
from indi_mcp.observatory_store import Observatory, ObservatorySummary
from indi_mcp.rig_store import DraftDeviceInfo, Rig, RigCheck, RigDraft, RigSuggestion, RigSummary
from indi_mcp.script_engine import FilterAdoptOutcome, FilterSyncOutcome
from indi_mcp.script_runs import (
    ScriptRunPaused,
    ScriptRunPauseRejected,
    ScriptRunResumed,
    ScriptRunStarted,
    ScriptRunStatus,
)
from indi_mcp.script_store import FrameType, Script, ScriptSummary

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


def _resolve_unique_connected_component(rig_id: str, role: str) -> rig_store.Component:
    """The single, device-connected component of `rig_id` matching `role` (INDIMCP-64).

    A rig's `role` is explicitly allowed to be shared by more than one component
    (`rig_store.Component.role`'s own docstring), so `sync_filter_names`/
    `adopt_filter_names_from_driver` can't just take the first match the way a careless
    `next(...)` would — that risks silently reading one physical filter wheel's `device`/
    `slots` while `rig_store.update_component_slots` (called by both tools) writes to *every*
    component sharing the role. Raising here if `role` doesn't resolve to exactly one
    connected component mirrors `script_engine._resolve_role_to_component`'s own strict
    behavior for a script run resolving roles to devices.
    """
    rig = rig_store.get_rig(rig_id)
    matches = [c for c in rig.components if c.role == role and c.device is not None]
    if len(matches) != 1:
        raise ValueError(
            f"rig {rig_id!r} has {len(matches)} connected component(s) for role {role!r}; "
            "expected exactly one"
        )
    return matches[0]


@mcp.tool()
async def sync_filter_names(rig_id: str, role: str) -> FilterSyncOutcome:
    """Push `rig_id`'s configured filter names for `role` to the EFW driver's live
    `FILTER_NAME`, if they disagree (INDIMCP-64).

    A deliberate action only: `select_filter` never does this on its own — it either adopts
    the driver's names onto the rig if the rig has no `slots` configured, or fails fatally if
    it does and they disagree, but never overwrites the driver itself — since overwriting a
    live device's own configuration should always be something an operator or client
    explicitly asked for. Call this tool (or use a script's own explicit `sync_filter_names`
    step) when that's actually what's wanted; see `adopt_filter_names_from_driver` for the
    reverse direction (copying the driver's config onto the rig instead). Raises if `role`
    isn't a connected `filterWheel`-like component with `slots` configured, if the device
    doesn't expose `FILTER_NAME`, or if the rig and driver declare a different *number* of
    filter slots (refuses to push a configuration for what's likely a differently-sized wheel).
    """
    component = _resolve_unique_connected_component(rig_id, role)
    assert component.device is not None  # guaranteed by _resolve_unique_connected_component
    return await script_engine.sync_filter_names(role, component.device, component.slots or {})


@mcp.tool()
async def adopt_filter_names_from_driver(rig_id: str, role: str) -> FilterAdoptOutcome:
    """Copy the EFW driver's live `FILTER_NAME` for `role` onto rig `rig_id`, overwriting
    whatever filter `slots` the rig currently declares (INDIMCP-64) — the reverse direction
    from `sync_filter_names`.

    A deliberate action only, for when a rig and its driver disagree and the operator decides
    the *driver* is the source of truth this time. `select_filter`'s own automatic
    reconciliation never overwrites a rig that already has `slots` configured (it fails
    fatally on disagreement instead, requiring the operator to choose a direction explicitly);
    this tool (or a script's own explicit `adopt_filter_names_from_driver` step) is that
    choice. Raises if `role` isn't a connected `filterWheel`-like component, if the device
    doesn't expose `FILTER_NAME`, if the driver declares no filter slots at all, or if
    persisting the change to the rig's YAML file fails.
    """
    component = _resolve_unique_connected_component(rig_id, role)
    assert component.device is not None  # guaranteed by _resolve_unique_connected_component
    return await script_engine.adopt_filter_names_from_driver(
        rig_id, role, component.device, component.slots or {}
    )


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


# Typed, named convenience wrappers around `run_script` for the most common built-in scripts
# (INDIMCP-49) — each is a thin passthrough to `script_runs.start_script` with its `script_id`
# fixed and its own parameters given proper types instead of a bare `dict[str, Any]`, for
# better client-side discoverability (an MCP tool's declared parameter schema, not just its
# docstring). Each still returns immediately with a `runId` exactly like `run_script` itself —
# same execution path, same `get_script_status`/`cancel_script`/`pause_script`/`resume_script`
# story — since a script run can take anywhere from sub-second (park) to many minutes
# (capture_frame's exposure), and this project never blocks an MCP tool call on that (see
# `run_script`'s own docstring). The underlying `scripts/*.yaml` file is still there and still
# the thing a composed sequence's own `run_script`/`repeat`/`if` steps call into — this is an
# additional entry point, not a replacement for the scripting layer, which stays how more
# complex multi-step sequences (e.g. `capture_light_sequence`) get built.


@mcp.tool()
async def park(rig_id: str) -> ScriptRunStarted:
    """Park the rig's mount — see `scripts/park.yaml` (INDIMCP-48)."""
    return await script_runs.start_script("park", rig_id, {})


@mcp.tool()
async def unpark(rig_id: str) -> ScriptRunStarted:
    """Unpark the rig's mount — see `scripts/unpark.yaml` (INDIMCP-48)."""
    return await script_runs.start_script("unpark", rig_id, {})


@mcp.tool()
async def slew(rig_id: str, ra: float, dec: float) -> ScriptRunStarted:
    """Slew the rig's mount to a fixed RA/Dec — see `scripts/slew.yaml` (INDIMCP-8).

    `ra` is in hours, `dec` in degrees. Slewing to a named object (e.g. "M101") isn't
    supported yet (INDIMCP-29).
    """
    return await script_runs.start_script("slew", rig_id, {"ra": ra, "dec": dec})


@mcp.tool()
async def cool_camera(
    rig_id: str, targetTempC: float = -10, timeoutSeconds: float = 300
) -> ScriptRunStarted:
    """Cool the rig's camera to `targetTempC` and wait for it to stabilize — see
    `scripts/cool_camera.yaml` (INDIMCP-56)."""
    return await script_runs.start_script(
        "cool_camera", rig_id, {"targetTempC": targetTempC, "timeoutSeconds": timeoutSeconds}
    )


@mcp.tool()
async def select_filter(rig_id: str, filterName: str) -> ScriptRunStarted:
    """Select a filter on the rig's filter wheel by name — see `scripts/select_filter.yaml`
    (INDIMCP-61).

    Reconciles the rig's configured filter names against the driver's live state before
    selecting (adopts the driver's names if the rig has none configured, fails fatally on a
    real disagreement) — see `sync_filter_names`/`adopt_filter_names_from_driver` for how to
    resolve a disagreement explicitly.
    """
    return await script_runs.start_script("select_filter", rig_id, {"filterName": filterName})


@mcp.tool()
async def set_focus_position(rig_id: str, position: int) -> ScriptRunStarted:
    """Move the rig's focuser to an absolute position — see `scripts/set_focus_position.yaml`
    (INDIMCP-62/63). Checked against the rig component's own `minPosition`/`maxPosition`,
    if declared.
    """
    return await script_runs.start_script("set_focus_position", rig_id, {"position": position})


@mcp.tool()
async def connect(rig_id: str, role: str) -> ScriptRunStarted:
    """Connect whichever device fills `role` in `rig_id` — see `scripts/connect.yaml`."""
    return await script_runs.start_script("connect", rig_id, {"role": role})


@mcp.tool()
async def disconnect(rig_id: str, role: str) -> ScriptRunStarted:
    """Disconnect whichever device fills `role` in `rig_id` — see `scripts/disconnect.yaml`."""
    return await script_runs.start_script("disconnect", rig_id, {"role": role})


@mcp.tool()
async def capture_frame(
    rig_id: str,
    exposureSeconds: float,
    frameType: FrameType = "Light",
    binningX: int = 1,
    binningY: int = 1,
    gain: float | None = None,
    offset: float | None = None,
    frameX: int | None = None,
    frameY: int | None = None,
    frameWidth: int | None = None,
    frameHeight: int | None = None,
    location_id: str | None = None,
) -> ScriptRunStarted:
    """Capture a single frame from the rig's camera — see `scripts/capture_frame.yaml`
    (INDIMCP-44).

    `gain`/`offset` omitted (the default) leave the device's current setting alone rather
    than sending a fixed number. `frameX`/`frameY`/`frameWidth`/`frameHeight` default to the
    full sensor; set all four together for a sub-frame. `location_id` is passed straight
    through to `run_script` for this script's own celestial-context FITS header enrichment.
    """
    return await script_runs.start_script(
        "capture_frame",
        rig_id,
        {
            "exposureSeconds": exposureSeconds,
            "frameType": frameType,
            "binningX": binningX,
            "binningY": binningY,
            "gain": gain,
            "offset": offset,
            "frameX": frameX,
            "frameY": frameY,
            "frameWidth": frameWidth,
            "frameHeight": frameHeight,
        },
        location_id=location_id,
    )


@mcp.tool()
async def plate_solve(
    rig_id: str,
    exposureSeconds: float | None = None,
    syncMount: bool = True,
    timeoutSeconds: float = 60,
) -> ScriptRunStarted:
    """Plate-solve a frame via astrometry.net's local solve-field — see
    `scripts/plate_solve.yaml` (INDIMCP-27/45).

    `exposureSeconds` captures a fresh frame first; omit it to solve whichever frame was
    most recently captured in this run. `syncMount` (default `True`) syncs the mount's
    coordinates to the solved position. Does not retry toward a target tolerance — see
    `plate_solve_until_precision` (INDIMCP-47) for that.
    """
    return await script_runs.start_script(
        "plate_solve",
        rig_id,
        {
            "exposureSeconds": exposureSeconds,
            "syncMount": syncMount,
            "timeoutSeconds": timeoutSeconds,
        },
    )


@mcp.tool()
async def plate_solve_uploaded_frame(
    fitsDataBase64: str,
    raHintHours: float | None = None,
    decHintDeg: float | None = None,
    scaleLowArcsecPerPixel: float | None = None,
    scaleHighArcsecPerPixel: float | None = None,
    timeoutSeconds: float = 60,
) -> plate_solver.UploadedFrameSolveResult:
    """Plate-solve a FITS file the Client Computer supplies directly — INDIMCP-76 — rather
    than one captured from a rig's camera (see `plate_solve` for that).

    `fitsDataBase64` is the FITS file's raw bytes, base64-encoded (MCP tool arguments are
    JSON; there's no binary parameter type, so this is the same encoding `frame://{frameId}`
    already returns captured frames in, just in the upload direction).

    Saved into the frame store (`device="uploaded"`, no `run_id`) before solving, so it's
    retrievable afterward via `list_frames`/`frame://{frameId}` like any other frame — WCS
    headers included if the solve succeeds, kept even if it doesn't, so a failed solve
    doesn't lose the upload.

    There's no rig/mount to derive a position or plate-scale hint from automatically (unlike
    `plate_solve`, which reads both off the rig's own configuration and the mount's live
    coordinates) — pass `raHintHours`/`decHintDeg` and/or `scaleLowArcsecPerPixel`/
    `scaleHighArcsecPerPixel` directly if known, to narrow and speed up the search; omit
    either pair for an unhinted solve (slower, still valid).

    Raises an error if `fitsDataBase64` doesn't decode to a file `astropy.io.fits` can open,
    or if `solve-field` doesn't solve it within `timeoutSeconds` (the frame is still saved
    and retrievable in that case — see `plate_solver.solve_uploaded_frame`).
    """
    data = await asyncio.to_thread(base64.b64decode, fitsDataBase64)
    return await plate_solver.solve_uploaded_frame(
        data,
        ra_hint_hours=raHintHours,
        dec_hint_deg=decHintDeg,
        scale_low_arcsec=scaleLowArcsecPerPixel,
        scale_high_arcsec=scaleHighArcsecPerPixel,
        timeout_seconds=timeoutSeconds,
    )


@mcp.tool()
async def plate_solve_until_precision(
    rig_id: str,
    exposureSeconds: float,
    toleranceArcsec: float = 30,
    maxAttempts: int = 3,
    timeoutSeconds: float = 60,
) -> ScriptRunStarted:
    """Repeatedly plate-solve, sync, and re-slew toward the rig's mount's own commanded
    target until within `toleranceArcsec`, or `maxAttempts` is exhausted — see
    `scripts/plate_solve_until_precision.yaml` (INDIMCP-27/47).

    Requires a prior slew (so the mount's `TARGET_EOD_COORD` — the last commanded slew
    target — is actually set to something meaningful); fails immediately if it isn't.
    """
    return await script_runs.start_script(
        "plate_solve_until_precision",
        rig_id,
        {
            "exposureSeconds": exposureSeconds,
            "toleranceArcsec": toleranceArcsec,
            "maxAttempts": maxAttempts,
            "timeoutSeconds": timeoutSeconds,
        },
    )


@mcp.tool()
async def list_astrometry_index_files(
    catalog: astrometry_index.Catalog = "tycho2",
    rig_id: str | None = None,
) -> list[astrometry_index.IndexFileStatus]:
    """List every scale `catalog` publishes and whether it's installed under
    `INDI_MCP_ASTROMETRY_INDEX_DIR` (INDIMCP-77) — see `docs/PlateSolve.md`.

    `catalog` is `"tycho2"` (wide fields, 22 arcmin-33deg) or `"2mass"` (the full 2-2000
    arcmin range) — the same two choices Ekos's own index-file downloader offers.

    If `rig_id` is given, each entry's `neededForRig` flags whether that scale's coverage
    (padded by `astrometry_index.DEFAULT_RIG_MARGIN_SCALES` extra scales on each side, since
    a rig's computed field of view is only ever an estimate) actually overlaps the rig's own
    configured field of view (its `telescope` component's `focalLengthMm` and `camera`
    component's `pixelSizeMicron`/`pixelsX`/`pixelsY`) — `None` throughout if `rig_id` is
    omitted, or if the rig doesn't have enough optics configured to compute a field of view
    from.
    """
    rig = rig_store.get_rig(rig_id) if rig_id is not None else None
    return await asyncio.to_thread(astrometry_index.list_index_files, catalog=catalog, rig=rig)


@mcp.tool()
async def download_astrometry_index_files(
    catalog: astrometry_index.Catalog = "tycho2",
    indexNumbers: list[int] | None = None,
    minArcmin: float | None = None,
    maxArcmin: float | None = None,
    rig_id: str | None = None,
) -> list[int]:
    """Download whichever `catalog` index files aren't already installed under
    `INDI_MCP_ASTROMETRY_INDEX_DIR` (INDIMCP-77) — see `docs/PlateSolve.md`.

    `catalog` is `"tycho2"` (wide fields, 22 arcmin-33deg) or `"2mass"` (the full 2-2000
    arcmin range) — the same two choices Ekos's own index-file downloader offers.

    Pass **exactly one** of: `indexNumbers` (explicit scale numbers `catalog` publishes),
    `minArcmin`/`maxArcmin` together (every scale covering that exact field-of-view range),
    or `rig_id` (computes `minArcmin`/`maxArcmin` from that rig's own configured optics, then
    pads by `astrometry_index.DEFAULT_RIG_MARGIN_SCALES` extra scales on each side — the same
    way `list_astrometry_index_files`'s `neededForRig` does, and for the same reason: a rig's
    computed field of view is only ever an estimate, so this installs a little headroom
    rather than exactly one bracket that might just miss) — rejected if none or more than one
    is given, rather than silently prioritizing one, so a call that accidentally passes two
    (e.g. `rig_id` alongside explicit `indexNumbers`) fails loudly instead of quietly
    ignoring one of them. Returns the scale numbers that had at least one file actually
    downloaded — already-fully-installed ones are left alone.
    """
    arcmin_range_given = minArcmin is not None or maxArcmin is not None
    selectors_given = sum([indexNumbers is not None, rig_id is not None, arcmin_range_given])
    if selectors_given != 1:
        raise ValueError(
            "pass exactly one of indexNumbers, rig_id, or both minArcmin and maxArcmin"
        )
    if arcmin_range_given and (minArcmin is None or maxArcmin is None):
        raise ValueError("pass both minArcmin and maxArcmin together, not just one")

    if indexNumbers is not None:
        return await astrometry_index.download_index_files(indexNumbers, catalog=catalog)

    margin_scales = 0
    if rig_id is not None:
        rig = rig_store.get_rig(rig_id)
        minArcmin, maxArcmin = astrometry_index.field_of_view_arcmin_for_rig(rig)
        margin_scales = astrometry_index.DEFAULT_RIG_MARGIN_SCALES
    # Reached only when exactly one selector was given (checked above) and it wasn't
    # indexNumbers -- so either rig_id just resolved both above, or arcmin_range_given's own
    # check already confirmed neither is None. Asserted here purely for the type checker.
    assert minArcmin is not None
    assert maxArcmin is not None
    index_numbers = astrometry_index.index_numbers_for_field_of_view(
        minArcmin, maxArcmin, catalog=catalog, margin_scales=margin_scales
    )
    if not index_numbers:
        raise ValueError(f"no known {catalog!r} scale covers {minArcmin}-{maxArcmin} arcmin")
    return await astrometry_index.download_index_files(index_numbers, catalog=catalog)


@mcp.tool()
async def track_off(rig_id: str) -> ScriptRunStarted:
    """Turn off the rig's mount tracking — see `scripts/track_off.yaml` (INDIMCP-49)."""
    return await script_runs.start_script("track_off", rig_id, {})


@mcp.tool()
async def set_track_mode(rig_id: str, modeSwitchElement: str) -> ScriptRunStarted:
    """Select the rig's mount tracking mode — see `scripts/set_track_mode.yaml` (INDIMCP-49).

    `modeSwitchElement` is the INDI `TELESCOPE_TRACK_MODE` switch member to enable, e.g.
    `"TRACK_SIDEREAL"`, `"TRACK_SOLAR"`, `"TRACK_LUNAR"`, or `"TRACK_CUSTOM"` (pair the last
    with `set_custom_tracking_rate` to also set a custom rate).
    """
    return await script_runs.start_script(
        "set_track_mode", rig_id, {"modeSwitchElement": modeSwitchElement}
    )


@mcp.tool()
async def set_custom_tracking_rate(
    rig_id: str, raRateArcsecPerSec: float, decRateArcsecPerSec: float
) -> ScriptRunStarted:
    """Select custom tracking on the rig's mount and set its RA/Dec rate — see
    `scripts/set_custom_tracking_rate.yaml` (INDIMCP-49)."""
    return await script_runs.start_script(
        "set_custom_tracking_rate",
        rig_id,
        {
            "raRateArcsecPerSec": raRateArcsecPerSec,
            "decRateArcsecPerSec": decRateArcsecPerSec,
        },
    )


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
