"""Managing astrometry.net index files (INDIMCP-77): checking which are installed under
`INDI_MCP_ASTROMETRY_INDEX_DIR`, and downloading whichever are missing from
`data.astrometry.net`.

**Two catalogs supported: `"tycho2"` (the 4100-series) and `"2mass"` (the 4200-series)** —
matching the two choices Ekos's own index-file downloader has historically offered. A third,
narrower-field option (the Gaia-based 5200-series) exists at astrometry.net but is hosted
separately at `portal.nersc.gov`, which doesn't currently respond at all (verified directly,
not just from this server) — deliberately left out until that host is reachable again to
confirm its real file layout, rather than guessing at a healpix-sharding scheme for a feature
that downloads real files onto a real Pi.

Both supported catalogs share one universal "scale number" (0-19) -> field-diameter-in-
arcminutes table (`_SCALE_RANGES_ARCMIN`, from astrometry.net's own reference table,
https://astrometry.net/doc/readme.html) — scale is a property of the geometric quad size, not
of which catalog backs it. They differ in:

- **Which scales are published at all.** `tycho2` only ships scales 7-19 (wide fields); the
  narrower scales 0-6 are meant to be covered by the Gaia-based 5200-series instead (see
  above). `2mass` ships the full 0-19 range.
- **Sharding.** `tycho2` is one file per scale, hosted directly under `data.astrometry.net/
  4100/`. `2mass` shards its narrower scales into many per-healpix files (48 files for scales
  0-4, 12 for scales 5-7) under `data.astrometry.net/4200/`; scales 8-19 are single files,
  same as `tycho2`'s own shape. Both facts verified directly against the real directory
  listings, not assumed from a pattern.

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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypedDict, get_args

from indi_mcp import rig_store

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_RIG_MARGIN_SCALES",
    "INDEX_BASE_URL_ENV",
    "INDEX_DIR_ENV",
    "Catalog",
    "IndexFileStatus",
    "download_index_files",
    "ensure_astrometry_config",
    "field_of_view_arcmin_for_rig",
    "index_numbers_for_field_of_view",
    "list_index_files",
]

Catalog = Literal["tycho2", "2mass"]

INDEX_DIR_ENV = "INDI_MCP_ASTROMETRY_INDEX_DIR"
_DEFAULT_INDEX_DIR = Path("astrometry_index")

INDEX_BASE_URL_ENV = "INDI_MCP_ASTROMETRY_INDEX_BASE_URL"
"""Override the base URL (scheme+host, no catalog path) index files are downloaded from —
e.g. for a local mirror. Each catalog's own path (`/4100`, `/4200`) is appended to this."""
_DEFAULT_INDEX_BASE_URL = "https://data.astrometry.net"

_DOWNLOAD_TIMEOUT_SECONDS = 30.0
"""Timeout for the *connection* (and each individual read), not the whole download — a
165 MB file over a slow home connection can legitimately take minutes, but a stalled
connection that never delivers another byte within this long shouldn't hang forever."""

_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
"""Streamed to disk this many bytes at a time — index files run up to ~165 MB, and this
project cares about not fully buffering large binary data in memory on a resource-
constrained Pi (the same reasoning `server.download_frame`'s own docstring discusses for
captured frames, INDIMCP-89, just applied to a download instead of an upload/read)."""

DEFAULT_RIG_MARGIN_SCALES = 1
"""Default `index_numbers_for_field_of_view`'s `margin_scales` for a rig-derived field of
view (`list_index_files`'/`server.download_astrometry_index_files`'s `rig`/`rig_id` path) —
one extra scale on each side of the exact bracket, covering a rig's own optics numbers being
a slight underestimate or overestimate without installing the entire catalog."""

_download_locks: dict[Path, asyncio.Lock] = {}
"""Serializes downloads per destination path — see `download_index_files`'s own docstring
for the check-then-act race this prevents. Never cleaned up, but bounded: at most one entry
per (catalog, scale, shard) file this server ever downloads, across its whole lifetime — not
worth the complexity of pruning."""

