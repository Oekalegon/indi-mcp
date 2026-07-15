from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from indi_mcp import indi_driver, indi_server


def _make_driver(label: str, name: str = "indi_ccd_simulator") -> MagicMock:
    driver = MagicMock()
    driver.label = label
    driver.name = name
    driver.version = "1.0"
    driver.family = "CCDs"
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

    catalog = MagicMock()
    catalog.drivers = [_make_driver("CCD Simulator")]
    catalog.by_label.return_value = None
    monkeypatch.setattr(indi_driver, "_catalog", catalog)
    monkeypatch.setattr(indi_driver, "_get_catalog", lambda: catalog)

    return Mocks(server=server, catalog=catalog)


async def test_get_driver_catalog_lists_known_drivers(mocks: Mocks) -> None:
    catalog = await indi_driver.get_driver_catalog()

    assert catalog == [
        {"name": "indi_ccd_simulator", "label": "CCD Simulator", "version": "1.0", "family": "CCDs"}
    ]


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


async def test_stop_driver_stops_running_driver(mocks: Mocks) -> None:
    driver = _make_driver("CCD Simulator")
    mocks.catalog.by_label.return_value = driver
    mocks.server.get_running_drivers.return_value = {"CCD Simulator": driver}

    status = await indi_driver.stop_driver("CCD Simulator")

    mocks.server.stop_driver.assert_called_once_with(driver)
    assert status == {"label": "CCD Simulator", "running": False}


async def test_stop_driver_rejects_driver_that_is_not_running(mocks: Mocks) -> None:
    mocks.server.get_running_drivers.return_value = {}

    with pytest.raises(ValueError, match="Driver not running"):
        await indi_driver.stop_driver("CCD Simulator")

    mocks.server.stop_driver.assert_not_called()


async def test_list_running_drivers_reports_currently_running_drivers(mocks: Mocks) -> None:
    driver = _make_driver("CCD Simulator")
    mocks.server.get_running_drivers.return_value = {"CCD Simulator": driver}

    running = await indi_driver.list_running_drivers()

    assert running == [{"label": "CCD Simulator", "running": True}]
