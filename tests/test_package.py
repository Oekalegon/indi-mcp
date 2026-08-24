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


@pytest.mark.parametrize(
    ("tool_name", "required_params_by_action_constant_name"),
    [
        ("mount_action", "_MOUNT_ACTION_PARAMS"),
        ("camera_action", "_CAMERA_ACTION_REQUIRED_PARAMS"),
        ("frames", "_FRAMES_REQUIRED_PARAMS"),
        ("manage_frame", "_MANAGE_FRAME_REQUIRED_PARAMS"),
    ],
)
async def test_action_tool_schema_exposes_required_params_by_action(
    tool_name: str, required_params_by_action_constant_name: str
) -> None:
    """Every `*_action`/discriminated tool merged from several single-purpose tools hits the
    same schema gap: a parameter required only for one `action` value can't express that
    conditional required-ness in the tool's top-level `required` list, so it's optional at the
    schema level regardless of `action`, with no fallback default if omitted (first found in
    `mount_action`/`camera_action`, INDIMCP-116, PR review of #91; recurred in `frames`/
    `manage_frame`, INDIMCP-120, PR review of #94). The real per-`action` required set is
    instead attached to `action`'s own schema under `requiredParamsByAction`, restoring the
    visibility a schema-reading caller lost when these tools were merged from one-tool-per-
    action/script wrappers.
    """
    import indi_mcp.server as server_module

    expected = getattr(server_module, required_params_by_action_constant_name)

    tools = await mcp.list_tools()
    tool = next(tool for tool in tools if tool.name == tool_name)
    required_by_action = tool.inputSchema["properties"]["action"]["requiredParamsByAction"]

    assert set(required_by_action) == set(expected)
    for action, params in expected.items():
        assert set(required_by_action[action]) == params


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
