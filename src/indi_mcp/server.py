"""The INDI MCP server instance and its entrypoint."""

import logging
from typing import Literal

from mcp.server.fastmcp import FastMCP

from indi_mcp import indi_driver, indi_messaging, indi_server, rig_store
from indi_mcp.indi_driver import DriverInfo, DriverStatus
from indi_mcp.indi_messaging import IndiEvent, MessagingStatus
from indi_mcp.indi_server import INDI_PORT, IndiServerStatus
from indi_mcp.rig_store import DraftDeviceInfo, Rig, RigCheck, RigDraft, RigSuggestion, RigSummary

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
def save_rig(rig: Rig, overwrite: bool = False) -> Rig:
    """Save a rig definition — hand-authored, or completed from a `draft_rig` result.

    Writes `rig` to `rigs/<rig.id>.yaml` and reloads it so it's immediately
    available by `id` to `get_rig`/`suggest_rig`/`check_rig`. Refuses to
    replace an existing rig file unless `overwrite` is set, since reusing an
    `id` could otherwise silently destroy a previously saved rig.
    """
    return rig_store.save_rig(rig, overwrite=overwrite)


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


def run(transport: Transport = "stdio", host: str = "127.0.0.1", port: int = 8000) -> None:
    """Start serving the MCP server over the given transport.

    `host`/`port` only apply to the `sse` and `streamable-http` transports.
    """
    logging.basicConfig(level=logging.INFO)
    rig_store.load_rigs()
    if transport != "stdio":
        mcp.settings.host = host
        mcp.settings.port = port
        logger.info(
            "Starting indi-mcp server (transport=%s, host=%s, port=%d)", transport, host, port
        )
    else:
        logger.info("Starting indi-mcp server (transport=%s)", transport)
    mcp.run(transport=transport)
