"""Version and build identification for the running server.

The version number is the package version, bumped by hand in
`pyproject.toml` on releases. The build timestamp is a rough, automatic
stand-in for a commit id: it records when `develop` was last merged, written
by `.github/workflows/build-timestamp.yml` into `_build_info.py`, which this
module reads. It is `None` on a checkout CI hasn't run against yet (e.g. a
fresh local clone before the first merge).
"""

from importlib.metadata import PackageNotFoundError, version
from typing import TypedDict

from indi_mcp import _build_info

__all__ = ["ServerInfo", "get_server_info"]


class ServerInfo(TypedDict):
    """Version and build identification for the running server."""

    version: str
    buildTimestamp: str | None


def get_server_info() -> ServerInfo:
    """Report the running server's package version and last-merge build timestamp.

    `importlib.metadata.version` does synchronous filesystem I/O, but it's a
    cheap one-off dist-info read on a tool a client calls rarely, not a hot
    path — not worth `asyncio.to_thread`'s overhead/complexity for this.
    """
    try:
        package_version = version("indi-mcp")
    except PackageNotFoundError:
        package_version = "unknown"
    return {
        "version": package_version,
        "buildTimestamp": _build_info.BUILD_TIMESTAMP,
    }
