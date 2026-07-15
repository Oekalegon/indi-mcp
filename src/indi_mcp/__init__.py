"""INDI MCP server: an MCP server that controls astrophotography equipment via INDI."""

import argparse

from indi_mcp.server import Transport, run


def main() -> None:
    """CLI entrypoint: parse arguments and start the MCP server."""
    parser = argparse.ArgumentParser(
        prog="indi-mcp",
        description="An MCP server that controls astrophotography equipment via INDI.",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "sse", "streamable-http"),
        default="stdio",
        help="MCP transport to serve over (default: stdio)",
    )
    args = parser.parse_args()
    transport: Transport = args.transport
    run(transport=transport)
