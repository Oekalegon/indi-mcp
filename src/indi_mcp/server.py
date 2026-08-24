"""The INDI MCP server instance and its entrypoint."""

import asyncio
import base64
import contextlib
import logging
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Annotated, Any, Literal, cast
from urllib.parse import quote, unquote

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import McpError
from mcp.types import INVALID_PARAMS, ErrorData
from pydantic import AnyUrl, Field
from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse, Response

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
    server_info,
)
from indi_mcp.event_streams import ConnectionEvent
from indi_mcp.flat_calibration_sweep import FlatCalibrationSweepStarted, FlatCalibrationSweepStatus
from indi_mcp.frame_store import FrameMetadata
from indi_mcp.indi_driver import DriverInfo, DriverStatus
from indi_mcp.indi_messaging import DeviceProperties, IndiEvent, MessagingStatus
from indi_mcp.indi_server import INDI_PORT, IndiServerStatus
from indi_mcp.issues import Issue, Severity
from indi_mcp.observatory_store import (
    DraftLocationDeviceInfo,
    Observatory,
    ObservatoryDraft,
    ObservatorySummary,
)
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
from indi_mcp.sensor_calibration_sweep import (
    SensorCalibrationSweepStarted,
    SensorCalibrationSweepStatus,
)
from indi_mcp.server_info import ServerInfo

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
    """Advertise resource subscription support for `indi://messages`/`indi://mcp-server`.

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
    resource (`foo://bar`) would silently "succeed" — `event_streams` would register the
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
                    "indi://messages(/{device}), indi://mcp-server/scripts(/{runId}), or "
                    "indi://mcp-server/connection(/{target})"
                ),
            )
        )
    return uri_str


@mcp._mcp_server.subscribe_resource()
async def _subscribe_to_event_stream(uri: AnyUrl) -> None:
    """Handle `resources/subscribe` for `indi://messages`/`indi://mcp-server` (and their scoped
    forms).

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
async def get_server_info() -> ServerInfo:
    """Report this MCP server's package version and last-merge build timestamp."""
    return server_info.get_server_info()


@mcp.tool()
async def manage_indi_infra(
    component: Literal["server", "driver", "messaging"],
    action: Literal["start", "stop", "restart"],
    label: str | None = None,
    host: str | None = None,
    port: int | None = None,
) -> IndiServerStatus | DriverStatus | MessagingStatus:
    """Start, stop, or restart the INDI server, a single INDI driver, or the messaging
    connection — replaces the old `start/stop/restart_indi_server`, `start/stop_indi_driver`,
    and `start/stop_indi_messaging` tools (INDIMCP-114).

    Valid `action`s depend on `component`: `"server"` supports `start`/`stop`/`restart`;
    `"driver"` and `"messaging"` support only `start`/`stop` — there is no `restart` for
    either (for a driver, stop then start; for messaging, reconnect the same way) — raises
    `ValueError` if asked for one. `label` is required for, and only valid with,
    `component="driver"` (the driver's catalog label, e.g. `"CCD Simulator"`). `host`/`port`
    are only valid with `component="messaging"`'s `start` (`host` defaults to `"localhost"`,
    `port` to `INDI_PORT`) or `component="server"`'s `start`/`restart` (`port`, defaulting to
    `INDI_PORT`). Any other combination — e.g. `label` with `component="server"`, or `port`
    with `component="driver"` — raises `ValueError` rather than silently ignoring the
    irrelevant argument.
    """
    if component == "server":
        if label is not None:
            raise ValueError('label is only valid with component="driver"')
        if host is not None:
            raise ValueError('host is only valid with component="messaging"')
        return await _manage_indi_server(action, port)

    if component == "driver":
        if host is not None or port is not None:
            raise ValueError(
                'host/port are only valid with component="server" or component="messaging"'
            )
        return await _manage_indi_driver(action, label)

    if label is not None:
        raise ValueError('label is only valid with component="driver"')
    return await _manage_indi_messaging(action, host, port)


async def _manage_indi_server(
    action: Literal["start", "stop", "restart"], port: int | None
) -> IndiServerStatus:
    """`manage_indi_infra`'s `component="server"` branch, extracted for readability."""
    if action == "start":
        return await indi_server.start_server(port if port is not None else INDI_PORT)
    if action == "stop":
        if port is not None:
            raise ValueError('port is only valid with action="start" or "restart"')
        return await indi_server.stop_server()
    return await indi_server.restart_server(port)


async def _manage_indi_driver(
    action: Literal["start", "stop", "restart"], label: str | None
) -> DriverStatus:
    """`manage_indi_infra`'s `component="driver"` branch, extracted for readability."""
    if label is None:
        raise ValueError('label is required for component="driver"')
    if action == "start":
        return await indi_driver.start_driver(label)
    if action == "stop":
        return await indi_driver.stop_driver(label)
    raise ValueError('action="restart" is not supported for component="driver" — stop then start')


async def _manage_indi_messaging(
    action: Literal["start", "stop", "restart"], host: str | None, port: int | None
) -> MessagingStatus:
    """`manage_indi_infra`'s `component="messaging"` branch, extracted for readability."""
    if action == "start":
        return await indi_messaging.start_messaging(
            host if host is not None else "localhost",
            port if port is not None else INDI_PORT,
        )
    if action == "stop":
        if host is not None or port is not None:
            raise ValueError('host/port are only valid with action="start"')
        return await indi_messaging.stop_messaging()
    raise ValueError(
        'action="restart" is not supported for component="messaging" — stop then start'
    )


@mcp.tool()
async def get_indi_status(
    component: Literal["server", "messaging"],
) -> IndiServerStatus | MessagingStatus:
    """Report whether the INDI server or the messaging connection is running — replaces
    `get_indi_server_status`/`get_indi_messaging_status` (INDIMCP-114).

    No `component="driver"` here — per-driver status is `list_indi_drivers(scope="running")`,
    which reports every running driver at once rather than one at a time.
    """
    if component == "server":
        return await indi_server.get_status()
    return await indi_messaging.get_status()


@mcp.tool()
async def list_indi_drivers(
    scope: Literal["catalog", "running"],
) -> list[DriverInfo] | list[DriverStatus]:
    """List INDI drivers — either the full catalog installed on this device
    (`scope="catalog"`) or only those currently running (`scope="running"`) — replaces
    `list_indi_driver_catalog`/`list_running_indi_drivers` (INDIMCP-114).
    """
    if scope == "catalog":
        return await indi_driver.get_driver_catalog()
    return await indi_driver.list_running_drivers()


@mcp.tool()
async def indi_property(
    action: Literal["get", "set"],
    device: str,
    name: str | None = None,
    elements: dict[str, str] | None = None,
) -> DeviceProperties | IndiEvent:
    """Get the live state of every property on an INDI device, or set one property's
    elements — replaces `get_device_properties`/`send_indi_property` (INDIMCP-114).

    `action="get"` queries `indiserver` directly (`getProperties`) rather than returning
    whatever was last cached, so the result reflects the device's actual state at call time
    when possible — check the returned `refreshed` flag, which is `False` if the driver
    didn't respond in time and `properties` fell back to a previously-cached reading. `name`/
    `elements` are not valid with `action="get"`. `action="set"` sends `elements` to
    `device`'s property `name`, and requires both — raises `ValueError` if either is missing,
    or if `name`/`elements` are given alongside `action="get"`.
    """
    if action == "get":
        if name is not None or elements is not None:
            raise ValueError('name/elements are only valid with action="set"')
        return await indi_messaging.get_device_properties(device)
    if name is None or elements is None:
        raise ValueError('action="set" requires both name and elements')
    return await indi_messaging.send_property(device, name, elements)


