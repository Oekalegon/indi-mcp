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
    "classify_device",
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


async def classify_device(name: str) -> str | None:
    """Return the driver family (e.g. `"CCDs"`, `"Focusers"`) for the INDI device `name`.

    `None` if `name` isn't a known driver label. Device names match driver
    labels 1:1 (drivers report their own device name, which conventionally
    matches the catalog label), so this looks the name up directly as a
    catalog label rather than needing its own device-to-driver mapping.
    """
    driver = await asyncio.to_thread(lambda: _get_catalog().by_label(name))
    return driver.family if driver is not None else None


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


def _running_driver_processes() -> tuple[set[str], list[str]]:
    """Scan `indiserver`'s local child processes once, splitting them by whether they
    carry a `-n "<label>"` argument.

    `indiweb.IndiServer.start_driver` always passes `-n "<label>"` on the command line for a
    locally-spawned driver (it's only skipped for remote/MDPD drivers, which don't spawn a
    local process at all — see `_is_locally_observable`). A running process's own cmdline is
    therefore an authoritative, collision-free way to identify which label it is: unlike the
    driver binary, which multiple catalog entries can share (e.g. templated/MDPD-style XML),
    the label passed via `-n` is unique per running instance.

    Drivers started by a *different* INDI client writing directly to `indiserver`'s FIFO
    (e.g. KStars/EKOS) never get `-n` set, since that flag is only added by indiweb's own
    `start_driver`. For those, this also returns the running processes' binary names so the
    caller can attempt a best-effort catalog match (see `_label_for_unlabeled_binary`).
    """
    labels: set[str] = set()
    unlabeled_binaries: list[str] = []
    for proc in indi_server.get_driver_processes():
        try:
            cmdline = proc.cmdline()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if not cmdline:
            continue
        label = None
        for i, arg in enumerate(cmdline):
            if arg == "-n" and i + 1 < len(cmdline):
                label = cmdline[i + 1]
                break
        if label is not None:
            labels.add(label)
        else:
            unlabeled_binaries.append(Path(cmdline[0]).name)
    return labels, unlabeled_binaries


def _label_for_unlabeled_binary(binary: str, catalog: DriverCollection) -> str | None:
    """Best-effort catalog label for a driver process with no `-n` argument.

    Matches `binary` (a running process's basename) against locally-observable catalog
    entries. Returns a label only when exactly one entry uses that binary: per
    `_is_locally_observable`, multiple catalog entries can legitimately share a binary
    (e.g. templated/MDPD-style XML), and without the `-n` label there is no way to tell
    which one is actually running, so an ambiguous match is left unresolved (`None`)
    rather than risking the wrong label.
    """
    candidates = [
        driver
        for driver in catalog.drivers
        if _is_locally_observable(driver) and Path(driver.binary).name == binary
    ]
    if len(candidates) == 1:
        return candidates[0].label
    return None


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

    Processes with no `-n` label (started by another INDI client directly via the FIFO, e.g.
    KStars/EKOS) are folded in here too, via a best-effort binary match — see
    `_label_for_unlabeled_binary`.
    """
    running_labels, unlabeled_binaries = _running_driver_processes()
    registry = indi_server._server.get_running_drivers()
    catalog = _get_catalog()

    for binary in unlabeled_binaries:
        label = _label_for_unlabeled_binary(binary, catalog)
        if label is not None:
            running_labels.add(label)

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
