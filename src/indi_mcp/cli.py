"""A small standalone CLI for manually testing/debugging the INDI MCP tools.

Talks directly to the `indi_server`/`indi_driver`/`indi_messaging` modules
(the same functions the MCP tools in `server.py` call) rather than going
through MCP itself, so it can be run on its own during development to manage
`indiserver`/drivers and to watch the event stream, without needing an MCP
client.
"""

import argparse
import asyncio
import contextlib
import logging

from indi_mcp import indi_driver, indi_messaging, indi_server
from indi_mcp.indi_messaging import IndiEvent
from indi_mcp.indi_server import INDI_PORT

logger = logging.getLogger(__name__)

_LISTEN_POLL_INTERVAL = 0.5


def _format_event(event: IndiEvent) -> str:
    parts = [event["timestamp"], event["kind"]]
    if event["type"] is not None:
        parts.append(event["type"])
    if event["device"] is not None:
        parts.append(event["device"])
    if event["name"] is not None:
        parts.append(event["name"])
    line = " | ".join(parts)
    if event["message"]:
        line += f" | {event['message']}"
    if event["elements"]:
        line += f" | {event['elements']}"
    return line


async def _cmd_server_status(_args: argparse.Namespace) -> None:
    status = await indi_server.get_status()
    print(f"indiserver: {'running' if status['running'] else 'stopped'} (port {status['port']})")


async def _cmd_server_start(args: argparse.Namespace) -> None:
    status = await indi_server.start_server(args.port)
    print(f"indiserver started: running={status['running']} port={status['port']}")


async def _cmd_server_stop(_args: argparse.Namespace) -> None:
    status = await indi_server.stop_server()
    print(f"indiserver stopped: running={status['running']}")


async def _cmd_server_restart(args: argparse.Namespace) -> None:
    status = await indi_server.restart_server(args.port)
    print(f"indiserver restarted: running={status['running']} port={status['port']}")


async def _cmd_driver_list(_args: argparse.Namespace) -> None:
    catalog = await indi_driver.get_driver_catalog()
    for driver in catalog:
        marker = "" if driver["installed"] else "  [not installed]"
        print(
            f"{driver['label']:<30} {driver['family']:<20} "
            f"{driver['name']} ({driver['version']}){marker}"
        )


async def _cmd_driver_running(_args: argparse.Namespace) -> None:
    running = await indi_driver.list_running_drivers()
    if not running:
        print("No drivers running.")
        return
    for driver in running:
        print(driver["label"])


async def _cmd_driver_start(args: argparse.Namespace) -> None:
    status = await indi_driver.start_driver(args.label)
    print(f"Driver started: {status['label']} running={status['running']}")


async def _cmd_driver_stop(args: argparse.Namespace) -> None:
    status = await indi_driver.stop_driver(args.label)
    print(f"Driver stopped: {status['label']} running={status['running']}")


async def _cmd_listen(args: argparse.Namespace) -> None:
    await indi_messaging.start_messaging(args.host, args.port)
    print(
        f"Listening for INDI events on {args.host}:{args.port} "
        f"(device filter: {args.device or 'all'}). Press Ctrl+C to stop."
    )
    last_timestamp: str | None = None
    try:
        while True:
            events = indi_messaging.list_messages(args.device, limit=200)
            new_events = []
            for event in events:
                if last_timestamp is not None and event["timestamp"] <= last_timestamp:
                    break
                new_events.append(event)
            if new_events:
                last_timestamp = new_events[0]["timestamp"]
                for event in reversed(new_events):
                    print(_format_event(event))
            await asyncio.sleep(_LISTEN_POLL_INTERVAL)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await indi_messaging.stop_messaging()
        print("Stopped listening.")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="indi-mcp-cli",
        description=(
            "Manually manage the INDI server/drivers and watch its event stream, "
            "for testing/debugging the INDI MCP tools."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    server_parser = subparsers.add_parser("server", help="Manage the indiserver process")
    server_sub = server_parser.add_subparsers(dest="server_command", required=True)

    server_status = server_sub.add_parser("status", help="Show indiserver status")
    server_status.set_defaults(func=_cmd_server_status)

    server_start = server_sub.add_parser("start", help="Start indiserver")
    server_start.add_argument("--port", type=int, default=INDI_PORT)
    server_start.set_defaults(func=_cmd_server_start)

    server_stop = server_sub.add_parser("stop", help="Stop indiserver")
    server_stop.set_defaults(func=_cmd_server_stop)

    server_restart = server_sub.add_parser("restart", help="Restart indiserver")
    server_restart.add_argument("--port", type=int, default=None)
    server_restart.set_defaults(func=_cmd_server_restart)

    driver_parser = subparsers.add_parser("driver", help="Manage INDI drivers")
    driver_sub = driver_parser.add_subparsers(dest="driver_command", required=True)

    driver_list = driver_sub.add_parser("list", help="List the driver catalog")
    driver_list.set_defaults(func=_cmd_driver_list)

    driver_running = driver_sub.add_parser("running", help="List currently running drivers")
    driver_running.set_defaults(func=_cmd_driver_running)

    driver_start = driver_sub.add_parser("start", help="Start a driver by catalog label")
    driver_start.add_argument("label")
    driver_start.set_defaults(func=_cmd_driver_start)

    driver_stop = driver_sub.add_parser("stop", help="Stop a running driver by catalog label")
    driver_stop.add_argument("label")
    driver_stop.set_defaults(func=_cmd_driver_stop)

    listen_parser = subparsers.add_parser(
        "listen", help="Connect to indiserver and print incoming events"
    )
    listen_parser.add_argument("--host", default="localhost")
    listen_parser.add_argument("--port", type=int, default=INDI_PORT)
    listen_parser.add_argument("--device", default=None, help="Only show events for this device")
    listen_parser.set_defaults(func=_cmd_listen)

    return parser


def main() -> None:
    """CLI entrypoint: parse arguments and dispatch to the matching command."""
    logging.basicConfig(level=logging.WARNING)
    parser = _build_parser()
    args = parser.parse_args()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(args.func(args))
