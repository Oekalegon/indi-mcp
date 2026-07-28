"""Managing astrometry.net index files (INDIMCP-77): checking which are installed under
`INDI_MCP_ASTROMETRY_INDEX_DIR`, and downloading whichever are missing from
`data.astrometry.net`.

**Scoped to the 4100-series (Tycho-2 based) index files only.** That series covers field
diameters from 22 arcmin to 33 degrees — the range that covers the overwhelming majority of
amateur imaging setups (a few hundred mm to a few meters of focal length with a typical
modern CMOS/CCD sensor) — as a single file per scale, hosted directly under
`data.astrometry.net/4100/` with no sharding. Narrower fields (very long focal length + small
pixels) need the 5200-series, which is sharded into many per-healpix files and hosted on a
different server (`portal.nersc.gov`) — deliberately out of scope here for now; a setup that
narrow still works with `plate_solve`, it just needs those index files installed by some
other means until this module grows support for that series too.

`solve()` (`plate_solver.py`) is the actual consumer of whatever ends up in the configured
index directory: if `INDEX_DIR_ENV` is set, it points `solve-field` at that directory via a
small generated `astrometry.cfg` (`ensure_astrometry_config` below) — `solve-field` itself
has no `--index-dir` flag; index-file location is only configurable through `add_path`
directives in a config file, passed with `--config`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import urllib.request
from pathlib import Path
from typing import TypedDict

from indi_mcp import rig_store

logger = logging.getLogger(__name__)

__all__ = [
    "INDEX_BASE_URL_ENV",
    "INDEX_DIR_ENV",
    "IndexFileStatus",
    "download_index_files",
    "ensure_astrometry_config",
    "field_of_view_arcmin_for_rig",
    "index_numbers_for_field_of_view",
    "list_index_files",
]

INDEX_DIR_ENV = "INDI_MCP_ASTROMETRY_INDEX_DIR"
_DEFAULT_INDEX_DIR = Path("astrometry_index")

INDEX_BASE_URL_ENV = "INDI_MCP_ASTROMETRY_INDEX_BASE_URL"
_DEFAULT_INDEX_BASE_URL = "https://data.astrometry.net/4100"

_DOWNLOAD_TIMEOUT_SECONDS = 30.0
"""Timeout for the *connection* (and each individual read), not the whole download — a
165 MB file over a slow home connection can legitimately take minutes, but a stalled
connection that never delivers another byte within this long shouldn't hang forever."""

_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
"""Streamed to disk this many bytes at a time — index files run up to ~165 MB, and this
project cares about not fully buffering large binary data in memory on a resource-
constrained Pi (the same reasoning `frame://{frameId}`'s own docstring discusses, just
applied to a download instead of a read)."""

_download_locks: dict[Path, asyncio.Lock] = {}
"""Serializes `download_index_files` per destination path — see that function's own
docstring for the check-then-act race this prevents. Never cleaned up, but bounded: at most
one entry per known 4100-series index number (13 total) per directory ever downloaded into,
across the server's whole lifetime — not worth the complexity of pruning."""

# index number -> (min, max) field diameter in arcminutes a solve should be narrowed to
# before this index is a good match — from astrometry.net's own reference table
# (https://astrometry.net/doc/readme.html), matching the 4100-series' actual scale range.
_INDEX_SCALE_RANGES_ARCMIN: dict[int, tuple[float, float]] = {
    7: (22.0, 30.0),
    8: (30.0, 42.0),
    9: (42.0, 60.0),
    10: (60.0, 85.0),
    11: (85.0, 120.0),
    12: (120.0, 170.0),
    13: (170.0, 240.0),
    14: (240.0, 340.0),
    15: (340.0, 480.0),
    16: (480.0, 680.0),
    17: (680.0, 1000.0),
    18: (1000.0, 1400.0),
    19: (1400.0, 2000.0),
}


class IndexFileStatus(TypedDict):
    """One known 4100-series index file's identity, coverage, and installed state."""

    indexNumber: int
    filename: str
    minArcmin: float
    maxArcmin: float
    installed: bool
    sizeBytes: int | None
    neededForRig: bool | None
    """Whether this index's range overlaps the rig's own field of view — `None` if
    `list_index_files` wasn't given a rig to check against."""


