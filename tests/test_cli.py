from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from indi_mcp import cli, indi_driver, indi_messaging, indi_server


@dataclass
class Mocks:
    start_server: AsyncMock
    stop_server: AsyncMock
    restart_server: AsyncMock
    list_running_drivers: AsyncMock
    start_driver: AsyncMock
    stop_driver: AsyncMock
    start_messaging: AsyncMock
    stop_messaging: AsyncMock


@pytest.fixture(autouse=True)
def mocks(monkeypatch: pytest.MonkeyPatch) -> Mocks:
    get_status = AsyncMock(return_value={"running": True, "port": 7624})
    start_server = AsyncMock(return_value={"running": True, "port": 7624})
    stop_server = AsyncMock(return_value={"running": False, "port": 7624})
    restart_server = AsyncMock(return_value={"running": True, "port": 7625})
    monkeypatch.setattr(indi_server, "get_status", get_status)
    monkeypatch.setattr(indi_server, "start_server", start_server)
    monkeypatch.setattr(indi_server, "stop_server", stop_server)
    monkeypatch.setattr(indi_server, "restart_server", restart_server)

    catalog = [
        {"name": "indi_simulator_ccd", "label": "CCD Simulator", "version": "1.0", "family": "CCDs"}
    ]
    get_driver_catalog = AsyncMock(return_value=catalog)
    list_running_drivers = AsyncMock(return_value=[{"label": "CCD Simulator", "running": True}])
    start_driver = AsyncMock(return_value={"label": "CCD Simulator", "running": True})
    stop_driver = AsyncMock(return_value={"label": "CCD Simulator", "running": False})
    monkeypatch.setattr(indi_driver, "get_driver_catalog", get_driver_catalog)
    monkeypatch.setattr(indi_driver, "list_running_drivers", list_running_drivers)
    monkeypatch.setattr(indi_driver, "start_driver", start_driver)
    monkeypatch.setattr(indi_driver, "stop_driver", stop_driver)

    start_messaging = AsyncMock()
    stop_messaging = AsyncMock()
    monkeypatch.setattr(indi_messaging, "start_messaging", start_messaging)
    monkeypatch.setattr(indi_messaging, "stop_messaging", stop_messaging)

    return Mocks(
        start_server=start_server,
        stop_server=stop_server,
        restart_server=restart_server,
        list_running_drivers=list_running_drivers,
        start_driver=start_driver,
        stop_driver=stop_driver,
        start_messaging=start_messaging,
        stop_messaging=stop_messaging,
    )


def _parse(args: list[str]) -> cli.argparse.Namespace:
    return cli._build_parser().parse_args(args)


async def test_server_status_reports_running(capsys: pytest.CaptureFixture[str]) -> None:
    args = _parse(["server", "status"])

    await args.func(args)

    assert "running" in capsys.readouterr().out


async def test_server_start_passes_port(mocks: Mocks, capsys: pytest.CaptureFixture[str]) -> None:
    args = _parse(["server", "start", "--port", "7624"])

    await args.func(args)

    mocks.start_server.assert_called_once_with(7624)
    assert "started" in capsys.readouterr().out


async def test_server_stop(mocks: Mocks) -> None:
    args = _parse(["server", "stop"])

    await args.func(args)

    mocks.stop_server.assert_called_once()


async def test_server_restart_default_port_is_none(mocks: Mocks) -> None:
    args = _parse(["server", "restart"])

    await args.func(args)

    mocks.restart_server.assert_called_once_with(None)


async def test_driver_list_prints_catalog(capsys: pytest.CaptureFixture[str]) -> None:
    args = _parse(["driver", "list"])

    await args.func(args)

    assert "CCD Simulator" in capsys.readouterr().out


async def test_driver_running_prints_labels(capsys: pytest.CaptureFixture[str]) -> None:
    args = _parse(["driver", "running"])

    await args.func(args)

    assert "CCD Simulator" in capsys.readouterr().out


async def test_driver_running_reports_none_running(
    mocks: Mocks, capsys: pytest.CaptureFixture[str]
) -> None:
    mocks.list_running_drivers.return_value = []
    args = _parse(["driver", "running"])

    await args.func(args)

    assert "No drivers running" in capsys.readouterr().out


async def test_driver_start_passes_label(mocks: Mocks) -> None:
    args = _parse(["driver", "start", "CCD Simulator"])

    await args.func(args)

    mocks.start_driver.assert_called_once_with("CCD Simulator")


async def test_driver_stop_passes_label(mocks: Mocks) -> None:
    args = _parse(["driver", "stop", "CCD Simulator"])

    await args.func(args)

    mocks.stop_driver.assert_called_once_with("CCD Simulator")


async def test_listen_starts_and_stops_messaging(
    mocks: Mocks, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(indi_messaging, "list_messages", lambda device, limit: [])

    async def _raise_keyboard_interrupt(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.asyncio, "sleep", _raise_keyboard_interrupt)

    args = _parse(["listen", "--host", "example.local", "--port", "7624"])

    await args.func(args)

    mocks.start_messaging.assert_called_once_with("example.local", 7624)
    mocks.stop_messaging.assert_called_once()


async def test_listen_prints_new_events_newest_last(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    events = [
        {
            "kind": "propertyUpdate",
            "type": "number",
            "device": "CCD Simulator",
            "name": "CCD_EXPOSURE",
            "state": "Busy",
            "message": None,
            "elements": {"CCD_EXPOSURE_VALUE": "5.0"},
            "timestamp": "2026-07-15T00:00:01+00:00",
        },
        {
            "kind": "message",
            "type": None,
            "device": "CCD Simulator",
            "name": None,
            "state": None,
            "message": "Hello",
            "elements": None,
            "timestamp": "2026-07-15T00:00:00+00:00",
        },
    ]
    monkeypatch.setattr(indi_messaging, "list_messages", lambda device, limit: events)

    calls = {"count": 0}

    async def _sleep_once(*_args: object, **_kwargs: object) -> None:
        calls["count"] += 1
        if calls["count"] > 1:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.asyncio, "sleep", _sleep_once)

    args = _parse(["listen"])

    await args.func(args)

    out = capsys.readouterr().out
    assert out.index("Hello") < out.index("CCD_EXPOSURE_VALUE")
