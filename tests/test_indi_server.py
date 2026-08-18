from dataclasses import dataclass
from unittest.mock import MagicMock

import psutil
import pytest

from indi_mcp import event_streams, indi_server


@dataclass
class Mocks:
    server: MagicMock
    launch: MagicMock
    launched_cmd: MagicMock


@pytest.fixture(autouse=True)
def mocks(monkeypatch: pytest.MonkeyPatch) -> Mocks:
    event_streams._connections.clear()
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


async def test_start_server_publishes_connection_made_when_it_becomes_running(
    mocks: Mocks,
) -> None:
    """INDIMCP-57: a successful start should surface as a connection-lifecycle event, not
    just a `running: True` status only visible to whoever happens to poll `get_status`."""
    mocks.server.is_running.return_value = True

    await indi_server.start_server(port=7625)

    events = event_streams.read_connection("indiserver")["events"]
    assert len(events) == 1
    assert events[0]["kind"] == "connectionMade"
    assert events[0]["target"] == "indiserver"


async def test_start_server_does_not_publish_when_the_poll_times_out(
    mocks: Mocks, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A start attempt that never actually becomes visible to psutil shouldn't claim a
    connection was made — see `test_start_server_returns_not_running_if_poll_times_out`."""
    monkeypatch.setattr(indi_server, "_STARTUP_POLL_TIMEOUT", 0.05)
    monkeypatch.setattr(indi_server, "_STARTUP_POLL_INTERVAL", 0.01)
    mocks.server.is_running.return_value = False

    await indi_server.start_server(port=7625)

    assert event_streams.read_connection("indiserver")["events"] == []


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


async def test_stop_server_publishes_connection_lost_when_it_actually_stops(
    mocks: Mocks,
) -> None:
    mocks.server.is_running.return_value = True
    await indi_server.start_server(port=7625)
    event_streams._connections.clear()  # only interested in what stop_server itself publishes
    # `stop_server` checks `is_running` twice: once before stopping (still running: True) and
    # once after (confirms it actually stopped: False) — a plain `return_value = False` would
    # make *both* calls report already-stopped, never observing the transition it publishes on.
    mocks.server.is_running.side_effect = [True, False]

    await indi_server.stop_server()

    events = event_streams.read_connection("indiserver")["events"]
    assert len(events) == 1
    assert events[0]["kind"] == "connectionLost"
    assert events[0]["target"] == "indiserver"


async def test_stop_server_does_not_publish_when_it_was_not_running(mocks: Mocks) -> None:
    """Stopping an already-stopped server is a routine no-op call, not a real disconnect —
    see `_connection_event`'s `was_running` guard in `stop_server`."""
    mocks.server.is_running.return_value = False

    await indi_server.stop_server()

    assert event_streams.read_connection("indiserver")["events"] == []


async def test_stop_server_tolerates_async_cmd_already_reaped(mocks: Mocks) -> None:
    """`_server.stop()` (psutil-based) frequently wins the race to reap the process
    before `_async_cmd.terminate()` runs, which then raises a bare `ProcessLookupError`
    from `os.getpgid()` -- that must not propagate out of `stop_server()`."""
    mocks.server.is_running.return_value = True
    await indi_server.start_server(port=7625)
    mocks.launched_cmd.terminate.side_effect = ProcessLookupError
    mocks.server.is_running.return_value = False

    status = await indi_server.stop_server()

    mocks.launched_cmd.terminate.assert_called_once()
    assert status == {"running": False, "port": 7625}


async def test_stop_server_does_not_swallow_other_terminate_errors(mocks: Mocks) -> None:
    mocks.server.is_running.return_value = True
    await indi_server.start_server(port=7625)
    mocks.launched_cmd.terminate.side_effect = RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await indi_server.stop_server()

    assert indi_server._async_cmd is None


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


def _make_indiserver_process(port: int, children: list[MagicMock]) -> MagicMock:
    proc = MagicMock()
    proc.info = {"name": "indiserver", "cmdline": ["indiserver", "-p", str(port)]}
    proc.children.return_value = children
    return proc


def test_get_driver_processes_returns_children_of_matching_indiserver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver_proc = MagicMock()
    server_proc = _make_indiserver_process(indi_server.INDI_PORT, [driver_proc])
    other_server_proc = _make_indiserver_process(7625, [MagicMock()])
    monkeypatch.setattr(
        indi_server.psutil,
        "process_iter",
        lambda _fields: [other_server_proc, server_proc],
    )

    processes = indi_server.get_driver_processes(indi_server.INDI_PORT)

    assert processes == [driver_proc]


def test_get_driver_processes_returns_empty_when_no_indiserver_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(indi_server.psutil, "process_iter", lambda _fields: [])

    assert indi_server.get_driver_processes(indi_server.INDI_PORT) == []


def test_get_driver_processes_survives_process_disappearing_mid_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A process can exit between the process_iter snapshot and reading its .info,
    raising psutil.NoSuchProcess. This shouldn't propagate as an unhandled exception
    from a routinely-polled status call -- it should just find no server process."""

    def _raise(_fields: list[str]) -> list[MagicMock]:
        raise psutil.NoSuchProcess(pid=1234)

    monkeypatch.setattr(indi_server.psutil, "process_iter", _raise)

    assert indi_server.get_driver_processes(indi_server.INDI_PORT) == []
