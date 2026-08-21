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


async def test_get_server_info_is_registered_as_a_tool() -> None:
    tools = await mcp.list_tools()

    assert "get_server_info" in [tool.name for tool in tools]


async def test_configuration_config_schema_exposes_the_real_per_kind_shape() -> None:
    """`configuration`'s `config` parameter is a plain `dict[str, Any]` at runtime (INDIMCP-115)
    — its schema-fidelity is only recoverable via `_CONFIG_SCHEMA_BY_KIND`'s `json_schema_extra`
    (see `server.py`'s `_defuse_schema_refs`). Regression coverage for two failure modes hit
    while building that: embedding a schema with an un-rebased `$ref`/`$defs` breaks pydantic's
    *own* schema generation for the whole `configuration` tool (a `KeyError` at import time,
    not just a wrong schema), and naively inlining `Script`'s genuinely self-referential step
    schema recurses infinitely. Both would currently crash collecting this very test module, so
    this mostly documents *why* `mcp.list_tools()` succeeding at all is the real assertion —
    the schema-shape checks below are the added value once it does.
    """
    tools = await mcp.list_tools()
    configuration_tool = next(tool for tool in tools if tool.name == "configuration")

    by_kind = configuration_tool.inputSchema["properties"]["config"]["schemaByKind"]

    assert set(by_kind) == {"rig", "observatory", "script"}
    assert set(by_kind["rig"]["required"]) == {"id", "name", "components"}
    assert set(by_kind["observatory"]["required"]) == {
        "id",
        "name",
        "latitudeDeg",
        "longitudeDeg",
    }
    assert "id" in by_kind["script"]["required"]

    # No reserved $ref/$defs keys leaked through anywhere -- those are exactly what broke
    # pydantic's own schema generation for this tool before `_defuse_schema_refs` existed.
    for schema in by_kind.values():
        dumped = str(schema)
        assert "'$ref'" not in dumped
        assert "'$defs'" not in dumped


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
