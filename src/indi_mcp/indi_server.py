"""Management of the `indiserver` process, backed by indiweb's IndiServer."""

import asyncio
import logging
from typing import TypedDict

from indiweb.indi_server import INDI_PORT
from indiweb.indi_server import IndiServer as _IndiServer

logger = logging.getLogger(__name__)

__all__ = [
    "INDI_PORT",
    "IndiServerStatus",
    "get_status",
    "restart_server",
    "start_server",
    "stop_server",
]

_server = _IndiServer()
_current_port = INDI_PORT


class IndiServerStatus(TypedDict):
    """Current state of the managed `indiserver` process."""

    running: bool
    port: int


async def start_server(port: int = INDI_PORT) -> IndiServerStatus:
    """Start `indiserver` on the given port, restarting it if already running."""
    global _current_port
    logger.info("Starting indiserver on port %d", port)
    await asyncio.to_thread(_server.start, port)
    _current_port = port
    return await get_status()


async def stop_server() -> IndiServerStatus:
    """Stop `indiserver`."""
    logger.info("Stopping indiserver on port %d", _current_port)
    await asyncio.to_thread(_server.stop, _current_port)
    return await get_status()


async def restart_server(port: int | None = None) -> IndiServerStatus:
    """Restart `indiserver`, keeping its current port unless a new one is given."""
    effective_port = port if port is not None else _current_port
    logger.info("Restarting indiserver on port %d", effective_port)
    await asyncio.to_thread(_server.stop, _current_port)
    return await start_server(effective_port)


async def get_status() -> IndiServerStatus:
    """Report whether `indiserver` is running, and on which port."""
    running = await asyncio.to_thread(_server.is_running, _current_port)
    return {"running": running, "port": _current_port}
