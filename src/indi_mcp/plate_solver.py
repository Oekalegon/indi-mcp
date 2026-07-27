"""Plate-solving a FITS frame via astrometry.net's local `solve-field` CLI (INDIMCP-27/45).

Local `solve-field` + index files, not the `nova.astrometry.net` web API — see
`docs/PlateSolve.md` for why (this project's deployment target, a headless Pi running
unattended imaging sequences, can't depend on outbound internet or a third-party service's
availability/rate limits for something a running sequence blocks on). Index-file
installation/management is a deployment concern (and a separate ticket, tracking which are
present and downloading missing ones) — this module only assumes `solve-field` and *some*
index files are already installed.

`solve()` is the only entry point script_engine's `plate_solve` step handler needs; it shells
out to `solve-field` with `asyncio.create_subprocess_exec` (not `asyncio.to_thread` +
`subprocess.run`, unlike this codebase's other blocking-work convention — see the module's
own reasoning below) and parses the resulting `.wcs` file, itself a small valid FITS header,
directly — no hand-rolled WCS math.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from astropy.io import fits

logger = logging.getLogger(__name__)

__all__ = [
    "ASTROMETRY_BIN_ENV",
    "ASTROMETRY_TIMEOUT_SECONDS_ENV",
    "PlateSolveResult",
    "solve",
]

ASTROMETRY_BIN_ENV = "INDI_MCP_ASTROMETRY_BIN"
_DEFAULT_ASTROMETRY_BIN = "solve-field"

ASTROMETRY_TIMEOUT_SECONDS_ENV = "INDI_MCP_ASTROMETRY_TIMEOUT_SECONDS"
_DEFAULT_TIMEOUT_SECONDS = 60.0

_DEFAULT_SEARCH_RADIUS_DEG = 5.0
"""How far from the position hint `solve-field` searches, in degrees.