@mcp.resource("indi://messages", mime_type="application/json")
def read_indi_message_stream() -> dict[str, list[IndiEvent]]:
    """The rolling window of recent INDI messaging-layer events, newest first.

    Subscribable via `resources/subscribe`, per `docs/Design.md#event-
    streams`: a subscriber is sent `notifications/resources/updated`
    whenever a new event is published, and re-reads this resource to fetch
    it. This is a best-effort, live-only channel — a client that was
    disconnected should use `get_events(stream="messages")` (the durable
    event log, INDIMCP-15) to catch up, not assume it saw everything.
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
def list_config(
    kind: Literal["rig", "observatory", "script"],
) -> list[RigSummary] | list[ObservatorySummary] | list[ScriptSummary]:
    """List the id/name of every configured rig, observatory, or script — replaces
    `list_rigs`/`list_observatories`/`list_scripts` (INDIMCP-115).

    See `docs/RigSchema.md`, `docs/ObservatorySchema.md`, `docs/ScriptSchema.md` for the
    full shape of each `kind`.
    """
    if kind == "rig":
        return rig_store.list_rigs()
    if kind == "observatory":
        return observatory_store.list_observatories()
    return script_store.list_scripts()


def _defuse_schema_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Rename `model_json_schema()`'s reserved `$ref`/`$defs` keys to the non-reserved
    `x-ref`/`x-defs` (the standard JSON Schema vendor-extension prefix), so embedding this
    schema inside another tool's `json_schema_extra` doesn't break that tool's own schema
    generation.

    `pydantic`'s `GenerateJsonSchema` walks the *entire* final document for any literal
    `$ref` key while resolving its own model's schema — including inert documentation data
    sitting inside an unrelated field's `json_schema_extra` (`_CONFIG_SCHEMA_BY_KIND`, below)
    — and tries to resolve it against its own internal definitions table regardless of what
    it actually points to or whether it was ever meant for pydantic to interpret. Rewriting
    the target path (rather than renaming the key) doesn't help: confirmed by
    `KeyError: '#/properties/config/schemaByKind/rig/$defs/Component'` even after pointing
    the ref at the real nested location — pydantic's walker doesn't understand a `$ref`
    outside its own model is meant for an external reader, not for its own resolution.
    Renaming the key entirely sidesteps this, since only the literal strings `$ref`/`$defs`
    trigger pydantic's special-casing. `x-`-prefixed keys are the standard JSON Schema
    convention for vendor/tool-specific extensions a generic consumer should ignore but a
    schema-aware one can still resolve — a reader of `schemaByKind` just needs to know this
    substitution to reconstruct the original schema, same as resolving any other `$ref`.
    """

    def _rewrite(node: Any) -> Any:
        if isinstance(node, dict):
            renamed = {
                ("x-ref" if key == "$ref" else "x-defs" if key == "$defs" else key): value
                for key, value in node.items()
            }
            return {key: _rewrite(value) for key, value in renamed.items()}
        if isinstance(node, list):
            return [_rewrite(item) for item in node]
        return node

    return cast(dict[str, Any], _rewrite(schema))


_CONFIG_SCHEMA_BY_KIND = {
    "rig": _defuse_schema_refs(Rig.model_json_schema()),
    "observatory": _defuse_schema_refs(Observatory.model_json_schema()),
    "script": _defuse_schema_refs(Script.model_json_schema()),
}
"""The real `Rig`/`Observatory`/`Script` JSON Schemas, keyed by `configuration`'s `kind`.

`configuration`'s own `config` parameter has to be a plain `dict[str, Any]` — one parameter
represents three different shapes depending on the sibling `kind` argument, and JSON Schema
can't make a parameter's shape conditional on another parameter's value (see
`docs/ToolSurfaceRedesign.md`'s "payload merged across `kind` loses its structural schema"
trade-off). Attaching the full schemas here as `config`'s `json_schema_extra` (below) restores
that visibility for a schema-reading client — without a discriminated union, which would mean
adding a shared discriminator field to `Rig`/`Observatory`/`Script`, breaking every existing
`rigs/`/`observatories/`/`scripts/*.yaml` file's on-disk schema for no benefit to the two
models here, which don't need one — `kind` already tells `configuration` which shape to
expect and validate against; this is documentation only, not enforced by FastMCP itself.
"""


@mcp.tool()
async def configuration(
    action: Literal["get", "save", "draft"],
    kind: Literal["rig", "observatory", "script"],
    config_id: str | None = None,
    config: Annotated[
        dict[str, Any] | None,
        Field(json_schema_extra={"schemaByKind": _CONFIG_SCHEMA_BY_KIND}),
    ] = None,
    overwrite: bool = False,
) -> Rig | Observatory | Script | RigDraft | ObservatoryDraft:
    """Get, save, or draft a rig/observatory/script configuration — replaces
    `get_rig`/`get_observatory`/`get_script`, `save_rig`/`save_observatory`/`save_script`,
    and `draft_rig`/`draft_observatory` (INDIMCP-115).

    `action="get"` requires `config_id` (the config to fetch), rejects `config`/`overwrite`.
    `action="save"` requires `config` — validated against the shape `kind` expects (`Rig`/
    `Observatory`/`Script`) before anything is written, same as the old `save_rig`/
    `save_observatory`/`save_script` tools — and rejects `config_id`, since the id to save
    under lives inside `config` itself (`config["id"]`), not as a separate argument. `config`'s
    own real per-`kind` JSON Schema (every required/optional field, nested shapes) is attached
    to its parameter schema under `schemaByKind` for a schema-reading caller — `config` itself
    stays a plain object here since its shape depends on the sibling `kind` argument, which
    JSON Schema can't express directly.
    `action="draft"` takes none of `config_id`/`config`/`overwrite`, and only supports
    `kind="rig"`/`kind="observatory"` — there is no `draft_script` equivalent (a script has no
    live-device state to pre-fill from); raises `ValueError` for `kind="script"`. Every other
    invalid combination — e.g. `config_id` alongside `action="save"`, or `config` alongside
    `action="get"` — also raises `ValueError` rather than silently ignoring the irrelevant
    argument.
    """
    if action == "get":
        if config is not None or overwrite:
            raise ValueError('config/overwrite are only valid with action="save"')
        if config_id is None:
            raise ValueError('action="get" requires config_id')
        if kind == "rig":
            return rig_store.get_rig(config_id)
        if kind == "observatory":
            return observatory_store.get_observatory(config_id)
        return script_store.get_script(config_id)

    if action == "save":
        if config_id is not None:
            raise ValueError('config_id is not valid with action="save" — pass it inside config')
        if config is None:
            raise ValueError('action="save" requires config')
        if kind == "rig":
            return await asyncio.to_thread(
                rig_store.save_rig, Rig.model_validate(config), overwrite=overwrite
            )
        if kind == "observatory":
            return await asyncio.to_thread(
                observatory_store.save_observatory,
                Observatory.model_validate(config),
                overwrite=overwrite,
            )
        return await asyncio.to_thread(
            script_store.save_script, Script.model_validate(config), overwrite=overwrite
        )

    if config_id is not None or config is not None or overwrite:
        raise ValueError('config_id/config/overwrite are not valid with action="draft"')
    if kind == "rig":
        return await _draft_rig()
    if kind == "observatory":
        return await _draft_observatory()
    raise ValueError('action="draft" is not supported for kind="script"')


