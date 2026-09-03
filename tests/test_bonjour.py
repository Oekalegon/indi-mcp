from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from indi_mcp import bonjour


@dataclass
class _FakeIp:
    ip: object
    is_IPv4: bool
    is_IPv6: bool = False


@dataclass
class _FakeAdapter:
    ips: list[_FakeIp]


def test_addresses_for_host_returns_the_given_host_when_not_a_wildcard() -> None:
    assert bonjour._addresses_for_host("192.168.1.20") == ["192.168.1.20"]


def test_addresses_for_host_expands_a_wildcard_host_to_every_non_loopback_ipv4_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapters = [
        _FakeAdapter(ips=[_FakeIp(ip="127.0.0.1", is_IPv4=True)]),
        _FakeAdapter(ips=[_FakeIp(ip="192.168.1.20", is_IPv4=True)]),
    ]
    monkeypatch.setattr(bonjour.ifaddr, "get_adapters", lambda: adapters)

    assert bonjour._addresses_for_host("0.0.0.0") == ["192.168.1.20"]


def test_addresses_for_host_includes_non_loopback_ipv6_addresses_for_a_wildcard_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapters = [
        _FakeAdapter(ips=[_FakeIp(ip=("::1", 0, 0), is_IPv4=False, is_IPv6=True)]),
        _FakeAdapter(
            ips=[
                _FakeIp(ip="192.168.1.20", is_IPv4=True),
                _FakeIp(ip=("fe80::1", 0, 0), is_IPv4=False, is_IPv6=True),
            ]
        ),
    ]
    monkeypatch.setattr(bonjour.ifaddr, "get_adapters", lambda: adapters)

    assert bonjour._addresses_for_host("::") == ["192.168.1.20", "fe80::1"]


def test_addresses_for_host_raises_when_no_non_loopback_address_is_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapters = [
        _FakeAdapter(
            ips=[
                _FakeIp(ip="127.0.0.1", is_IPv4=True),
                _FakeIp(ip=("::1", 0, 0), is_IPv4=False, is_IPv6=True),
            ]
        )
    ]
    monkeypatch.setattr(bonjour.ifaddr, "get_adapters", lambda: adapters)

    with pytest.raises(OSError, match="no non-loopback"):
        bonjour._addresses_for_host("0.0.0.0")


def test_start_advertising_registers_a_service_with_the_expected_type_and_properties(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    zeroconf_instance = MagicMock()
    monkeypatch.setattr(bonjour, "Zeroconf", MagicMock(return_value=zeroconf_instance))
    monkeypatch.setattr(bonjour.socket, "gethostname", lambda: "telescope.local")

    service = bonjour.start_advertising("192.168.1.20", 8000, path="/mcp")

    zeroconf_instance.register_service.assert_called_once_with(
        service.info, allow_name_change=True
    )
    assert service.info.type == bonjour.SERVICE_TYPE
    assert service.info.name == f"telescope.{bonjour.SERVICE_TYPE}"
    assert service.info.port == 8000
    assert service.info.properties[b"path"] == b"/mcp"
    zeroconf_instance.close.assert_not_called()


@pytest.mark.parametrize(
    ("raw_hostname", "expected_instance_hostname"),
    [
        ("telescope", "telescope"),
        ("telescope.local", "telescope"),
        ("telescope.local.", "telescope"),
    ],
)
def test_start_advertising_strips_a_local_suffix_from_the_hostname_if_present(
    monkeypatch: pytest.MonkeyPatch, raw_hostname: str, expected_instance_hostname: str
) -> None:
    zeroconf_instance = MagicMock()
    monkeypatch.setattr(bonjour, "Zeroconf", MagicMock(return_value=zeroconf_instance))
    monkeypatch.setattr(bonjour.socket, "gethostname", lambda: raw_hostname)

    service = bonjour.start_advertising("192.168.1.20", 8000)

    assert service.info.name == f"{expected_instance_hostname}.{bonjour.SERVICE_TYPE}"
    assert service.info.server == f"{expected_instance_hostname}.local."


def test_start_advertising_closes_zeroconf_and_reraises_on_registration_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    zeroconf_instance = MagicMock()
    zeroconf_instance.register_service.side_effect = OSError("no multicast interface")
    monkeypatch.setattr(bonjour, "Zeroconf", MagicMock(return_value=zeroconf_instance))

    with pytest.raises(OSError, match="no multicast interface"):
        bonjour.start_advertising("192.168.1.20", 8000)

    zeroconf_instance.close.assert_called_once()


def test_try_start_advertising_returns_none_and_swallows_a_registration_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(*args: object, **kwargs: object) -> bonjour.AdvertisedService:
        raise OSError("no multicast interface")

    monkeypatch.setattr(bonjour, "start_advertising", _raise)

    assert bonjour.try_start_advertising("192.168.1.20", 8000) is None


def test_try_start_advertising_returns_the_service_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = bonjour.AdvertisedService(zeroconf=MagicMock(), info=MagicMock())
    monkeypatch.setattr(bonjour, "start_advertising", lambda *args, **kwargs: expected)

    assert bonjour.try_start_advertising("192.168.1.20", 8000) is expected


def test_stop_advertising_unregisters_and_closes() -> None:
    zeroconf_instance = MagicMock()
    service = bonjour.AdvertisedService(zeroconf=zeroconf_instance, info=MagicMock())

    bonjour.stop_advertising(service)

    zeroconf_instance.unregister_service.assert_called_once_with(service.info)
    zeroconf_instance.close.assert_called_once()


def test_stop_advertising_still_closes_when_unregister_raises() -> None:
    zeroconf_instance = MagicMock()
    zeroconf_instance.unregister_service.side_effect = OSError("network gone")
    service = bonjour.AdvertisedService(zeroconf=zeroconf_instance, info=MagicMock())

    bonjour.stop_advertising(service)

    zeroconf_instance.close.assert_called_once()
