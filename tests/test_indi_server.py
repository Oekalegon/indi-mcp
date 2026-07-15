from unittest.mock import MagicMock

import pytest

from indi_mcp import indi_server


@pytest.fixture(autouse=True)
def mock_server(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock = MagicMock()
    mock.is_running.return_value = False
    monkeypatch.setattr(indi_server, "_server", mock)
    monkeypatch.setattr(indi_server, "_current_port", indi_server.INDI_PORT)
    return mock


async def test_start_server_starts_on_given_port(mock_server: MagicMock) -> None:
    mock_server.is_running.return_value = True

    status = await indi_server.start_server(port=7625)

    mock_server.start.assert_called_once_with(7625)
    assert status == {"running": True, "port": 7625}


async def test_stop_server_stops_current_port(mock_server: MagicMock) -> None:
    await indi_server.start_server(port=7625)
    mock_server.is_running.return_value = False

    status = await indi_server.stop_server()

    mock_server.stop.assert_called_with(7625)
    assert status == {"running": False, "port": 7625}


async def test_restart_server_keeps_current_port_by_default(mock_server: MagicMock) -> None:
    await indi_server.start_server(port=7625)
    mock_server.is_running.return_value = True

    status = await indi_server.restart_server()

    mock_server.stop.assert_called_with(7625)
    mock_server.start.assert_called_with(7625)
    assert status == {"running": True, "port": 7625}


async def test_restart_server_switches_to_new_port(mock_server: MagicMock) -> None:
    await indi_server.start_server(port=7625)
    mock_server.is_running.return_value = True

    status = await indi_server.restart_server(port=7626)

    mock_server.stop.assert_called_with(7625)
    mock_server.start.assert_called_with(7626)
    assert status == {"running": True, "port": 7626}


async def test_get_status_reports_running_state(mock_server: MagicMock) -> None:
    mock_server.is_running.return_value = False

    status = await indi_server.get_status()

    assert status == {"running": False, "port": indi_server.INDI_PORT}
