"""Management of the `indiserver` process.

`indiserver` is launched directly here rather than through indiweb's
`IndiServer.start()`, because that method hardcodes a `-u <socket>` flag that
this INDI build (the same version used on the target Raspberry Pi) does not
support: it fails argument parsing and exits immediately, regardless of
whether any driver is given. Stopping and status-checking still delegate to
indiweb's `IndiServer`, since those paths are psutil-based and don't depend
on `-u`.
"""

import asyncio
import logging
import threading
from subprocess import call
from typing import TypedDict

from indiweb.async_system_command import AsyncSystemCommand
from indiweb.indi_server import INDI_FIFO, INDI_PORT
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
_async_cmd: AsyncSystemCommand | None = None


class IndiServerStatus(TypedDict):
    """Current state of the managed `indiserver` process."""

    running: bool
    port: int


def _clear_fifo(fifo: str = INDI_FIFO) -> None:
    call(["rm", "-f", fifo])
    call(["mkfifo", fifo])


def _launch(port: int, fifo: str = INDI_FIFO) -> AsyncSystemCommand:
    """Start `indiserver` in the background and return its async command handle."""
    cmd = f"indiserver -p {port} -m 1000 -v -f {fifo} > /tmp/indiserver.log 2>&1"
    logger.info(cmd)
    async_cmd = AsyncSystemCommand(cmd)
    threading.Thread(target=async_cmd.run, daemon=True).start()
    return async_cmd


async def start_server(port: int = INDI_PORT) -> IndiServerStatus:
    """Start `indiserver` on the given port, restarting it if already running."""
    global _current_port, _async_cmd
    if (await get_status())["running"]:
        await stop_server()
    logger.info("Starting indiserver on port %d", port)
    await asyncio.to_thread(_clear_fifo)
    _async_cmd = await asyncio.to_thread(_launch, port)
    _current_port = port
    return await get_status()


async def stop_server() -> IndiServerStatus:
    """Stop `indiserver`."""
    global _async_cmd
    logger.info("Stopping indiserver on port %d", _current_port)
    await asyncio.to_thread(_server.stop, _current_port)
    if _async_cmd is not None:
        await asyncio.to_thread(_async_cmd.terminate)
        _async_cmd = None
    return await get_status()


async def restart_server(port: int | None = None) -> IndiServerStatus:
    """Restart `indiserver`, keeping its current port unless a new one is given."""
    effective_port = port if port is not None else _current_port
    logger.info("Restarting indiserver on port %d", effective_port)
    await stop_server()
    return await start_server(effective_port)


async def get_status() -> IndiServerStatus:
    """Report whether `indiserver` is running, and on which port."""
    running = await asyncio.to_thread(_server.is_running, _current_port)
    return {"running": running, "port": _current_port}
