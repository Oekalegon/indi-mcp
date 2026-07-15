# INDI MCP Server

An [MCP](https://modelcontextprotocol.io) server that controls astrophotography equipment via [INDI](https://indilib.org) on a Raspberry Pi (or equivalent device), and exposes it to other computers on the local network.

## What it does

* **Equipment control** — connect to an INDI server and control mounts, cameras, filter wheels, focusers and other astrophotography gear through MCP tools.
* **INDI server management** — manage the INDI server itself: install/remove drivers, and start, stop or restart the server, all via MCP.
* **Frame storage** — captured frames are stored on the Raspberry Pi and can be listed, inspected and transferred to another computer on the local network via MCP.

## Documentation

* [Design Document](docs/Design.md)
* [Deployment](docs/Deployment.md) — running the server locally (`stdio`) vs. as a
  systemd service on the Raspberry Pi (`streamable-http`)
* [Rig YAML Schema](docs/RigSchema.md) — field reference for `rigs/*.yaml` imaging rig
  definitions

## Debug CLI

`indi-mcp-cli` is a small standalone tool for manually testing/debugging the INDI server and
driver management tools, without needing an MCP client:

```bash
uv run indi-mcp-cli server status
uv run indi-mcp-cli server start --port 7624
uv run indi-mcp-cli driver list
uv run indi-mcp-cli driver start "CCD Simulator"
uv run indi-mcp-cli listen --device "CCD Simulator"   # prints incoming events until Ctrl+C
```

The `driver` subcommands read the driver catalog from `/usr/share/indi/` by default, which only
exists where INDI's drivers are actually installed (e.g. the Raspberry Pi). On a machine with a
local INDI install elsewhere (e.g. Homebrew on macOS, typically `/usr/local/share/indi`), point
`indiweb` at it via the `INDI_DATA_DIR` env var:

```bash
export INDI_DATA_DIR=/usr/local/share/indi
uv run indi-mcp-cli driver list
```

## Status

Early setup stage — MCP server skeleton in place, with INDI server management tools (start/stop/restart/status) implemented.

## Tech stack

* Python 3.12+, managed with [uv](https://docs.astral.sh/uv/)
* [Official Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk) (`mcp`)
* [`indipyclient`](https://indipyclient.readthedocs.io) — pure-Python INDI client, used for equipment control
* [`indiweb`](https://pypi.org/project/indiweb/) — used as a library only (its `IndiServer`/`DriverCollection` classes), for `indiserver` process/FIFO control and driver-catalog parsing; its bundled web app is not used
* [Ruff](https://docs.astral.sh/ruff/) for linting and formatting
* [ty](https://github.com/astral-sh/ty) for static type checking
* [pytest](https://docs.pytest.org/), with `pytest-asyncio` and `pytest-cov`
* [pre-commit](https://pre-commit.com/) to run the above on every commit

## Development setup

```bash
uv sync --dev
uv run pre-commit install
```

Common tasks:

```bash
uv run ruff check .          # lint
uv run ruff format .         # format
uv run ty check .            # type-check
uv run pytest --cov          # test
```

## Contributing / branching model

This project follows a git-flow-style workflow:

* `main` — always releasable; only accepts merges from `release/*` or `hotfix/*` branches.
* `develop` — integration branch for ongoing work.
* `feature/*` — branched from and merged back into `develop`.

A CI check (`enforce-merge-policy`) rejects pull requests that don't follow these rules.