def _index_dir(directory: Path | None) -> Path:
    if directory is not None:
        return directory
    return Path(os.environ.get(INDEX_DIR_ENV, _DEFAULT_INDEX_DIR))


def _index_base_url() -> str:
    return os.environ.get(INDEX_BASE_URL_ENV, _DEFAULT_INDEX_BASE_URL)


def _index_filename(index_number: int) -> str:
    return f"index-41{index_number:02d}.fits"


def _index_url(index_number: int) -> str:
    return f"{_index_base_url()}/{_index_filename(index_number)}"


def field_of_view_arcmin_for_rig(rig: rig_store.Rig) -> tuple[float, float]:
    """The rig's own configured `(min, max)` field-of-view diameter, in arcminutes — from
    its `"telescope"` component's `focalLengthMm` and `"camera"` component's
    `pixelSizeMicron`/`pixelsX`/`pixelsY`, the same optics configuration
    `script_engine._add_telescope_optics_fields`'s `SCALE` header and `plate_solve`'s own
    scale hint already use (`scale = 206.265 * pixelSizeMicron / focalLengthMm`,
    arcsec/pixel). Raises `ValueError` if the rig doesn't have both configured — there's
    nothing to compute a field of view from otherwise.
    """
    telescope = next((c for c in rig.components if c.role == "telescope"), None)
    camera = next((c for c in rig.components if c.role == "camera"), None)
    if telescope is None or telescope.focalLengthMm is None:
        raise ValueError(f"rig {rig.id!r} has no telescope with focalLengthMm configured")
    if (
        camera is None
        or camera.pixelSizeMicron is None
        or camera.pixelsX is None
        or camera.pixelsY is None
    ):
        raise ValueError(
            f"rig {rig.id!r}'s camera has no pixelSizeMicron/pixelsX/pixelsY configured"
        )
    scale_arcsec_per_pixel = 206.265 * camera.pixelSizeMicron / telescope.focalLengthMm
    width_arcmin = camera.pixelsX * scale_arcsec_per_pixel / 60.0
    height_arcmin = camera.pixelsY * scale_arcsec_per_pixel / 60.0
    return (min(width_arcmin, height_arcmin), max(width_arcmin, height_arcmin))


def index_numbers_for_field_of_view(min_arcmin: float, max_arcmin: float) -> list[int]:
    """Every known 4100-series index number whose own coverage range overlaps
    `[min_arcmin, max_arcmin]` — a simple overlap test, not the more precise "quad size
    10%-100% of image size" astrometry.net's own docs suggest, but adequate for picking a
    reasonable set of index files to have installed for a given field of view.
    """
    return sorted(
        index_number
        for index_number, (lo, hi) in _INDEX_SCALE_RANGES_ARCMIN.items()
        if hi >= min_arcmin and lo <= max_arcmin
    )


def list_index_files(
    *, directory: Path | None = None, rig: rig_store.Rig | None = None
) -> list[IndexFileStatus]:
    """List every known 4100-series index file and whether it's installed under
    `directory` (defaults to `INDEX_DIR_ENV`, falling back to `./astrometry_index`).

    If `rig` is given, each entry's `neededForRig` flags whether that index's range
    actually overlaps the rig's own configured field of view (`field_of_view_arcmin_for_rig`)
    — so "missing but irrelevant to this rig" can be told apart from "missing and needed."
    `None` throughout if `rig` isn't given, or if it has no computable field of view.
    """
    resolved_dir = _index_dir(directory)
    needed: set[int] | None = None
    if rig is not None:
        try:
            min_arcmin, max_arcmin = field_of_view_arcmin_for_rig(rig)
        except ValueError:
            needed = None
        else:
            needed = set(index_numbers_for_field_of_view(min_arcmin, max_arcmin))

    statuses: list[IndexFileStatus] = []
    for index_number in sorted(_INDEX_SCALE_RANGES_ARCMIN):
        min_arcmin, max_arcmin = _INDEX_SCALE_RANGES_ARCMIN[index_number]
        path = resolved_dir / _index_filename(index_number)
        installed = path.is_file()
        statuses.append(
            {
                "indexNumber": index_number,
                "filename": _index_filename(index_number),
                "minArcmin": min_arcmin,
                "maxArcmin": max_arcmin,
                "installed": installed,
                "sizeBytes": path.stat().st_size if installed else None,
                "neededForRig": (index_number in needed) if needed is not None else None,
            }
        )
    return statuses