async def _draft_rig() -> RigDraft:
    """`configuration`'s `action="draft"`/`kind="rig"` branch, extracted for readability.

    Pre-fills a draft rig skeleton from currently connected INDI devices, combining each
    device's driver family (camera/filter wheel/focuser/mount) with whatever live properties
    it exposes (CCD_INFO, FILTER_NAME, focuser range) into a starting point. Never
    auto-finalizes a rig: fields INDI can't supply and any ambiguous role assignments are left
    for the operator to complete and save themselves.
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


async def _draft_observatory() -> ObservatoryDraft:
    """`configuration`'s `action="draft"`/`kind="observatory"` branch, extracted for
    readability.

    Pre-fills a draft observatory location from a connected device's live
    `GEOGRAPHIC_COORD`. INDI's `GEOGRAPHIC_COORD` standard property (LAT/LONG/ELEV) is
    exposed by GPS drivers and often by mount drivers too. Never auto-selects or auto-saves a
    location: the result is a starting point — `id`/`name` have no INDI equivalent, and a
    stale/missing/all-zero fix is flagged in `notes` — for the operator to complete and save
    themselves via `configuration(action="save", kind="observatory", ...)`, consistent with
    `_draft_rig`.
    """
    devices: list[DraftLocationDeviceInfo] = []
    for name in indi_messaging.list_devices():
        coord = indi_messaging.get_property_values(name, "GEOGRAPHIC_COORD")
        devices.append(
            {
                "name": name,
                "geographicCoord": coord,
                "state": (
                    indi_messaging.get_property_state(name, "GEOGRAPHIC_COORD")
                    if coord is not None
                    else None
                ),
            }
        )
    return observatory_store.draft_observatory(devices)


def _resolve_unique_connected_component(rig_id: str, role: str) -> rig_store.Component:
    """The single, device-connected component of `rig_id` matching `role` (INDIMCP-64).

    A rig's `role` is explicitly allowed to be shared by more than one component
    (`rig_store.Component.role`'s own docstring), so `rig_diagnostics`'s `action="sync"`
    can't just take the first match the way a careless `next(...)` would — that risks
    silently reading one physical filter wheel's `device`/`slots` while
    `rig_store.update_component_slots` (called by both sync directions) writes to *every*
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
async def rig_diagnostics(
    action: Literal["check", "suggest", "sync"],
    rig_id: str | None = None,
    role: str | None = None,
    direction: Literal["to_driver", "from_driver"] | None = None,
) -> RigCheck | list[RigSuggestion] | FilterSyncOutcome | FilterAdoptOutcome:
    """Check a rig's device connectivity, suggest which configured rig is likely mounted, or
    reconcile a filter wheel's names against its driver — replaces `check_rig`, `suggest_rig`,
    and `sync_filter_names`/`adopt_filter_names_from_driver` (INDIMCP-115, INDIMCP-64).

    `action="check"` warns on any of `rig_id`'s devices that aren't currently connected — a
    warning, not a hard failure, since a rig might be intentionally used without one of its
    devices (e.g. imaging without a guide camera). `action="suggest"` proposes which
    configured rig is likely mounted, by matching connected INDI devices — never
    auto-selects; candidates are sorted best match first — and takes neither `rig_id` nor
    `role`/`direction`, since it considers every configured rig, not one.

    `action="sync"` requires `rig_id`, `role`, and `direction` together. `direction=
    "to_driver"` pushes `rig_id`'s configured filter names for `role` onto the EFW driver's
    live `FILTER_NAME`, if they disagree — a deliberate action only: `select_filter` never
    does this on its own (it either adopts the driver's names onto the rig if the rig has no
    `slots` configured, or fails fatally if it does and they disagree, but never overwrites
    the driver itself, since overwriting a live device's own configuration should always be
    something an operator or client explicitly asked for). `direction="from_driver"` is the
    reverse: copies the driver's live `FILTER_NAME` for `role` onto `rig_id`, overwriting
    whatever filter `slots` the rig currently declares — for when a rig and its driver
    disagree and the operator decides the *driver* is the source of truth this time. Either
    direction raises if `role` isn't a connected `filterWheel`-like component, or if the
    device doesn't expose `FILTER_NAME`; `to_driver` additionally raises if the rig and driver
    declare a different *number* of filter slots (refuses to push a configuration for what's
    likely a differently-sized wheel); `from_driver` additionally raises if the driver
    declares no filter slots at all, or if persisting the change to the rig's YAML file fails.

    Every other combination — `role`/`direction` with `action="check"`, `rig_id`/`role`/
    `direction` with `action="suggest"`, `action="sync"` missing `rig_id`/`role`/`direction`
    — raises `ValueError`.
    """
    if action == "suggest":
        if rig_id is not None or role is not None or direction is not None:
            raise ValueError('rig_id/role/direction are not valid with action="suggest"')
        return rig_store.suggest_rig(indi_messaging.list_devices())

    if rig_id is None:
        raise ValueError(f"action={action!r} requires rig_id")

    if action == "check":
        if role is not None or direction is not None:
            raise ValueError('role/direction are only valid with action="sync"')
        return rig_store.check_rig(rig_id, indi_messaging.list_devices())

    if role is None or direction is None:
        raise ValueError('action="sync" requires both role and direction')
    component = _resolve_unique_connected_component(rig_id, role)
    assert component.device is not None  # guaranteed by _resolve_unique_connected_component
    if direction == "to_driver":
        return await script_engine.sync_filter_names(role, component.device, component.slots or {})
    return await script_engine.adopt_filter_names_from_driver(
        rig_id, role, component.device, component.slots or {}
    )


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


_MOUNT_ACTION_PARAMS: dict[str, set[str]] = {
    "park": set(),
    "unpark": set(),
    "track_off": set(),
    "slew": {"ra", "dec"},
    "set_track_mode": {"modeSwitchElement"},
    "set_custom_tracking_rate": {"raRateArcsecPerSec", "decRateArcsecPerSec"},
}
"""`mount_action`'s per-`action` parameter set — every param here is required for its action
(none are optional), so this doubles as both the allowed and the required set. Exposed as a
module-level constant, rather than kept local to `mount_action`, so
`test_wrapper_tool_signature_matches_the_scripts_own_parameters` can keep cross-checking each
action's parameters against its underlying script's own declared parameters directly, the way
it did for the standalone wrapper tools this replaces — a single source of truth instead of a
second hardcoded expectations list drifting out of sync with either side.
"""


@mcp.tool()
async def mount_action(
    rig_id: str,
    action: Annotated[
        Literal[
            "park", "unpark", "slew", "track_off", "set_track_mode", "set_custom_tracking_rate"
        ],
        Field(
            json_schema_extra={
                "requiredParamsByAction": {
                    name: sorted(params) for name, params in _MOUNT_ACTION_PARAMS.items()
                }
            }
        ),
    ],
    ra: float | None = None,
    dec: float | None = None,
    modeSwitchElement: str | None = None,
    raRateArcsecPerSec: float | None = None,
    decRateArcsecPerSec: float | None = None,
) -> ScriptRunStarted:
    """Park, unpark, slew, stop tracking, or set the rig's mount's tracking mode/rate —
    replaces `park`/`unpark`/`slew`/`track_off`/`set_track_mode`/`set_custom_tracking_rate`
    (INDIMCP-116). Each action starts the built-in script of the same name (e.g.
    `action="park"` runs `scripts/park.yaml`) with exactly that action's own parameters.

    `ra`/`dec` (`ra` in hours, `dec` in degrees) are required for, and only valid with,
    `action="slew"` (INDIMCP-8) — slewing to a named object (e.g. "M101") isn't supported yet
    (INDIMCP-29). `modeSwitchElement` is required for, and only valid with,
    `action="set_track_mode"` (INDIMCP-49) — the INDI `TELESCOPE_TRACK_MODE` switch member to
    enable, e.g. `"TRACK_SIDEREAL"`, `"TRACK_SOLAR"`, `"TRACK_LUNAR"`, or `"TRACK_CUSTOM"`
    (pair the last with `action="set_custom_tracking_rate"` to also set a custom rate).
    `raRateArcsecPerSec`/`decRateArcsecPerSec` are required for, and only valid with,
    `action="set_custom_tracking_rate"` (INDIMCP-49), which also selects custom tracking on
    the mount. `park`/`unpark`/`track_off` (INDIMCP-48/49) take no parameters at all — any
    parameter above given alongside an action it doesn't belong to raises `ValueError`. Every
    parameter above is optional at the schema level regardless of `action`, since a required-
    ness that depends on a sibling field's value can't be expressed in JSON Schema — `action`'s
    own schema carries the real per-`action` required set under `requiredParamsByAction` for a
    schema-reading caller (see `_MOUNT_ACTION_PARAMS`).
    """
    given = {
        "ra": ra,
        "dec": dec,
        "modeSwitchElement": modeSwitchElement,
        "raRateArcsecPerSec": raRateArcsecPerSec,
        "decRateArcsecPerSec": decRateArcsecPerSec,
    }
    given_names = {name for name, value in given.items() if value is not None}
    allowed = _MOUNT_ACTION_PARAMS[action]
    if given_names != allowed:
        raise ValueError(
            f"action={action!r} requires exactly {sorted(allowed)}, got {sorted(given_names)}"
        )
    return await script_runs.start_script(action, rig_id, {name: given[name] for name in allowed})


_CAMERA_ACTION_ALLOWED_PARAMS: dict[str, set[str]] = {
    "cool": {"targetTempC", "timeoutSeconds"},
    "cooler_on": set(),
    "cooler_off": set(),
    "abort_exposure": set(),
    "capture_frame": {
        "exposureSeconds",
        "frameType",
        "binningX",
        "binningY",
        "gain",
        "offset",
        "frameX",
        "frameY",
        "frameWidth",
        "frameHeight",
        "location_id",
    },
}
_CAMERA_ACTION_REQUIRED_PARAMS: dict[str, set[str]] = {
    "cool": set(),
    "cooler_on": set(),
    "cooler_off": set(),
    "abort_exposure": set(),
    "capture_frame": {"exposureSeconds"},
}
"""`camera_action`'s per-`action` allowed/required parameter sets, exposed as module-level
constants for the same reason as `_MOUNT_ACTION_PARAMS` above. `cool`'s parameters are
optional at the tool-schema level (see `docs/ToolSurfaceRedesign.md`'s conditional-defaults
trade-off) but still real script parameters with real defaults once resolved — see
`_CAMERA_ACTION_RESOLVED_DEFAULTS` below for the values `test_wrapper_tool_signature_matches_
the_scripts_own_parameters` checks those resolve to.
"""

_CAMERA_ACTION_RESOLVED_DEFAULTS: dict[str, dict[str, Any]] = {
    "cool": {"targetTempC": -10, "timeoutSeconds": 300},
    "cooler_on": {},
    "cooler_off": {},
    "abort_exposure": {},
    "capture_frame": {"frameType": "Light", "binningX": 1, "binningY": 1},
}
"""The real default `camera_action` resolves each optional parameter to when omitted (matching
the old `cool_camera`/`capture_frame` tools' own defaults) — not visible in `camera_action`'s
own Python signature, where every parameter defaults to `None` as a sentinel meaning "omitted",
since the same parameter's real default differs by `action` (`docs/ToolSurfaceRedesign.md`'s
conditional-defaults trade-off). Exposed here purely so
`test_wrapper_tool_signature_matches_the_scripts_own_parameters` can still check these against
each script's own declared default, the same cross-check the old single-purpose wrapper tools
got for free from their real (non-sentinel) Python defaults.

Nested by `action` (matching `_CAMERA_ACTION_ALLOWED_PARAMS`/`_CAMERA_ACTION_REQUIRED_PARAMS`'s
own shape), not a single flat `{param_name: default}` map — a flat map would silently give the
wrong value if a future action ever reused one of these parameter names with a different
intended default, exactly the scenario this dict exists to handle correctly (PR review of #91).
"""


@mcp.tool()
async def camera_action(
    rig_id: str,
    action: Annotated[
        Literal["cool", "cooler_on", "cooler_off", "abort_exposure", "capture_frame"],
        Field(
            json_schema_extra={
                "requiredParamsByAction": {
                    name: sorted(params) for name, params in _CAMERA_ACTION_REQUIRED_PARAMS.items()
                }
            }
        ),
    ],
    targetTempC: float | None = None,
    timeoutSeconds: float | None = None,
    exposureSeconds: float | None = None,
    frameType: FrameType | None = None,
    binningX: int | None = None,
    binningY: int | None = None,
    gain: float | None = None,
    offset: float | None = None,
    frameX: int | None = None,
    frameY: int | None = None,
    frameWidth: int | None = None,
    frameHeight: int | None = None,
    location_id: str | None = None,
) -> ScriptRunStarted:
    """Cool the rig's camera, toggle its cooler, abort an in-progress exposure, or capture a
    frame — replaces `cool_camera`/`cooler_on`/`cooler_off`/`abort_exposure`/`capture_frame`
    (INDIMCP-116).

    `action="cool"` (INDIMCP-56) takes `targetTempC`/`timeoutSeconds`, both optional
    (defaulting to `-10`/`300`, same as the old `cool_camera` tool's own defaults — omitted
    here rather than given real parameter defaults, since a default only applies to this one
    action; see `docs/ToolSurfaceRedesign.md`'s conditional-defaults trade-off). `action=
    "cooler_on"`/`"cooler_off"`/`"abort_exposure"` (INDIMCP-84/86) take no parameters.
    `action="capture_frame"` (INDIMCP-44) requires `exposureSeconds`; `frameType`/`binningX`/
    `binningY` default to `"Light"`/`1`/`1` if omitted (same reasoning as `cool`'s defaults);
    `gain`/`offset` omitted leave the device's current setting alone rather than sending a
    fixed number; `frameX`/`frameY`/`frameWidth`/`frameHeight` default to the full sensor —
    set all four together for a sub-frame; `location_id` is passed straight through to
    `run_script` for this script's own celestial-context FITS header enrichment. Any parameter
    given alongside an action it doesn't belong to, or a required parameter missing for the
    action given, raises `ValueError`. Every parameter above is optional at the schema level
    regardless of `action` — `exposureSeconds` included, despite having no fallback if
    omitted for `action="capture_frame"` — since JSON Schema can't express a required-ness
    that depends on a sibling field's value; `action`'s own schema carries the real per-
    `action` required set under `requiredParamsByAction` for a schema-reading caller (see
    `_CAMERA_ACTION_REQUIRED_PARAMS`).
    """
    given = {
        "targetTempC": targetTempC,
        "timeoutSeconds": timeoutSeconds,
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
        "location_id": location_id,
    }
    given_names = {name for name, value in given.items() if value is not None}
    allowed = _CAMERA_ACTION_ALLOWED_PARAMS[action]
    required = _CAMERA_ACTION_REQUIRED_PARAMS[action]
    if not given_names <= allowed:
        raise ValueError(f"action={action!r} doesn't accept {sorted(given_names - allowed)}")
    if not required <= given_names:
        raise ValueError(f"action={action!r} requires {sorted(required)}")

    if action == "cool":
        defaults = _CAMERA_ACTION_RESOLVED_DEFAULTS["cool"]
        return await script_runs.start_script(
            "cool_camera",
            rig_id,
            {
                "targetTempC": targetTempC if targetTempC is not None else defaults["targetTempC"],
                "timeoutSeconds": (
                    timeoutSeconds if timeoutSeconds is not None else defaults["timeoutSeconds"]
                ),
            },
        )
    if action in ("cooler_on", "cooler_off", "abort_exposure"):
        return await script_runs.start_script(action, rig_id, {})

    defaults = _CAMERA_ACTION_RESOLVED_DEFAULTS["capture_frame"]
    return await script_runs.start_script(
        "capture_frame",
        rig_id,
        {
            "exposureSeconds": exposureSeconds,
            "frameType": frameType if frameType is not None else defaults["frameType"],
            "binningX": binningX if binningX is not None else defaults["binningX"],
            "binningY": binningY if binningY is not None else defaults["binningY"],
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
async def filter_wheel_action(
    rig_id: str, action: Literal["select"], filterName: str
) -> ScriptRunStarted:
    """Select a filter on the rig's filter wheel by name — replaces `select_filter`
    (INDIMCP-116, INDIMCP-61).

    Reconciles the rig's configured filter names against the driver's live state before
    selecting (adopts the driver's names if the rig has none configured, fails fatally on a
    real disagreement) — see `rig_diagnostics`'s `action="sync"` for how to resolve a
    disagreement explicitly. `action` only ever takes `"select"` today — kept as an explicit
    discriminator, matching every other `*_action` tool in this group, so a future filter
    wheel action (e.g. a `"home"` action) doesn't need another tool-surface change.
    """
    return await script_runs.start_script("select_filter", rig_id, {"filterName": filterName})


@mcp.tool()
async def focuser_action(
    rig_id: str, action: Literal["set_position"], position: int
) -> ScriptRunStarted:
    """Move the rig's focuser to an absolute position — replaces `set_focus_position`
    (INDIMCP-116, INDIMCP-62/63). Checked against the rig component's own `minPosition`/
    `maxPosition`, if declared. `action` only ever takes `"set_position"` today — kept as an
    explicit discriminator for the same reason as `filter_wheel_action`'s.
    """
    return await script_runs.start_script("set_focus_position", rig_id, {"position": position})


@mcp.tool()
async def set_connection(rig_id: str, role: str, connected: bool) -> ScriptRunStarted:
    """Connect or disconnect whichever device fills `role` in `rig_id` — replaces
    `connect`/`disconnect` (INDIMCP-116)."""
    return await script_runs.start_script(
        "connect" if connected else "disconnect", rig_id, {"role": role}
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

    `fitsDataBase64` is the FITS file's raw bytes, base64-encoded — MCP tool arguments are
    JSON, with no binary parameter type, so there's no way around base64 for this upload
    direction (unlike downloading a captured frame, INDIMCP-89, which streams raw bytes over
    plain HTTP instead).

    Saved into the frame store (`device="uploaded"`, no `run_id`) before solving, so it's
    retrievable afterward via `list_frames`'s returned `downloadUrl` like any other frame — WCS
    headers included if the solve succeeds, kept even if it doesn't, so a failed solve
    doesn't lose the upload.

    There's no rig/mount to derive a position or plate-scale hint from automatically (unlike
    plate-solving a rig's own camera via `run_script`/`manage_script_run` against the built-in
    `plate_solve_rig` script, which reads both off the rig's own configuration and the mount's
    live coordinates) — pass `raHintHours`/`decHintDeg` and/or `scaleLowArcsecPerPixel`/
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


_ASTROMETRY_INDEX_ACTION_ALLOWED_PARAMS: dict[str, set[str]] = {
    "list": {"rig_id", "minArcmin", "maxArcmin"},
    "download": {"indexNumbers", "rig_id", "minArcmin", "maxArcmin"},
}
"""Allowed selector parameters per `manage_astrometry_index` action — `catalog` is common to
both actions and always valid, so it's excluded from this map. Unlike `_MOUNT_ACTION_PARAMS`,
this doesn't double as a required set: each action's own rule is combinatorial (at most one
selector for `list`, exactly one for `download`), not a fixed required-parameter set, so it
can't be expressed via `requiredParamsByAction` schema metadata — the rule is documented in
`manage_astrometry_index`'s own docstring and enforced in its body instead.
"""


@mcp.tool()
async def manage_astrometry_index(
    action: Literal["list", "download"],
    catalog: astrometry_index.Catalog = "tycho2",
    indexNumbers: list[int] | None = None,
    rig_id: str | None = None,
    minArcmin: float | None = None,
    maxArcmin: float | None = None,
) -> list[astrometry_index.IndexFileStatus] | list[int]:
    """List or download astrometry.net index files for `catalog` under
    `INDI_MCP_ASTROMETRY_INDEX_DIR` (INDIMCP-77) — replaces `list_astrometry_index_files`/
    `download_astrometry_index_files` (INDIMCP-119) — see `docs/PlateSolve.md`.

    `catalog` is `"tycho2"` (wide fields, 22 arcmin-33deg) or `"2mass"` (the full 2-2000
    arcmin range) — the same two choices Ekos's own index-file downloader offers. Valid with
    either action.

    `action="list"`: returns every scale `catalog` publishes and whether it's installed. Pass
    **at most one** of `rig_id` or `minArcmin`/`maxArcmin` together to also get a `needed`
    flag per entry (`None` throughout otherwise) — this is also how to preview which files a
    field of view would need *without downloading anything*: pass the range (or a rig) here,
    then filter the result for `needed: true`; nothing is written to disk or fetched over the
    network for this action regardless. `rig_id` computes the field of view from that rig's
    own configured optics (its `telescope` component's `focalLengthMm` and `camera`
    component's `pixelSizeMicron`/`pixelsX`/`pixelsY`) and pads it by
    `astrometry_index.DEFAULT_RIG_MARGIN_SCALES` extra scales on each side, since a rig's
    computed field of view is only ever an estimate; `minArcmin`/`maxArcmin` given directly is
    used exactly as given, no padding. `needed` is `None` throughout if neither is given, or
    if `rig_id` is given but that rig doesn't have enough optics configured to compute a field
    of view from. `indexNumbers` isn't a valid parameter with this action. Raises `ValueError`
    if both `rig_id` and an arcmin range are given, or if only one of `minArcmin`/`maxArcmin`
    is given.

    `action="download"`: downloads whichever index files aren't already installed and returns
    the scale numbers that had at least one file actually downloaded (already-fully-installed
    ones are left alone). Pass **exactly one** of: `indexNumbers` (explicit scale numbers
    `catalog` publishes), `minArcmin`/`maxArcmin` together (every scale covering that exact
    field-of-view range), or `rig_id` (computes `minArcmin`/`maxArcmin` from that rig's own
    configured optics the same way `action="list"`'s `needed` does, then pads the same way) —
    rejected if none or more than one is given, rather than silently prioritizing one, so a
    call that accidentally passes two (e.g. `rig_id` alongside explicit `indexNumbers`) fails
    loudly instead of quietly ignoring one of them. Raises `ValueError` if selectors are
    miscombined, or if no known `catalog` scale covers a derived arcmin range.

    Each action only accepts its own selector parameters (`_ASTROMETRY_INDEX_ACTION_
    ALLOWED_PARAMS`) — passing a parameter the other action doesn't recognize (e.g.
    `indexNumbers` with `action="list"`) raises `ValueError`.
    """
    given_names = {
        name
        for name, value in {
            "indexNumbers": indexNumbers,
            "rig_id": rig_id,
            "minArcmin": minArcmin,
            "maxArcmin": maxArcmin,
        }.items()
        if value is not None
    }
    disallowed = given_names - _ASTROMETRY_INDEX_ACTION_ALLOWED_PARAMS[action]
    if disallowed:
        raise ValueError(f"action={action!r} does not accept {sorted(disallowed)}")

    if action == "list":
        if rig_id is not None and (minArcmin is not None or maxArcmin is not None):
            raise ValueError("pass rig_id or minArcmin/maxArcmin, not both")
        if (minArcmin is None) != (maxArcmin is None):
            raise ValueError("pass both minArcmin and maxArcmin together, not just one")
        rig = rig_store.get_rig(rig_id) if rig_id is not None else None
        return await asyncio.to_thread(
            astrometry_index.list_index_files,
            catalog=catalog,
            rig=rig,
            min_arcmin=minArcmin,
            max_arcmin=maxArcmin,
        )

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
async def manage_script_run(
    run_id: str, action: Literal["status", "cancel", "pause", "resume"]
) -> ScriptRunStatus | ScriptRunPaused | ScriptRunResumed | ScriptRunPauseRejected:
    """Check status, cancel, pause, or resume a run started by `run_script` — replaces
    `get_script_status`/`cancel_script`/`pause_script`/`resume_script` (INDIMCP-117).

    `action="status"` returns the most recently known status for the run. `action="cancel"`
    waits for the run to actually stop, and always applies regardless of whether the run is
    pausable — unlike `action="pause"`/`"resume"`. `action="pause"` pauses the run at its next
    safe point, only if its script declared itself `pausable` — rejected (not queued or
    silently ignored) if the script has no safe point to suspend at.
    `action="resume"` resumes a run previously paused with `action="pause"`.
    """
    if action == "status":
        return script_runs.get_script_status(run_id)
    if action == "cancel":
        return await script_runs.cancel_script(run_id)
    if action == "pause":
        return script_runs.pause_script(run_id)
    return script_runs.resume_script(run_id)


_CALIBRATION_SWEEP_ALLOWED_PARAMS: dict[str, set[str]] = {
    "sensor": {
        "gains",
        "offsets",
        "flatExposureSecondsList",
        "biasCount",
        "darkCount",
        "biasExposureSeconds",
        "location_id",
    },
    "flat": {
        "gains",
        "offsets",
        "exposureSecondsList",
        "filterName",
        "focusPosition",
        "count",
        "location_id",
    },
}
_CALIBRATION_SWEEP_REQUIRED_PARAMS: dict[str, set[str]] = {
    "sensor": {"gains", "offsets", "flatExposureSecondsList", "biasCount", "darkCount"},
    "flat": {"gains", "offsets", "exposureSecondsList", "filterName", "focusPosition", "count"},
}
"""`run_calibration_sweep`'s per-`kind` allowed/required parameter sets — `biasExposureSeconds`
(sensor, defaults to `0.0`) and `location_id` (both kinds) are the only optional entries;
everything else is required for its own `kind`. Exposed as module-level constants for the same
reason as `_MOUNT_ACTION_PARAMS`/`_CAMERA_ACTION_ALLOWED_PARAMS` (INDIMCP-116): a single source
of truth for validation, not duplicated in a test's own expectations list.
"""


@mcp.tool()
async def run_calibration_sweep(
    kind: Literal["sensor", "flat"],
    rig_id: str,
    gains: list[float] | None = None,
    offsets: list[float] | None = None,
    flatExposureSecondsList: list[float] | None = None,
    biasCount: int | None = None,
    darkCount: int | None = None,
    biasExposureSeconds: float | None = None,
    exposureSecondsList: list[float] | None = None,
    filterName: str | None = None,
    focusPosition: int | None = None,
    count: int | None = None,
    location_id: str | None = None,
) -> SensorCalibrationSweepStarted | FlatCalibrationSweepStarted:
    """Run a sensor (bias + flat-dark) or flat calibration sweep across a cartesian product of
    gain/offset/exposure settings, returning immediately with a `sweepId` — replaces
    `run_sensor_calibration_sweep`/`run_flat_calibration_sweep` (INDIMCP-118).

    `kind="sensor"` (INDIMCP-102) runs `capture_sensor_calibration_set` (bias + flat-dark) once
    per (`gains`, `offsets`, `flatExposureSecondsList`) combination; `biasCount`/`darkCount`
    are required, `biasExposureSeconds` defaults to `0.0` if omitted. Does not stage a flat
    panel or capture flats itself — this is the bias/flat-dark half of a calibration set only;
    the flat side is `kind="flat"`. `kind="flat"` (INDIMCP-103) runs `capture_flat_sequence`
    once per (`gains`, `offsets`, `exposureSecondsList`) combination; `filterName`/
    `focusPosition`/`count` are required and shared across every combination. Assumes the flat
    panel is already staged before this is called — this tool has no way to prompt for or
    verify that; the caller (typically a client app, having confirmed with its human operator)
    is responsible for staging it first.

    `gains`/`offsets` are required for both kinds. List-valued arguments are needed because a
    script's own `parameters` can't carry list-valued inputs — see `docs/SensorCalibration.md`
    for why this is a dedicated tool rather than a script step; every list argument must be
    non-empty. Never blocks until the sweep finishes — a full sweep can run far longer than any
    single script (many combinations, each a real capture sequence) — poll
    `manage_calibration_sweep`'s `action="status"` for progress and the eventual terminal
    outcome, or `action="cancel"` to stop it early. Any parameter given that doesn't belong to
    `kind`, or a required parameter missing for it, raises `ValueError`.
    """
    given = {
        "gains": gains,
        "offsets": offsets,
        "flatExposureSecondsList": flatExposureSecondsList,
        "biasCount": biasCount,
        "darkCount": darkCount,
        "biasExposureSeconds": biasExposureSeconds,
        "exposureSecondsList": exposureSecondsList,
        "filterName": filterName,
        "focusPosition": focusPosition,
        "count": count,
        "location_id": location_id,
    }
    given_names = {name for name, value in given.items() if value is not None}
    allowed = _CALIBRATION_SWEEP_ALLOWED_PARAMS[kind]
    required = _CALIBRATION_SWEEP_REQUIRED_PARAMS[kind]
    if not given_names <= allowed:
        raise ValueError(f"kind={kind!r} doesn't accept {sorted(given_names - allowed)}")
    if not required <= given_names:
        raise ValueError(f"kind={kind!r} requires {sorted(required)}")

    if kind == "sensor":
        # Reached only when kind="sensor" and the required-params check above passed, so
        # every one of these is guaranteed non-None. Asserted here purely for the type
        # checker, matching manage_astrometry_index's own convention.
        assert gains is not None
        assert offsets is not None
        assert flatExposureSecondsList is not None
        assert biasCount is not None
        assert darkCount is not None
        return await sensor_calibration_sweep.start_sweep(
            rig_id,
            gains,
            offsets,
            flatExposureSecondsList,
            biasCount,
            darkCount,
            bias_exposure_seconds=(biasExposureSeconds if biasExposureSeconds is not None else 0.0),
            location_id=location_id,
        )

    # Reached only when kind="flat" and the required-params check above passed, so every
    # one of these is guaranteed non-None. Asserted here purely for the type checker,
    # matching manage_astrometry_index's own convention.
    assert gains is not None
    assert offsets is not None
    assert exposureSecondsList is not None
    assert filterName is not None
    assert focusPosition is not None
    assert count is not None
    return await flat_calibration_sweep.start_sweep(
        rig_id,
        gains,
        offsets,
        exposureSecondsList,
        filterName,
        focusPosition,
        count,
        location_id=location_id,
    )


@mcp.tool()
async def manage_calibration_sweep(
    sweep_id: str, action: Literal["status", "cancel"]
) -> SensorCalibrationSweepStatus | FlatCalibrationSweepStatus:
    """Check status or cancel a calibration sweep started by `run_calibration_sweep` — replaces
    `get_sensor_calibration_sweep_status`/`cancel_sensor_calibration_sweep`/
    `get_flat_calibration_sweep_status`/`cancel_flat_calibration_sweep` (INDIMCP-118).

    Whether `sweep_id` belongs to a sensor or flat sweep is resolved via each tracker's own
    `sweep_exists` — a sweep id is only ever registered in one of the two, never both — rather
    than by triggering and catching a not-found error, so this doesn't depend on
    `get_sweep_status`/`cancel_sweep` never raising `ValueError` for any other reason.
    `action="cancel"` waits for the sweep to actually stop, cancelling whichever combination's
    capture run is currently in flight (if any) rather than letting it finish first. Raises
    `ValueError` if `sweep_id` isn't recognized by either tracker.
    """
    if sensor_calibration_sweep.sweep_exists(sweep_id):
        tracker = sensor_calibration_sweep
    elif flat_calibration_sweep.sweep_exists(sweep_id):
        tracker = flat_calibration_sweep
    else:
        raise ValueError(f"no calibration sweep found for sweepId {sweep_id!r}")

    if action == "status":
        return tracker.get_sweep_status(sweep_id)
    return await tracker.cancel_sweep(sweep_id)


@mcp.resource("indi://mcp-server/scripts", mime_type="application/json")
def read_script_event_stream() -> dict[str, list[ScriptRunStatus]]:
    """The rolling window of recent scripting-layer events, newest first.

    Same subscription mechanism and best-effort caveat as
    `read_indi_message_stream` — see `docs/Design.md#event-streams`. Renamed from the
    top-level `indi://scripts` by INDIMCP-57 — see `event_streams`'s module docstring.
    """
    return cast(dict[str, list[ScriptRunStatus]], event_streams.read_scripts())


@mcp.resource("indi://mcp-server/scripts/{runId}", mime_type="application/json")
def read_script_event_stream_for_run(runId: str) -> dict[str, list[ScriptRunStatus]]:
    """Same as `read_script_event_stream`, scoped to events from one `runId`.

    `runId` is unquoted before filtering — see `read_indi_message_stream_for_device`.
    """
    return cast(dict[str, list[ScriptRunStatus]], event_streams.read_scripts(unquote(runId)))


@mcp.resource("indi://mcp-server/connection", mime_type="application/json")
def read_connection_event_stream() -> dict[str, list[ConnectionEvent]]:
    """The rolling window of recent connection-lifecycle events, newest first (INDIMCP-57).

    Covers three kinds of connection: this server's own TCP link to `indiserver`
    (`target="server"`), the `indiserver` process itself (`target="indiserver"`), and
    individual driver processes (`target=<driver label>`). Same subscription mechanism and
    best-effort caveat as `read_indi_message_stream` — see `docs/Design.md#event-streams`.
    """
    return cast(dict[str, list[ConnectionEvent]], event_streams.read_connection())


@mcp.resource("indi://mcp-server/connection/{target}", mime_type="application/json")
def read_connection_event_stream_for_target(target: str) -> dict[str, list[ConnectionEvent]]:
    """Same as `read_connection_event_stream`, scoped to events for one `target`.

    `target` is unquoted before filtering — see `read_indi_message_stream_for_device`.
    """
    return cast(dict[str, list[ConnectionEvent]], event_streams.read_connection(unquote(target)))


@mcp.tool()
async def get_events(
    stream: event_log.Stream,
    device: str | None = None,
    run_id: str | None = None,
    target: str | None = None,
    since: str | None = None,
) -> list[event_log.EventRecord]:
    """Catch up on missed `indi://messages`/`indi://mcp-server` events from the durable event log.

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
    first — the natural order for replaying missed events. `target` filters
    the `connection` stream (INDIMCP-57) the same way `device`/`run_id`
    filter `messages`/`scripts`.
    """
    return await asyncio.to_thread(
        event_log.get_events, stream, device=device, run_id=run_id, target=target, since=since
    )


class FrameMetadataResponse(FrameMetadata):
    """`FrameMetadata` plus `downloadUrl`/`issues` — what `list_frames`/`get_frame_metadata`
    actually return to a client (INDIMCP-89). `downloadUrl` is computed per response, not
    stored, since it depends on the server's own current transport/host/port, not anything
    about the frame itself — see `_frame_download_url`. `issues` surfaces conditions about this
    particular frame's metadata the client should know about — currently only a missing
    `checksumSha256` (INDIMCP-95 predates the frame), reported as a `frameChecksumMissing`
    `WARNING` — reusing `issues.Issue` rather than inventing a second ad hoc warning shape.
    Populated identically by both `list_frames` and `get_frame_metadata`, since both return
    this same shape; see `_frame_issues`.
    """

    downloadUrl: str | None
    issues: list[Issue]


def _frame_issues(metadata: FrameMetadata) -> list[Issue]:
    """`Issue`s about `metadata` itself, not about the request that fetched it.

    `checksumSha256` is `None` only for a frame captured before checksum support existed
    (INDIMCP-95) — see `FrameMetadata.checksumSha256`'s own docstring — so that's the one
    condition worth flagging: `WARNING`, not `ERROR`/`FATAL`, since the frame itself is fine,
    just not verifiable by hash.
    """
    if metadata["checksumSha256"] is not None:
        return []
    return [
        {
            "kind": "issue",
            "severity": Severity.WARNING,
            "code": "frameChecksumMissing",
            "message": (
                f"frame {metadata['frameId']!r} has no checksumSha256 — it was captured "
                "before checksum support existed and cannot be integrity-checked"
            ),
            "role": None,
            "device": metadata["device"],
        }
    ]


def _frame_download_url(frame_id: str) -> str | None:
    """The URL a LAN client can `GET` to download `frame_id`'s raw bytes, or `None`.

    `None` whenever there's no HTTP listener to point at at all — running under `stdio`
    (`_current_transport`), or before `run()` has set it. Built from `socket.gethostname()`
    rather than `mcp.settings.host`: the latter is the server's own *bind* address, which in
    production (`docs/Deployment.md`) is the wildcard `0.0.0.0` — not itself a reachable
    address for a client to connect back to. `frame_id` is a `uuid4` in practice
    (`frame_store.save_frame`) so this quoting is defensive, not load-bearing.
    """
    if _current_transport in (None, "stdio"):
        return None
    return f"http://{socket.gethostname()}:{mcp.settings.port}/frames/{quote(frame_id, safe='')}"


def _to_frame_response(metadata: FrameMetadata) -> FrameMetadataResponse:
    return {
        **metadata,
        "downloadUrl": _frame_download_url(metadata["frameId"]),
        "issues": _frame_issues(metadata),
    }


_FRAMES_ALLOWED_PARAMS: dict[str, set[str]] = {
    "list": {"run_id", "device", "since", "transferred"},
    "get": {"frame_id"},
}
_FRAMES_REQUIRED_PARAMS: dict[str, set[str]] = {
    "list": set(),
    "get": {"frame_id"},
}
"""`frames`'s per-`action` allowed/required parameter sets — every `action="list"` filter is
optional; `frame_id` is the only, required, parameter for `action="get"`. Exposed as
module-level constants for the same reason as `_MOUNT_ACTION_PARAMS` (INDIMCP-116).
"""


@mcp.tool()
async def frames(
    action: Annotated[
        Literal["list", "get"],
        Field(
            json_schema_extra={
                "requiredParamsByAction": {
                    name: sorted(params) for name, params in _FRAMES_REQUIRED_PARAMS.items()
                }
            }
        ),
    ],
    frame_id: str | None = None,
    run_id: str | None = None,
    device: str | None = None,
    since: str | None = None,
    transferred: bool | None = None,
) -> list[FrameMetadataResponse] | FrameMetadataResponse:
    """List captured frame metadata, or fetch metadata for a single frame — replaces
    `list_frames`/`get_frame_metadata` (INDIMCP-120).

    `action="list"` returns every frame's metadata, most recently captured first, filtered by
    `run_id`/`device`/`since`/`transferred` (all optional). `transferred` is a tri-state:
    omitted/`None` returns every frame, `true` only ones already confirmed received
    (`manage_frame`'s `action="confirm_transfer"`), `false` only ones still waiting to be
    retrieved — useful for checking what's left to download before `manage_frame`'s
    `action="purge"`. `action="get"` requires `frame_id`, and rejects the list filters. Never
    returns a frame's on-disk path; each frame's `downloadUrl` is a `GET`-able HTTP URL for its
    raw bytes (INDIMCP-89), `None` if this server has no HTTP listener to build one from
    (`stdio` transport). See `FrameMetadataResponse` for `issues`. Any parameter given
    alongside an action it doesn't belong to, or a required parameter missing for it, raises
    `ValueError`. `frame_id` is optional at the schema level regardless of `action`, despite
    having no fallback if omitted for `action="get"` — `action`'s own schema carries the real
    per-`action` required set under `requiredParamsByAction` for a schema-reading caller (see
    `_FRAMES_REQUIRED_PARAMS`).
    """
    given = {
        "frame_id": frame_id,
        "run_id": run_id,
        "device": device,
        "since": since,
        "transferred": transferred,
    }
    given_names = {name for name, value in given.items() if value is not None}
    allowed = _FRAMES_ALLOWED_PARAMS[action]
    required = _FRAMES_REQUIRED_PARAMS[action]
    if not given_names <= allowed:
        raise ValueError(f"action={action!r} doesn't accept {sorted(given_names - allowed)}")
    if not required <= given_names:
        raise ValueError(f"action={action!r} requires {sorted(required)}")

    if action == "get":
        # Reached only when action="get" and the required-params check above passed, so
        # frame_id is guaranteed non-None. Asserted here purely for the type checker,
        # matching manage_astrometry_index's own convention.
        assert frame_id is not None
        metadata = await asyncio.to_thread(frame_store.get_frame_metadata, frame_id)
        return _to_frame_response(metadata)

    metadata_list = await asyncio.to_thread(
        frame_store.list_frames, run_id=run_id, device=device, since=since, transferred=transferred
    )
    return [_to_frame_response(m) for m in metadata_list]


_MANAGE_FRAME_ALLOWED_PARAMS: dict[str, set[str]] = {
    "confirm_transfer": {"frame_id"},
    "delete": {"frame_id", "require_transferred"},
    "purge": {"older_than_days"},
}
_MANAGE_FRAME_REQUIRED_PARAMS: dict[str, set[str]] = {
    "confirm_transfer": {"frame_id"},
    "delete": {"frame_id"},
    "purge": {"older_than_days"},
}
"""`manage_frame`'s per-`action` allowed/required parameter sets — `require_transferred`
(`action="delete"` only) is the sole optional entry, defaulting to `True` if omitted (matching
the old `delete_frame` tool's own default; see `docs/ToolSurfaceRedesign.md`'s
conditional-defaults trade-off). Exposed as module-level constants for the same reason as
`_MOUNT_ACTION_PARAMS` (INDIMCP-116).
"""


@mcp.tool()
async def manage_frame(
    action: Annotated[
        Literal["confirm_transfer", "delete", "purge"],
        Field(
            json_schema_extra={
                "requiredParamsByAction": {
                    name: sorted(params) for name, params in _MANAGE_FRAME_REQUIRED_PARAMS.items()
                }
            }
        ),
    ],
    frame_id: str | None = None,
    require_transferred: bool | None = None,
    older_than_days: float | None = None,
) -> FrameMetadata | list[FrameMetadata]:
    """Confirm a frame's transfer, delete a single frame, or bulk-purge already-transferred
    frames — replaces `confirm_frame_transfer`/`delete_frame`/`purge_transferred_frames`
    (INDIMCP-120).

    `action="confirm_transfer"` requires `frame_id` and sets `transferredAt` — call this only
    after actually verifying the bytes downloaded via `frames`'s `downloadUrl` were received
    intact; this is what makes a frame eligible for `action="delete"`/`"purge"` later, so
    confirming a transfer that didn't really complete risks losing the only copy of that
    frame. `action="delete"` requires `frame_id`, deletes a single frame's file and metadata,
    and returns its metadata as it was — refuses to delete a frame that hasn't been confirmed
    transferred yet unless `require_transferred` is explicitly set to `false` (defaults to
    `true` if omitted) — this is a destructive action on the actual science data this server
    exists to capture, so it's safe by default rather than trusting every caller to check
    first. `action="purge"` requires `older_than_days`, bulk-deletes every already-transferred
    frame captured more than that many days ago, and returns the metadata of every frame
    actually deleted — never runs automatically, this is the only way old frames get cleaned
    up, since the INDI Device's own storage is limited; only ever considers frames already
    confirmed transferred, regardless of age, so a frame the Client Computer hasn't confirmed
    receiving yet is never deleted by this call. Any parameter given alongside an action it
    doesn't belong to, or a required parameter missing for it, raises `ValueError`. `frame_id`/
    `older_than_days` are optional at the schema level regardless of `action`, despite having
    no fallback if omitted for the actions that need them — `action`'s own schema carries the
    real per-`action` required set under `requiredParamsByAction` for a schema-reading caller
    (see `_MANAGE_FRAME_REQUIRED_PARAMS`).
    """
    given = {
        "frame_id": frame_id,
        "require_transferred": require_transferred,
        "older_than_days": older_than_days,
    }
    given_names = {name for name, value in given.items() if value is not None}
    allowed = _MANAGE_FRAME_ALLOWED_PARAMS[action]
    required = _MANAGE_FRAME_REQUIRED_PARAMS[action]
    if not given_names <= allowed:
        raise ValueError(f"action={action!r} doesn't accept {sorted(given_names - allowed)}")
    if not required <= given_names:
        raise ValueError(f"action={action!r} requires {sorted(required)}")

    # Reached only when the required-params check above passed for the given action, so
    # frame_id/older_than_days are guaranteed non-None below wherever each is used.
    # Asserted here purely for the type checker, matching manage_astrometry_index's
    # own convention.
    if action == "confirm_transfer":
        assert frame_id is not None
        return await asyncio.to_thread(frame_store.confirm_frame_transfer, frame_id)

    if action == "delete":
        assert frame_id is not None
        return await asyncio.to_thread(
            frame_store.delete_frame,
            frame_id,
            require_transferred=(require_transferred if require_transferred is not None else True),
        )

    assert older_than_days is not None
    return await asyncio.to_thread(
        frame_store.purge_transferred_frames, older_than=timedelta(days=older_than_days)
    )


@mcp.custom_route("/frames/{frameId}", methods=["GET"])
async def download_frame(request: Request) -> Response:
    """Stream a captured frame's raw bytes over plain HTTP — INDIMCP-89, replacing the old
    `frame://{frameId}` MCP resource.

    That resource read the whole frame into memory and returned it as one base64-encoded
    blob in a single JSON-RPC response — a ~17-23MB dark frame disconnected a real MCP client
    reading it (INDIMCP-89), since the MCP resource protocol has no chunked/range-read
    primitive to fall back on. `FileResponse` here streams straight from disk in normal-sized
    chunks instead, with no base64 inflation, so frame size stops being a protocol-level
    scalability wall. Registered via `custom_route` rather than `@mcp.tool()`/`@mcp.resource()`
    — this is a plain Starlette route living on the same host/port as the MCP endpoint itself,
    outside the MCP session/protocol entirely (see `mcp.server.fastmcp.FastMCP.custom_route`).

    Unauthenticated, same as every other tool/resource this server exposes (see
    `docs/Deployment.md`'s Hardening notes) — this doesn't introduce a new class of exposure,
    just a new URL path with the same trust model already accepted for the whole server.
    """
    frame_id = request.path_params["frameId"]
    try:
        path = await asyncio.to_thread(frame_store.get_frame_path, frame_id)
    except frame_store.FrameNotFoundError:
        return PlainTextResponse("Frame not found", status_code=404)
    return FileResponse(path, media_type="application/octet-stream", filename=path.name)


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_current_transport: Transport | None = None
"""Set by `run()` — lets `_frame_download_url` tell whether an HTTP listener exists at all."""


def run(transport: Transport = "stdio", host: str = "127.0.0.1", port: int = 8000) -> None:
    """Start serving the MCP server over the given transport.

    `host`/`port` only apply to the `sse` and `streamable-http` transports.
    """
    global _current_transport
    logging.basicConfig(level=logging.INFO)
    _current_transport = transport
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
