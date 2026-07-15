import sys

import pytest

import indi_mcp
from indi_mcp.server import mcp


def test_package_is_importable() -> None:
    assert indi_mcp is not None


def test_main_is_callable() -> None:
    assert callable(indi_mcp.main)


def test_server_has_expected_name() -> None:
    assert mcp.name == "indi-mcp"


def test_main_defaults_to_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(indi_mcp, "run", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(sys, "argv", ["indi-mcp"])

    indi_mcp.main()

    assert calls == [{"transport": "stdio", "host": "127.0.0.1", "port": 8000}]


def test_main_parses_transport_host_and_port(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(indi_mcp, "run", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(
        sys,
        "argv",
        ["indi-mcp", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "9000"],
    )

    indi_mcp.main()

    assert calls == [{"transport": "streamable-http", "host": "0.0.0.0", "port": 9000}]