Covers ordinary mount-model/polar-alignment error around the mount's own reported position —
not currently configurable per call (`script_engine._execute_plate_solve` has no schema field
for it either); worth revisiting once real-world use shows this too wide or too narrow.
"""

# Keywords copied verbatim from solve-field's own `.wcs` output onto the captured frame's FITS
# header (INDIMCP-69) — the CD-matrix form solve-field itself produces, not a CDELT/CROTA
# conversion: it's already a complete, standard WCS with no astrometric math of our own
# needed (and thus no chance of introducing an error solve-field itself didn't have).
_WCS_KEYWORDS = (
    "CRVAL1",
    "CRVAL2",
    "CRPIX1",
    "CRPIX2",
    "CD1_1",
    "CD1_2",
    "CD2_1",
    "CD2_2",
    "CTYPE1",
    "CTYPE2",
    "CUNIT1",
    "CUNIT2",
    "EQUINOX",
    "RADESYS",
)


@dataclass
class PlateSolveResult:
    """The outcome of a successful solve: the solved field center (J2000, degrees) and the
    full set of WCS keyword/comment pairs, ready for `fits_headers.write_fits_headers`."""

    raDegJ2000: float
    decDegJ2000: float
    wcsFields: dict[str, tuple[float | int | str, str]]


def _solve_field_bin() -> str:
    return os.environ.get(ASTROMETRY_BIN_ENV, _DEFAULT_ASTROMETRY_BIN)


def _default_timeout_seconds() -> float:
    return float(os.environ.get(ASTROMETRY_TIMEOUT_SECONDS_ENV, _DEFAULT_TIMEOUT_SECONDS))


async def solve(
    fits_path: Path,
    *,
    ra_hint_hours: float | None = None,
    dec_hint_deg: float | None = None,
    scale_low_arcsec: float | None = None,
    scale_high_arcsec: float | None = None,
    radius_deg: float = _DEFAULT_SEARCH_RADIUS_DEG,
    timeout_seconds: float | None = None,
    cancel_event: asyncio.Event | None = None,
) -> PlateSolveResult | None:
    """Run `solve-field` on `fits_path`, returning the solved WCS, or `None` if it didn't
    solve (or timed out).

    An unsolved field is an ordinary outcome — bad focus, clouds, too few stars, a wrong or
    missing scale/position hint — not an exception; the caller (`script_engine`) decides what
    that means (currently: fail the step, since `plate_solve` exists specifically to solve).

    `ra_hint_hours`/`dec_hint_deg` narrow the search to `radius_deg` around that position
    (converted to `solve-field`'s expected degrees) when both are given; `scale_low_arcsec`/
    `scale_high_arcsec` narrow it to that arcsec/pixel band when both are given. Either or
    both may be omitted (an unhinted solve is slower but still valid) — see
    `docs/PlateSolve.md` for why these hints matter enough to be worth computing at all: a
    blind solve over the full index range can take tens of seconds to minutes on a Pi, while
    a properly hinted one typically resolves in a few seconds.

    `cancel_event`, if given, is raced against the solve — the same event a caller (the
    `plate_solve` step handler) already polls between steps elsewhere. Unlike every other
    long-running wait in the execution engine (`_wait_for_property_state`, `_execute_wait_for`),
    which can afford to poll `cancel_event` in a loop with a short sleep between checks, a
    single `await proc.communicate()` can't be interrupted or polled mid-flight — so instead
    of polling, this races `proc.communicate()` against `cancel_event.wait()` with
    `asyncio.wait(..., return_when=FIRST_COMPLETED)`: whichever finishes first decides the
    outcome, and the process is killed either way if it's still running. Raises
    `asyncio.CancelledError` if `cancel_event` fires first — the caller (`script_engine`) is
    expected to catch this and translate it into its own `ScriptCancelled`, matching this
    module staying decoupled from `script_engine`'s exception vocabulary.

    Uses `asyncio.create_subprocess_exec`, not this codebase's usual `asyncio.to_thread` +
    blocking-call convention (`frame_store`, `fits_headers`): a solve can legitimately run
    the full timeout, and unlike a brief disk write, a *hung* process needs to be killable
    from here (`proc.kill()`) the moment a script run is cancelled or times out, not left
    running in a thread this module has no handle to stop.

    All of `solve-field`'s own working files (`.wcs`, `.solved`, `.axy`, ...) are written to a
    fresh temporary directory (`--dir`), never next to `fits_path` itself — that's the frame
    store's own frames directory, which shouldn't accumulate solver byproducts. `--new-fits
    none` skips writing a modified copy of the input FITS entirely, since the WCS this
    function returns is applied to the original frame by the caller instead
    (`fits_headers.write_fits_headers`), not by asking `solve-field` to do it.
    """
    timeout = timeout_seconds if timeout_seconds is not None else _default_timeout_seconds()
    with tempfile.TemporaryDirectory(prefix="indi-mcp-platesolve-") as work_dir:
        args = [
            _solve_field_bin(),
            "--overwrite",
            "--no-plots",
            "--new-fits",
            "none",
            "--dir",
            work_dir,
        ]
        if ra_hint_hours is not None and dec_hint_deg is not None:
            args += [
                "--ra",
                str(ra_hint_hours * 15.0),
                "--dec",
                str(dec_hint_deg),
                "--radius",
                str(radius_deg),
            ]
        if scale_low_arcsec is not None and scale_high_arcsec is not None:
            args += [
                "--scale-units",
                "arcsecperpix",
                "--scale-low",
                str(scale_low_arcsec),
                "--scale-high",
                str(scale_high_arcsec),
            ]
        args.append(str(fits_path))

        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        communicate_task = asyncio.ensure_future(proc.communicate())
        cancel_task = asyncio.ensure_future(cancel_event.wait()) if cancel_event else None
        waiters = [communicate_task, *([cancel_task] if cancel_task else [])]
        done, _pending = await asyncio.wait(
            waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )

        if cancel_task is not None and cancel_task in done:
            proc.kill()
            await proc.wait()
            communicate_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await communicate_task
            logger.info("solve-field on %s cancelled", fits_path)
            raise asyncio.CancelledError("solve-field cancelled")

        if cancel_task is not None:
            cancel_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cancel_task

        if communicate_task not in done:
            proc.kill()
            await proc.wait()
            communicate_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await communicate_task
            logger.info("solve-field timed out after %ss on %s", timeout, fits_path)
            return None

        _, stderr = communicate_task.result()

        if proc.returncode != 0:
            logger.info(
                "solve-field exited %s on %s (no solve): %s",
                proc.returncode,
                fits_path,
                stderr.decode(errors="replace").strip(),
            )
            return None

        wcs_path = Path(work_dir) / f"{fits_path.stem}.wcs"
        if not wcs_path.is_file():
            logger.info("solve-field reported success but produced no %s", wcs_path)
            return None
        return await asyncio.to_thread(_parse_wcs, wcs_path)


def _parse_wcs(wcs_path: Path) -> PlateSolveResult | None:
    """Read `solve-field`'s own `.wcs` output (already a minimal, valid FITS header) and
    extract `_WCS_KEYWORDS`, verbatim, into `write_fits_headers`' generic field shape."""
    with fits.open(wcs_path) as hdul:
        header = hdul[0].header
        try:
            ra_deg, dec_deg = float(header["CRVAL1"]), float(header["CRVAL2"])
        except (KeyError, ValueError, TypeError):
            logger.warning("solve-field's .wcs at %s has no usable CRVAL1/CRVAL2", wcs_path)
            return None
        fields: dict[str, tuple[float | int | str, str]] = {}
        for keyword in _WCS_KEYWORDS:
            if keyword in header:
                comment = header.comments[keyword] or f"{keyword} (astrometry.net solve)"
                fields[keyword] = (header[keyword], comment)
    return PlateSolveResult(raDegJ2000=ra_deg, decDegJ2000=dec_deg, wcsFields=fields)
