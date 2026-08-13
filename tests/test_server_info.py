import pytest

from indi_mcp import _build_info, server_info


def test_get_server_info_reports_package_version_and_build_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server_info, "version", lambda _name: "1.2.3")
    monkeypatch.setattr(_build_info, "BUILD_TIMESTAMP", "2026-08-13T10:00:00Z")

    info = server_info.get_server_info()

    assert info == {"version": "1.2.3", "buildTimestamp": "2026-08-13T10:00:00Z"}


def test_get_server_info_reports_none_build_timestamp_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server_info, "version", lambda _name: "1.2.3")
    monkeypatch.setattr(_build_info, "BUILD_TIMESTAMP", None)

    info = server_info.get_server_info()

    assert info["buildTimestamp"] is None


def test_get_server_info_falls_back_when_package_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_not_found(_name: str) -> str:
        raise server_info.PackageNotFoundError

    monkeypatch.setattr(server_info, "version", raise_not_found)

    info = server_info.get_server_info()

    assert info["version"] == "unknown"
