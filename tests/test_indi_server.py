from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from indi_mcp import indi_server


@dataclass
class Mocks:
    server: MagicMock
    launch: MagicMock
    launched_cmd: MagicMock


@pytest.fixture(autouse=True)
def mocks(monkeypatch: pytest.MonkeyPatch) -> Mocks:
    server = MagicMock()
    server.is_running.return_value = False
    monkeypatch.setattr(indi_server, "_server", server)
    monkeypatch.setattr(indi_server, "_current_port", indi_server.INDI_PORT)
    monkeypatch.setattr(indi_server, "_async_cmd", None)
    monkeypatch.setattr(indi_server, "_clear_fifo", MagicMock())

    launched_cmd = MagicMock(spec=indi_server.AsyncSystemCommand)
    launch = MagicMock(return_value=launched_cmd)
    monkeypatch.setattr(indi_server, "_launch", launch)

    return Mocks(server=server, launch=launch, launched_cmd=launched_cmd)


async def test_start_server_launches_indiserver_on_given_port(mocks: Mocks) -> None:
    mocks.server.is_running.return_value = True

    status = await indi_server.start_server(port=7625)

    mocks.launch.assert_called_once_with(7625)
    assert status == {"running": True, "port": 7625}


async def test_start_server_stops_existing_server_first(mocks: Mocks) -> None:
    mocks.server.is_running.return_value = True

    await indi_server.start_server(port=7625)

    mocks.server.stop.assert_called_once_with(indi_server.INDI_PORT)


async def test_stop_server_stops_current_port_and_terminates_async_cmd(mocks: Mocks) -> None:
    mocks.server.is_running.return_value = True
    await indi_server.start_server(port=7625)
    mocks.server.is_running.return_value = False

    status = await indi_server.stop_server()

    mocks.server.stop.assert_called_with(7625)
    mocks.launched_cmd.terminate.assert_called_once()
    assert status == {"running": False, "port": 7625}


async def test_restart_server_keeps_current_port_by_default(mocks: Mocks) -> None:
    mocks.server.is_running.return_value = True
    await indi_server.start_server(port=7625)

    status = await indi_server.restart_server()

    mocks.server.stop.assert_called_with(7625)
    assert mocks.launch.call_args_list[-1].args == (7625,)
    assert status == {"running": True, "port": 7625}


async def test_restart_server_switches_to_new_port(mocks: Mocks) -> None:
    mocks.server.is_running.return_value = True
    await indi_server.start_server(port=7625)

    status = await indi_server.restart_server(port=7626)

    assert mocks.launch.call_args_list[-1].args == (7626,)
    assert status == {"running": True, "port": 7626}


async def test_start_server_polls_until_process_becomes_visible(mocks: Mocks) -> None:
    mocks.server.is_running.side_effect = [False, False, True]

    status = await indi_server.start_server(port=7625)

    assert mocks.server.is_running.call_count == 3
    assert status == {"running": True, "port": 7625}


async def test_start_server_returns_not_running_if_poll_times_out(
    mocks: Mocks, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(indi_server, "_STARTUP_POLL_TIMEOUT", 0.05)
    monkeypatch.setattr(indi_server, "_STARTUP_POLL_INTERVAL", 0.01)
    mocks.server.is_running.return_value = False

    status = await indi_server.start_server(port=7625)

    assert status == {"running": False, "port": 7625}


async def test_get_status_reports_running_state(mocks: Mocks) -> None:
    mocks.server.is_running.return_value = False

    status = await indi_server.get_status()

    assert status == {"running": False, "port": indi_server.INDI_PORT}
