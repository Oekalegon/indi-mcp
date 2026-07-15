"""Management of INDI drivers: the on-disk catalog and running instances.

Driver start/stop delegates to the shared `IndiServer` instance in
`indi_server` (`indi_server._server`), since running drivers are tracked on
that instance and commands are written to the same `indiserver` FIFO used to
start/stop the server itself.
"""

import asyncio
import logging
import os
import shutil
from pathlib import Path
from typing import TypedDict

from indiweb.driver import DeviceDriver, DriverCollection

from indi_mcp import indi_server

logger = logging.getLogger(__name__)

__all__ = [
    "DriverInfo",
    "DriverStatus",
    "get_driver_catalog",
    "list_running_drivers",
    "start_driver",
    "stop_driver",
]

_catalog: DriverCollection | None = None


class DriverInfo(TypedDict):
    """A driver known to the INDI driver catalog, whether or not it is running."""

    name: str
    label: str
    version: str
    family: str
    binary: str
    installed: bool


class DriverStatus(TypedDict):
    """Current run state of a single driver."""

    label: str
    running: bool


def _get_catalog() -> DriverCollection:
    global _catalog
    if _catalog is None:
        _catalog = DriverCollection()
    return _catalog


def _find_driver(label: str) -> DeviceDriver:
    driver = _get_catalog().by_label(label)
    if driver is None:
        raise ValueError(f"Unknown driver: {label!r}")
    return driver


def _is_binary_installed(binary: str) -> bool:
    """Check whether a driver's binary is actually present and executable.

    Catalog entries come from XML shipped by driver packages that may not be
    installed (e.g. indi-full extras), so the binary they reference can be
    missing even though the entry is listed.
    """
    if not binary:
        return False
    if Path(binary).is_absolute():
        return os.access(binary, os.X_OK)
    return shutil.which(binary) is not None


async def get_driver_catalog() -> list[DriverInfo]:
    """List every driver known to the INDI driver catalog (installed on this device)."""
    drivers = await asyncio.to_thread(lambda: _get_catalog().drivers)
    return [
        {
            "name": d.name,
            "label": d.label,
            "version": d.version,
            "family": d.family,
            "binary": d.binary,
            "installed": _is_binary_installed(d.binary),
        }
        for d in drivers
    ]


async def start_driver(label: str) -> DriverStatus:
    """Start the INDI driver identified by its catalog label (e.g. "CCD Simulator")."""
    driver = await asyncio.to_thread(_find_driver, label)
    logger.info("Starting driver: %s", label)
    await asyncio.to_thread(indi_server._server.start_driver, driver)
    return {"label": label, "running": True}


async def stop_driver(label: str) -> DriverStatus:
    """Stop the running INDI driver identified by its catalog label."""
    if label not in indi_server._server.get_running_drivers():
        raise ValueError(f"Driver not running: {label!r}")
    driver = await asyncio.to_thread(_find_driver, label)
    logger.info("Stopping driver: %s", label)
    await asyncio.to_thread(indi_server._server.stop_driver, driver)
    return {"label": label, "running": False}


async def list_running_drivers() -> list[DriverStatus]:
    """List all currently running INDI drivers."""
    running = await asyncio.to_thread(indi_server._server.get_running_drivers)
    return [{"label": label, "running": True} for label in running]
