from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from indi_mcp import indi_driver, indi_server


def _make_driver(
    label: str, name: str = "indi_ccd_simulator", binary: str = "indi_ccd_simulator"
) -> MagicMock:
    driver = MagicMock()
    driver.label = label
    driver.name = name
    driver.version = "1.0"
    driver.family = "CCDs"
    driver.binary = binary
    driver.mdpd = False
    return driver


@dataclass
class Mocks:
    server: MagicMock
    catalog: MagicMock


@pytest.fixture(autouse=True)
def mocks(monkeypatch: pytest.MonkeyPatch) -> Mocks:
    server = MagicMock()
    server.get_running_drivers.return_value = {}
    monkeypatch.setattr(indi_server, "_server", server)
    monkeypatch.setattr(indi_server, "get_driver_processes", lambda: [])

    catalog = MagicMock()
    catalog.drivers = [_make_driver("CCD Simulator")]
    catalog.by_label.return_value = None
    monkeypatch.setattr(indi_driver, "_catalog", catalog)
    monkeypatch.setattr(indi_driver, "_get_catalog", lambda: catalog)

    return Mocks(server=server, catalog=catalog)


def _make_process(label: str, binary: str = "indi_ccd_simulator") -> MagicMock:
    """A psutil.Process-like mock for a driver started with `-n "<label>"`."""
    proc = MagicMock()
    proc.cmdline.return_value = [binary, "-n", label]
    return proc