# scale number -> (min, max) field diameter in arcminutes a solve should be narrowed to
# before this scale is a good match — from astrometry.net's own reference table
# (https://astrometry.net/doc/readme.html). Universal across catalogs (see module docstring).
_SCALE_RANGES_ARCMIN: dict[int, tuple[float, float]] = {
    0: (2.0, 2.8),
    1: (2.8, 4.0),
    2: (4.0, 5.6),
    3: (5.6, 8.0),
    4: (8.0, 11.0),
    5: (11.0, 16.0),
    6: (16.0, 22.0),
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


@dataclass(frozen=True)
class _CatalogSpec:
    prefix: int
    """The 4-digit index-number prefix (`4100`/`4200`) — `prefix + scale_number` gives the
    numeric part of every filename this catalog publishes for that scale."""

    scale_numbers: tuple[int, ...]
    """Which of the 20 universal scale numbers (0-19) this catalog actually publishes."""

    shard_counts: dict[int, int] = field(default_factory=dict)
    """scale number -> number of per-healpix shard files, for a scale that's sharded. A scale
    number absent here is a single file, no shard suffix."""


# Both verified directly against data.astrometry.net's real directory listings.
_CATALOGS: dict[Catalog, _CatalogSpec] = {
    "tycho2": _CatalogSpec(prefix=4100, scale_numbers=tuple(range(7, 20))),
    "2mass": _CatalogSpec(
        prefix=4200,
        scale_numbers=tuple(range(0, 20)),
        shard_counts={0: 48, 1: 48, 2: 48, 3: 48, 4: 48, 5: 12, 6: 12, 7: 12},
    ),
}


class IndexFileStatus(TypedDict):
    """One catalog's known index coverage for one scale number, and its installed state."""

    catalog: Catalog
    indexNumber: int
    filenames: list[str]
    """One filename (a single file) or several (a sharded scale — see `_CatalogSpec`)."""

    minArcmin: float
    maxArcmin: float
    installed: bool
    """`True` only if every one of `filenames` is present — a partially-downloaded sharded
    scale (e.g. an interrupted earlier download) reports `False`, since `solve-field` needs
    the whole scale's coverage, not a subset of its healpixes."""

    installedFileCount: int
    sizeBytes: int | None
    """Sum of the installed files' sizes, or `None` if none are installed yet."""

    needed: bool | None
    """Whether this scale's range overlaps the requested field of view — `None` if
    `list_index_files` wasn't given a `rig` or `min_arcmin`/`max_arcmin` to check against."""


def _index_dir(directory: Path | None) -> Path:
    if directory is not None:
        return directory
    return Path(os.environ.get(INDEX_DIR_ENV, _DEFAULT_INDEX_DIR))


def _index_base_url() -> str:
    return os.environ.get(INDEX_BASE_URL_ENV, _DEFAULT_INDEX_BASE_URL)


def _catalog_spec(catalog: Catalog) -> _CatalogSpec:
    spec = _CATALOGS.get(catalog)
    if spec is None:
        raise ValueError(f"unknown catalog {catalog!r} (expected one of {get_args(Catalog)})")
    return spec


def _index_filenames(catalog: Catalog, scale_number: int) -> list[str]:
    spec = _catalog_spec(catalog)
    if scale_number not in spec.scale_numbers:
        raise ValueError(
            f"catalog {catalog!r} doesn't publish scale {scale_number!r} "
            f"(has {sorted(spec.scale_numbers)})"
        )
    index_number = spec.prefix + scale_number
    shard_count = spec.shard_counts.get(scale_number)
    if shard_count is None:
        return [f"index-{index_number:04d}.fits"]
    return [f"index-{index_number:04d}-{shard:02d}.fits" for shard in range(shard_count)]


def _index_url(catalog: Catalog, filename: str) -> str:
    spec = _catalog_spec(catalog)
    return f"{_index_base_url()}/{spec.prefix}/{filename}"


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


def index_numbers_for_field_of_view(
    min_arcmin: float,
    max_arcmin: float,
    *,
    catalog: Catalog = "tycho2",
    margin_scales: int = 0,
) -> list[int]:
    """Every scale number `catalog` actually publishes whose own coverage range overlaps
    `[min_arcmin, max_arcmin]` — a simple overlap test, not the more precise "quad size
    10%-100% of image size" astrometry.net's own docs suggest, but adequate for picking a
    reasonable set of index files to have installed for a given field of view.

    `margin_scales` extends the result by that many additional scale numbers `catalog`
    publishes immediately below the smallest match and above the largest, rather than just
    the exact overlapping bracket — a rig's *computed* field of view is only ever an
    estimate (binning, slightly-off `focalLengthMm`/`pixelSizeMicron`, and a rectangular
    sensor's diagonal vs. its width/height all shift the real value around), so
    `field_of_view_arcmin_for_rig`-derived calls pass a non-zero margin to install a little
    headroom on both sides rather than exactly one bracket that might just miss. Defaults to
    `0` (the exact bracket only) for direct callers who already know precisely what range
    they want.
    """
    spec = _catalog_spec(catalog)
    all_scales = sorted(spec.scale_numbers)
    matched = [
        scale_number
        for scale_number in all_scales
        for lo, hi in [_SCALE_RANGES_ARCMIN[scale_number]]
        if hi >= min_arcmin and lo <= max_arcmin
    ]
    if not matched or margin_scales <= 0:
        return matched
    min_index = all_scales.index(matched[0])
    max_index = all_scales.index(matched[-1])
    return all_scales[max(0, min_index - margin_scales) : max_index + margin_scales + 1]


def list_index_files(
    *,
    catalog: Catalog = "tycho2",
    directory: Path | None = None,
    rig: rig_store.Rig | None = None,
    min_arcmin: float | None = None,
    max_arcmin: float | None = None,
) -> list[IndexFileStatus]:
    """List every scale number `catalog` publishes and whether it's installed under
    `directory` (defaults to `INDEX_DIR_ENV`, falling back to `./astrometry_index`).

    Pass **at most one** of `rig` or `min_arcmin`+`max_arcmin` to also get a `needed` flag
    per entry (`None` throughout otherwise) — this doubles as a way to see which files would
    be needed for a field of view *without downloading anything*: pass the range (or a rig)
    and filter the result for `needed is True`, no disk writes or network access involved
    beyond the installed-status check already being made. `rig` computes the field of view
    from its own configured optics (`field_of_view_arcmin_for_rig`) and pads it by
    `DEFAULT_RIG_MARGIN_SCALES` extra scales on each side, since a rig's computed field of
    view is only ever an estimate; `min_arcmin`/`max_arcmin` given directly is used exactly
    as given, no padding, for a caller who already knows precisely what range they want
    (e.g. checking coverage for a setup that has no saved rig at all) — matching
    `index_numbers_for_field_of_view`'s own default. Raises `ValueError` if both are given,
    or if only one of `min_arcmin`/`max_arcmin` is given.
    """
    if rig is not None and (min_arcmin is not None or max_arcmin is not None):
        raise ValueError("pass rig or min_arcmin/max_arcmin, not both")
    if (min_arcmin is None) != (max_arcmin is None):
        raise ValueError("pass both min_arcmin and max_arcmin together, not just one")

    spec = _catalog_spec(catalog)
    resolved_dir = _index_dir(directory)
    needed: set[int] | None = None
    if rig is not None:
        try:
            fov_min_arcmin, fov_max_arcmin = field_of_view_arcmin_for_rig(rig)
        except ValueError:
            needed = None
        else:
            needed = set(
                index_numbers_for_field_of_view(
                    fov_min_arcmin,
                    fov_max_arcmin,
                    catalog=catalog,
                    margin_scales=DEFAULT_RIG_MARGIN_SCALES,
                )
            )
    elif min_arcmin is not None and max_arcmin is not None:
        needed = set(index_numbers_for_field_of_view(min_arcmin, max_arcmin, catalog=catalog))

    statuses: list[IndexFileStatus] = []
    for scale_number in sorted(spec.scale_numbers):
        scale_min_arcmin, scale_max_arcmin = _SCALE_RANGES_ARCMIN[scale_number]
        filenames = _index_filenames(catalog, scale_number)
        paths = [resolved_dir / name for name in filenames]
        installed_sizes = [p.stat().st_size for p in paths if p.is_file()]
        statuses.append(
            {
                "catalog": catalog,
                "indexNumber": scale_number,
                "filenames": filenames,
                "minArcmin": scale_min_arcmin,
                "maxArcmin": scale_max_arcmin,
                "installed": len(installed_sizes) == len(filenames),
                "installedFileCount": len(installed_sizes),
                "sizeBytes": sum(installed_sizes) if installed_sizes else None,
                "needed": (scale_number in needed) if needed is not None else None,
            }
        )
    return statuses


def _download_one_file(url: str, dest: Path) -> None:
    """Blocking: download one file, streamed straight to disk in `_DOWNLOAD_CHUNK_BYTES`-
    sized chunks (never buffered whole in memory — these run up to ~165 MB) to a `.part`
    temp name, renamed to its real filename only once the whole download succeeds — so a
    truncated/interrupted download is never mistaken for a valid, complete file the next
    time `list_index_files`/`solve-field` looks for it.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    logger.info("Downloading astrometry.net index file %s from %s", dest.name, url)
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
    logger.info(
        "Downloaded astrometry.net index file %s (%d bytes)", dest.name, dest.stat().st_size
    )


async def download_index_files(
    index_numbers: list[int], *, catalog: Catalog = "tycho2", directory: Path | None = None
) -> list[int]:
    """Download whichever files back `index_numbers` (scale numbers) from `catalog` aren't
    already installed under `directory` (same default as `list_index_files`) — for a sharded
    scale, every missing shard is downloaded. Returns the scale numbers that had at least one
    file actually downloaded this call — an already-fully-installed scale is left alone.

    Raises `ValueError` for any scale number `catalog` doesn't publish *before* downloading
    anything, so a typo in a longer list doesn't waste bandwidth on the valid ones before
    failing on the last.

    Sequential, not concurrent: these are large files (up to ~165 MB) on what's typically a
    modest home/site internet connection — downloading several at once would just contend
    for the same bandwidth with no real speedup, only a higher chance of a partial-progress
    failure. Each download runs via `asyncio.to_thread` (`_download_one_file`), so it never
    blocks the event loop other script runs/device messaging depend on.

    Serialized per destination path (`_download_locks`) against a *second* caller racing the
    same missing file — e.g. two script runs on similar rigs, or an MCP client that times out
    waiting for a ~165 MB download and retries the same tool call while the first is still in
    flight. Without this, both callers would pass the `is_file()` check and both open the
    same `.part` path with `"wb"` (which truncates on open), corrupting whichever download
    was already in progress — the same check-then-act race `script_store.py`'s own
    `_save_script_lock` exists to prevent for concurrent script uploads.
    """
    spec = _catalog_spec(catalog)
    unknown = [n for n in index_numbers if n not in spec.scale_numbers]
    if unknown:
        raise ValueError(
            f"catalog {catalog!r} doesn't publish scale(s) {unknown} "
            f"(has {sorted(spec.scale_numbers)})"
        )

    resolved_dir = _index_dir(directory)
    downloaded: list[int] = []
    for scale_number in index_numbers:
        touched = False
        for filename in _index_filenames(catalog, scale_number):
            dest = resolved_dir / filename
            lock = _download_locks.setdefault(dest, asyncio.Lock())
            async with lock:
                if dest.is_file():
                    continue
                url = _index_url(catalog, filename)
                await asyncio.to_thread(_download_one_file, url, dest)
                touched = True
        if touched:
            downloaded.append(scale_number)
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
