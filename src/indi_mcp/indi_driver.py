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

import psutil
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


def _running_driver_labels() -> set[str]:
    """Labels of drivers `indiserver` currently has running as local children.

    `indiweb.IndiServer.start_driver` always passes `-n "<label>"` on the command line for a
    locally-spawned driver (it's only skipped for remote/MDPD drivers, which don't spawn a
    local process at all — see `_is_locally_observable`). So a running process's own cmdline
    is an authoritative, collision-free way to identify which label it is: unlike the driver
    binary, which multiple catalog entries can share (e.g. templated/MDPD-style XML), the
    label passed via `-n` is unique per running instance.
    """
    labels: set[str] = set()
    for proc in indi_server.get_driver_processes():
        try:
            cmdline = proc.cmdline()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        for i, arg in enumerate(cmdline):
            if arg == "-n" and i + 1 < len(cmdline):
                labels.add(cmdline[i + 1])
                break
    return labels


def _is_locally_observable(driver: DeviceDriver) -> bool:
    """Whether `driver` spawns a local `indiserver` child process we can detect via psutil.

    Remote drivers (binary contains "@") run on another host, and MDPD drivers share a
    single process across multiple catalog entries — neither case can be reliably confirmed
    or ruled out via the local process tree, so reconciliation leaves them alone and trusts
    whatever `start_driver`/`stop_driver` already recorded for them.
    """
    return "@" not in driver.binary and not driver.mdpd


def _reconcile_running_drivers() -> dict[str, DeviceDriver]:
    """Sync the in-memory running-driver registry against `indiserver`'s actual children.

    The registry (`indi_server._server`) is only populated by `start_driver`/`stop_driver`
    calls made within the current process, so it starts out empty after an MCP server
    restart even though `indiserver` and any drivers it already started are detached and
    keep running. This reconciles the registry against the real OS process tree so reads
    (and the running-driver check in `stop_driver`) reflect reality regardless of restarts.

    Remote and MDPD drivers are excluded from reconciliation (see `_is_locally_observable`):
    they don't correspond 1:1 with a local child process, so we'd otherwise incorrectly drop
    them from the registry the moment they're read back after being started.
    """
    running_labels = _running_driver_labels()
    registry = indi_server._server.get_running_drivers()
    catalog = _get_catalog()

    for label in running_labels:
        if label in registry:
            continue
        driver = catalog.by_label(label)
        if driver is not None and _is_locally_observable(driver):
            registry[label] = driver

    for label in [
        label
        for label, driver in registry.items()
        if _is_locally_observable(driver) and label not in running_labels
    ]:
        del registry[label]

    return registry


async def start_driver(label: str) -> DriverStatus:
    """Start the INDI driver identified by its catalog label (e.g. "CCD Simulator")."""
    driver = await asyncio.to_thread(_find_driver, label)
    logger.info("Starting driver: %s", label)
    await asyncio.to_thread(indi_server._server.start_driver, driver)
    return {"label": label, "running": True}


async def stop_driver(label: str) -> DriverStatus:
    """Stop the running INDI driver identified by its catalog label."""
    running = await asyncio.to_thread(_reconcile_running_drivers)
    if label not in running:
        raise ValueError(f"Driver not running: {label!r}")
    driver = await asyncio.to_thread(_find_driver, label)
    logger.info("Stopping driver: %s", label)
    await asyncio.to_thread(indi_server._server.stop_driver, driver)
    return {"label": label, "running": False}


async def list_running_drivers() -> list[DriverStatus]:
    """List all currently running INDI drivers."""
    running = await asyncio.to_thread(_reconcile_running_drivers)
    return [{"label": label, "running": True} for label in running]