async def test_get_driver_catalog_lists_known_drivers(
    mocks: Mocks, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(indi_driver.shutil, "which", lambda binary: "/usr/bin/" + binary)

    catalog = await indi_driver.get_driver_catalog()

    assert catalog == [
        {
            "name": "indi_ccd_simulator",
            "label": "CCD Simulator",
            "version": "1.0",
            "family": "CCDs",
            "binary": "indi_ccd_simulator",
            "installed": True,
        }
    ]


async def test_get_driver_catalog_flags_binaries_that_are_not_installed(
    mocks: Mocks, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(indi_driver.shutil, "which", lambda binary: None)

    catalog = await indi_driver.get_driver_catalog()

    assert catalog[0]["installed"] is False


@pytest.mark.parametrize(
    ("binary", "which_result", "exists", "expected"),
    [
        ("", None, False, False),
        ("indi_ccd_simulator", "/usr/bin/indi_ccd_simulator", False, True),
        ("indi_ccd_simulator", None, False, False),
        ("/opt/indi/bin/indi_ccd_simulator", None, True, True),
        ("/opt/indi/bin/indi_ccd_simulator", None, False, False),
    ],
)
def test_is_binary_installed(
    monkeypatch: pytest.MonkeyPatch,
    binary: str,
    which_result: str | None,
    exists: bool,
    expected: bool,
) -> None:
    monkeypatch.setattr(indi_driver.shutil, "which", lambda _binary: which_result)
    monkeypatch.setattr(indi_driver.os, "access", lambda _path, _mode: exists)

    assert indi_driver._is_binary_installed(binary) is expected


async def test_start_driver_starts_known_driver(mocks: Mocks) -> None:
    driver = _make_driver("CCD Simulator")
    mocks.catalog.by_label.return_value = driver

    status = await indi_driver.start_driver("CCD Simulator")

    mocks.server.start_driver.assert_called_once_with(driver)
    assert status == {"label": "CCD Simulator", "running": True}


async def test_start_driver_rejects_unknown_label(mocks: Mocks) -> None:
    mocks.catalog.by_label.return_value = None

    with pytest.raises(ValueError, match="Unknown driver"):
        await indi_driver.start_driver("Nonexistent Driver")

    mocks.server.start_driver.assert_not_called()


async def test_stop_driver_stops_running_driver(
    mocks: Mocks, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _make_driver("CCD Simulator")
    mocks.catalog.by_label.return_value = driver
    mocks.server.get_running_drivers.return_value = {"CCD Simulator": driver}
    monkeypatch.setattr(
        indi_server, "get_driver_processes", lambda: [_make_process("CCD Simulator")]
    )

    status = await indi_driver.stop_driver("CCD Simulator")

    mocks.server.stop_driver.assert_called_once_with(driver)
    assert status == {"label": "CCD Simulator", "running": False}


async def test_stop_driver_rejects_driver_that_is_not_running(mocks: Mocks) -> None:
    mocks.server.get_running_drivers.return_value = {}

    with pytest.raises(ValueError, match="Driver not running"):
        await indi_driver.stop_driver("CCD Simulator")

    mocks.server.stop_driver.assert_not_called()


async def test_list_running_drivers_reports_currently_running_drivers(
    mocks: Mocks, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _make_driver("CCD Simulator")
    mocks.server.get_running_drivers.return_value = {"CCD Simulator": driver}
    monkeypatch.setattr(
        indi_server, "get_driver_processes", lambda: [_make_process("CCD Simulator")]
    )

    running = await indi_driver.list_running_drivers()

    assert running == [{"label": "CCD Simulator", "running": True}]


async def test_list_running_drivers_reflects_processes_after_server_restart(
    mocks: Mocks, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The in-memory registry starts empty after an MCP server restart, but the
    driver process (started before the restart) survives, so it should still
    be reported as running once reconciled against the process tree."""
    driver = _make_driver("CCD Simulator")
    mocks.catalog.drivers = [driver]
    mocks.catalog.by_label.return_value = driver
    mocks.server.get_running_drivers.return_value = {}
    monkeypatch.setattr(
        indi_server, "get_driver_processes", lambda: [_make_process("CCD Simulator")]
    )

    running = await indi_driver.list_running_drivers()

    assert running == [{"label": "CCD Simulator", "running": True}]


async def test_list_running_drivers_disambiguates_shared_binary_by_label(
    mocks: Mocks, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two catalog drivers can share a binary (e.g. templated/MDPD-style XML). The
    running process's `-n` label -- not the binary -- must decide which one is running,
    so a shared binary doesn't cause the wrong driver to be reported as running."""
    driver_a = _make_driver("Device A", binary="indi_shared_driver")
    driver_b = _make_driver("Device B", binary="indi_shared_driver")
    mocks.catalog.drivers = [driver_a, driver_b]
    mocks.catalog.by_label.side_effect = {"Device A": driver_a, "Device B": driver_b}.get
    mocks.server.get_running_drivers.return_value = {}
    monkeypatch.setattr(
        indi_server,
        "get_driver_processes",
        lambda: [_make_process("Device B", binary="indi_shared_driver")],
    )

    running = await indi_driver.list_running_drivers()

    assert running == [{"label": "Device B", "running": True}]


async def test_list_running_drivers_drops_stale_registry_entries(
    mocks: Mocks, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the registry believes a driver is running but its process is gone
    (e.g. it crashed or was killed outside the MCP server), it shouldn't be
    reported as running."""
    driver = _make_driver("CCD Simulator")
    mocks.server.get_running_drivers.return_value = {"CCD Simulator": driver}
    monkeypatch.setattr(indi_server, "get_driver_processes", lambda: [])

    running = await indi_driver.list_running_drivers()

    assert running == []


async def test_list_running_drivers_keeps_remote_driver_with_no_local_process(
    mocks: Mocks, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A remote driver (binary contains "@") runs on another host, so indiserver
    never forks a local process for it. Reconciliation must not purge it from the
    registry just because no matching local process is found."""
    remote_driver = _make_driver("Remote Mount", binary="indi_lx200@192.168.1.50")
    mocks.catalog.drivers = [remote_driver]
    mocks.server.get_running_drivers.return_value = {"Remote Mount": remote_driver}
    monkeypatch.setattr(indi_server, "get_driver_processes", lambda: [])

    running = await indi_driver.list_running_drivers()

    assert running == [{"label": "Remote Mount", "running": True}]


async def test_list_running_drivers_keeps_mdpd_driver_with_no_local_process_match(
    mocks: Mocks, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An MDPD driver shares one process across multiple catalog entries, so it
    can't be reliably matched by binary alone. Reconciliation must not purge it."""
    mdpd_driver = _make_driver("MDPD Device")
    mdpd_driver.mdpd = True
    mocks.catalog.drivers = [mdpd_driver]
    mocks.server.get_running_drivers.return_value = {"MDPD Device": mdpd_driver}
    monkeypatch.setattr(indi_server, "get_driver_processes", lambda: [])

    running = await indi_driver.list_running_drivers()

    assert running == [{"label": "MDPD Device", "running": True}]


async def test_classify_device_returns_the_driver_family_for_a_known_device(
    mocks: Mocks,
) -> None:
    mocks.catalog.by_label.return_value = _make_driver("CCD Simulator")

    family = await indi_driver.classify_device("CCD Simulator")

    assert family == "CCDs"


async def test_classify_device_returns_none_for_an_unrecognized_device(mocks: Mocks) -> None:
    mocks.catalog.by_label.return_value = None

    family = await indi_driver.classify_device("Unrecognized Device")

    assert family is None
