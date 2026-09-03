"""Advertise this server on the local network via Bonjour/mDNS (zeroconf), INDIMCP-140.

Lets a client (e.g. Navi) discover a running `streamable-http`/`sse` server without the
operator typing in a hostname or IP — `server.run` registers a service here at startup and
unregisters it at shutdown. This is server-side advertisement only; the discovery/browsing
side lives in INDIMCPKit, and Navi's UI consumes that (see INDIMCP-140's companion todos).

The service is advertised under the custom type `_indi-mcp._tcp.local.`, with the MCP
endpoint's HTTP path carried in a TXT record — a client resolving the service still needs to
know to append that path (e.g. `/mcp`) to build the full endpoint URL, since Bonjour/mDNS
itself has no notion of an HTTP path.
"""

import logging
import socket
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import cast

import ifaddr
from zeroconf import ServiceInfo, Zeroconf

logger = logging.getLogger(__name__)

SERVICE_TYPE = "_indi-mcp._tcp.local."
"""This server's Bonjour/mDNS service type — custom, since there's no existing standard type
for an MCP server. `INDIMCPKit`'s discovery side must browse for this same string."""

_WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", ""})
"""`--host` values meaning "every interface" rather than one specific address — `ServiceInfo`
needs concrete addresses to advertise, so these are expanded to every non-loopback address this
machine actually has (see `_addresses_for_host`)."""

_LOOPBACK_ADDRESSES = frozenset({"127.0.0.1", "::1"})


@dataclass
class AdvertisedService:
    """A registered Bonjour/mDNS service, as returned by `start_advertising`.

    Holds both the `Zeroconf` instance and the `ServiceInfo` it was registered with, since
    unregistering later (`stop_advertising`) needs both.
    """

    zeroconf: Zeroconf
    info: ServiceInfo


def _addresses_for_host(host: str) -> list[str]:
    """The concrete IPv4/IPv6 addresses to advertise `host` as.

    A specific `host` (e.g. `192.168.1.20`) is advertised as-is. A wildcard `host` (the
    `--host 0.0.0.0` production setup, see `docs/Deployment.md`) has no address of its own to
    advertise, so every non-loopback address this machine has is advertised instead — a
    resolving client tries each until one works, the same way it would for any other
    multi-homed mDNS service.
    """
    if host not in _WILDCARD_HOSTS:
        return [host]
    addresses: set[str] = set()
    for adapter in ifaddr.get_adapters():
        for ip in adapter.ips:
            # ifaddr represents an IPv4 address as a plain str, an IPv6 address as a
            # (address, flowinfo, scope_id) tuple — see the ifaddr.IP.ip docstring.
            address = ip.ip[0] if ip.is_IPv6 else cast(str, ip.ip)
            if address not in _LOOPBACK_ADDRESSES:
                addresses.add(address)
    if not addresses:
        raise OSError(f"no non-loopback network address found to advertise for host={host!r}")
    return sorted(addresses)


def _server_version() -> str:
    try:
        return version("indi-mcp")
    except PackageNotFoundError:
        return "unknown"


def start_advertising(host: str, port: int, path: str = "/mcp") -> AdvertisedService:
    """Register this server as a Bonjour/mDNS service, advertising `host`:`port`:`path`.

    Raises whatever `zeroconf` itself raises (e.g. `OSError` if no multicast-capable network
    interface is available) — see `try_start_advertising` for the best-effort wrapper `run()`
    actually uses, so a Bonjour failure never prevents the MCP server itself from starting.
    """
    hostname = socket.gethostname().removesuffix(".local").removesuffix(".local.")
    instance_name = f"{hostname}.{SERVICE_TYPE}"
    addresses = _addresses_for_host(host)
    packed_addresses = [
        socket.inet_pton(socket.AF_INET6 if ":" in addr else socket.AF_INET, addr)
        for addr in addresses
    ]
    info = ServiceInfo(
        SERVICE_TYPE,
        instance_name,
        addresses=packed_addresses,
        port=port,
        properties={"path": path, "version": _server_version()},
        server=f"{hostname}.local.",
    )
    zeroconf = Zeroconf()
    try:
        # allow_name_change=True: if another device on the LAN already advertises under this
        # same hostname-derived instance name (e.g. two Pis cloned from the same SD-card
        # image, both still called "raspberrypi"), zeroconf renames this one to stay unique
        # rather than raising NonUniqueNameException — a discoverable renamed service beats
        # no service at all, which is what a raised exception here would end up as (caught by
        # try_start_advertising).
        zeroconf.register_service(info, allow_name_change=True)
    except Exception:
        zeroconf.close()
        raise
    logger.info(
        "Advertising indi-mcp via Bonjour/mDNS as %r (addresses=%s, port=%d, path=%s)",
        instance_name,
        addresses,
        port,
        path,
    )
    return AdvertisedService(zeroconf=zeroconf, info=info)


def try_start_advertising(host: str, port: int, path: str = "/mcp") -> AdvertisedService | None:
    """`start_advertising`, but a failure is logged and swallowed rather than raised.

    Bonjour/mDNS advertisement is a convenience for client discovery, not something the MCP
    server itself depends on — a network without multicast support (some container/VPN setups)
    or a machine with no non-loopback address shouldn't prevent the actual server from
    starting, so any failure here is a safety net rather than a fatal error, matching this
    codebase's other best-effort background behavior (e.g. `frame_store`'s WCS header write).
    """
    try:
        return start_advertising(host, port, path)
    except Exception:
        logger.warning(
            "Failed to advertise indi-mcp via Bonjour/mDNS; continuing without it", exc_info=True
        )
        return None


def stop_advertising(service: AdvertisedService) -> None:
    """Unregister and tear down a service `start_advertising`/`try_start_advertising` returned.

    Best-effort like registration itself: shutdown shouldn't hang or crash because the
    network is already gone (e.g. Wi-Fi dropped, or the interface was torn down) — an
    unreachable multicast group at this point isn't worth failing the server's shutdown over.
    """
    try:
        service.zeroconf.unregister_service(service.info)
    except Exception:
        logger.warning("Failed to unregister Bonjour/mDNS service during shutdown", exc_info=True)
    finally:
        service.zeroconf.close()
