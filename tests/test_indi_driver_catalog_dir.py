"""`_get_catalog`'s `DRIVER_CATALOG_DIR_ENV` override (INDIMCP-128).

Deliberately its own file rather than living in `test_indi_driver.py`: that file's `mocks`
fixture is `autouse=True` and replaces `_get_catalog` itself with a stub, which would hide the
exact behavior under test here.
"""

import pytest

from indi_mcp import indi_driver


@pytest.fixture(autouse=True)
def _reset_catalog_singleton() -> None:
    indi_driver._catalog = None


def test_get_catalog_uses_env_var_path_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(indi_driver.DRIVER_CATALOG_DIR_ENV, "/opt/homebrew/share/indi")
    captured: dict[str, str] = {}
    monkeypatch.setattr(
        indi_driver, "DriverCollection", lambda path: captured.setdefault("path", path)
    )

    indi_driver._get_catalog()

    assert captured["path"] == "/opt/homebrew/share/indi"


def test_get_catalog_falls_back_to_indi_data_dir_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(indi_driver.DRIVER_CATALOG_DIR_ENV, raising=False)
    captured: dict[str, str] = {}
    monkeypatch.setattr(
        indi_driver, "DriverCollection", lambda path: captured.setdefault("path", path)
    )

    indi_driver._get_catalog()

    assert captured["path"] == indi_driver.INDI_DATA_DIR


def test_get_catalog_caches_across_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(indi_driver.DRIVER_CATALOG_DIR_ENV, "/opt/homebrew/share/indi")
    call_count = 0

    def fake_driver_collection(path: str) -> str:
        nonlocal call_count
        call_count += 1
        return path

    monkeypatch.setattr(indi_driver, "DriverCollection", fake_driver_collection)

    first = indi_driver._get_catalog()
    second = indi_driver._get_catalog()

    assert first is second
    assert call_count == 1