def _download_one_index_file(index_number: int, directory: Path) -> None:
    """Blocking: download one index file, streamed straight to disk in
    `_DOWNLOAD_CHUNK_BYTES`-sized chunks (never buffered whole in memory — these run up to
    ~165 MB) to a `.part` temp name, renamed to its real filename only once the whole
    download succeeds — so a truncated/interrupted download is never mistaken for a valid,
    complete index file the next time `list_index_files`/`solve-field` looks for it.
    """
    directory.mkdir(parents=True, exist_ok=True)
    dest = directory / _index_filename(index_number)
    tmp = dest.with_name(dest.name + ".part")
    url = _index_url(index_number)
    logger.info("Downloading astrometry.net index %s from %s", dest.name, url)
    try:
        with (
            urllib.request.urlopen(url, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as response,
            tmp.open("wb") as f,
        ):
            while True:
                chunk = response.read(_DOWNLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                f.write(chunk)
        tmp.rename(dest)
    finally:
        tmp.unlink(missing_ok=True)
    logger.info("Downloaded astrometry.net index %s (%d bytes)", dest.name, dest.stat().st_size)


async def download_index_files(
    index_numbers: list[int], *, directory: Path | None = None
) -> list[int]:
    """Download whichever of `index_numbers` aren't already installed under `directory`
    (same default as `list_index_files`). Returns the index numbers actually downloaded —
    an already-installed one is left alone, not re-downloaded.

    Raises `ValueError` for any index number outside the known 4100-series range (7-19)
    *before* downloading anything, so a typo in a longer list doesn't waste bandwidth on
    the valid ones before failing on the last.

    Sequential, not concurrent: these are large files (up to ~165 MB) on what's typically a
    modest home/site internet connection — downloading several at once would just contend
    for the same bandwidth with no real speedup, only a higher chance of a partial-progress
    failure. Each download runs via `asyncio.to_thread` (`_download_one_index_file`), so it
    never blocks the event loop other script runs/device messaging depend on.

    Serialized per destination path (`_download_locks`) against a *second* caller racing the
    same missing index — e.g. two script runs on similar rigs, or an MCP client that times
    out waiting for a ~165 MB download and retries the same tool call while the first is
    still in flight. Without this, both callers would pass the `is_file()` check and both
    open the same `.part` path with `"wb"` (which truncates on open), corrupting whichever
    download was already in progress — the same check-then-act race `script_store.py`'s own
    `_save_script_lock` exists to prevent for concurrent script uploads.
    """
    unknown = [n for n in index_numbers if n not in _INDEX_SCALE_RANGES_ARCMIN]
    if unknown:
        raise ValueError(f"unknown 4100-series index number(s) {unknown} (expected 7-19)")

    resolved_dir = _index_dir(directory)
    downloaded: list[int] = []
    for index_number in index_numbers:
        dest = resolved_dir / _index_filename(index_number)
        lock = _download_locks.setdefault(dest, asyncio.Lock())
        async with lock:
            if dest.is_file():
                continue
            await asyncio.to_thread(_download_one_index_file, index_number, resolved_dir)
            downloaded.append(index_number)
    return downloaded


def ensure_astrometry_config(directory: Path) -> Path:
    """Write (or refresh) a minimal `astrometry.cfg` pointing `solve-field` at `directory`
    for index files, returning its path.

    `solve-field` has no `--index-dir`-style flag of its own — index-file location is only
    configurable via `add_path` directives in a config file, passed with `--config`
    (`plate_solver.solve`). Regenerated on every call rather than cached/checked for
    staleness: it's a two-line text file, cheap enough that "always correct" is simpler than
    any caching scheme, and it must exist before `solve-field` can use it regardless.
    """
    directory.mkdir(parents=True, exist_ok=True)
    config_path = directory / ".indi-mcp-astrometry.cfg"
    config_path.write_text(f"add_path {directory.resolve()}\nautoindex\n")
    return config_path
