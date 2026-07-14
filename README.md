# INDI MCP Server

An [MCP](https://modelcontextprotocol.io) server that controls astrophotography equipment via [INDI](https://indilib.org) on a Raspberry Pi (or equivalent device), and exposes it to other computers on the local network.

## What it does

* **Equipment control** — connect to an INDI server and control mounts, cameras, filter wheels, focusers and other astrophotography gear through MCP tools.
* **INDI server management** — manage the INDI server itself: install/remove drivers, and start, stop or restart the server, all via MCP.
* **Frame storage** — captured frames are stored on the Raspberry Pi and can be listed, inspected and transferred to another computer on the local network via MCP.

## Status

Early setup stage — project scaffolding only, no functionality implemented yet.

## Tech stack

* Python 3.12+, managed with [uv](https://docs.astral.sh/uv/)
* [Official Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk) (`mcp`)
* [`indipyclient`](https://indipyclient.readthedocs.io) — pure-Python INDI client
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
