"""The INDI MCP server instance and its entrypoint."""

import logging
from typing import Literal

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

mcp = FastMCP(
    name="indi-mcp",
    instructions=(
        "Controls astrophotography equipment via INDI: manage the INDI server "
        "and its drivers, send and receive INDI messages, and run capture scripts."
    ),
)

Transport = Literal["stdio", "sse", "streamable-http"]


def run(transport: Transport = "stdio") -> None:
    """Start serving the MCP server over the given transport."""
    logging.basicConfig(level=logging.INFO)
    logger.info("Starting indi-mcp server (transport=%s)", transport)
    mcp.run(transport=transport)
