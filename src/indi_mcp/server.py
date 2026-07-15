"""The INDI MCP server instance and its entrypoint."""

import logging
from typing import Literal

from mcp.server.fastmcp import FastMCP

from indi_mcp import indi_server
from indi_mcp.indi_server import INDI_PORT, IndiServerStatus

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


def run(transport: Transport = "stdio") -> None:
    """Start serving the MCP server over the given transport."""
    logging.basicConfig(level=logging.INFO)
    logger.info("Starting indi-mcp server (transport=%s)", transport)
    mcp.run(transport=transport)
