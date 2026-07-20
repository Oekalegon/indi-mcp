"""The INDI MCP server instance and its entrypoint."""

import asyncio
import logging
from datetime import timedelta
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from indi_mcp import (
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

mcp = FastMCP(
    name="indi-mcp",
    instructions=(
        "Controls astrophotography equipment via INDI: manage the INDI server "
        "and its drivers, send and receive INDI messages, and run capture scripts."
    ),
)

Transport = Literal["stdio", "sse", "streamable-http"]


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
    script_id: str, rig_id: str, parameters: dict[str, Any] | None = None
) -> ScriptRunStarted:
    """Start `script_id` running against `rig_id`, returning immediately with a `runId`.

    Scripts run long sequences against physical hardware and are meant to
    keep going even if the caller disconnects, so this never blocks until
    the script finishes (see `docs/Design.md#calling-scripts-and-script-
    results`) — poll `get_script_status(runId)` for progress and the
    eventual `scriptCompleted`/`scriptFailed` outcome, or use
    `cancel_script`/`pause_script`/`resume_script` to control the run.
    """
    return await script_runs.start_script(script_id, rig_id, parameters)


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
async def delete_frame(frame_id: str, require_transferred: bool = True) -> None:
    """Delete a single captured frame's file and metadata.

    Refuses to delete a frame that hasn't been confirmed transferred yet
    (via `confirm_frame_transfer`) unless `require_transferred` is
    explicitly set to `false` — this is a destructive action on the
    actual science data this server exists to capture, so it's safe by
    default rather than trusting every caller to check first.
    """
    await asyncio.to_thread(
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
        logger.info(
            "Starting indi-mcp server (transport=%s, host=%s, port=%d)", transport, host, port
        )
    else:
        logger.info("Starting indi-mcp server (transport=%s)", transport)
    mcp.run(transport=transport)
