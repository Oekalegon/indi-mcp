import indi_mcp
from indi_mcp.server import mcp


def test_package_is_importable() -> None:
    assert indi_mcp is not None


def test_main_is_callable() -> None:
    assert callable(indi_mcp.main)


def test_server_has_expected_name() -> None:
    assert mcp.name == "indi-mcp"
