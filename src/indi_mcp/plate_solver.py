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
own reasoning below) and parses the resulting `.wcs` file, itself a small valid FITS header.

This module only *parses* that raw WCS (`CRVAL`/`CRPIX`/`CTYPE`/CD-matrix — `solve-field`'s
own output shape) into `PlateSolveResult` — it deliberately doesn't reshape it into the
`CDELT`/`CROTA`/`SECPIX`/`RADECSYS` keywords this project's own FITS convention (and its
downstream readers, e.g. AstroKit) expect (INDIMCP-69); that CD-matrix-to-legacy-WCS
conversion is `fits_headers.wcs_fields_from_cd_matrix`, kept there rather than here since it's
about *shaping a FITS header*, not about talking to `solve-field`.

`write_wcs_headers` (best-effort, writes the converted WCS back onto an already-saved frame)
and `solve_uploaded_frame` (save + solve + write-back for a frame with no rig/run/mount behind
it at all, INDIMCP-76) are the two callers of the above actually need — `script_engine`'s
`plate_solve` step and `server.py`'s `plate_solve_uploaded_frame` tool respectively — sharing
this one implementation rather than each duplicating the save/solve/write-back sequence.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from astropy.io import fits

from indi_mcp import fits_headers, frame_store

logger = logging.getLogger(__name__)

__all__ = [
    "ASTROMETRY_BIN_ENV",
    "ASTROMETRY_TIMEOUT_SECONDS_ENV",
    "PlateSolveResult",
    "UploadedFrameSolveResult",
    "solve",
    "solve_uploaded_frame",
    "write_wcs_headers",
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


@dataclass
class PlateSolveResult:
    """The raw WCS solve-field produced: the solved field center (J2000, degrees) plus the
    CD-matrix form solve-field itself writes — `crpix1`/`crpix2` (the reference pixel),
    `ctype1`/`ctype2` (the projection, e.g. `RA---TAN`/`DEC--TAN`), and the 2x2 `cd1_1`/
    `cd1_2`/`cd2_1`/`cd2_2` matrix (pixel-to-sky transform, degrees/pixel).

    This is `solve-field`'s own output shape, unconverted — see
    `fits_headers.wcs_fields_from_cd_matrix` for turning this into the `CDELT`/`CROTA`/
    `SECPIX`/`RADECSYS` keywords this project's own FITS convention actually writes
    (INDIMCP-69).
    """

    raDegJ2000: float
    decDegJ2000: float
    crpix1: float
    crpix2: float
    ctype1: str
    ctype2: str
    cd1_1: float
    cd1_2: float
    cd2_1: float
    cd2_2: float


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
    """Read `solve-field`'s own `.wcs` output (already a minimal, valid FITS header) into a
    `PlateSolveResult`. `None` if any of the core WCS keywords `solve-field` is expected to
    always write on a successful solve (`CRVAL1/2`, `CRPIX1/2`, `CTYPE1/2`, the CD matrix)
    is missing or unparseable — a malformed `.wcs` file at that point would mean something
    is wrong with the `solve-field` install/version, not an ordinary "didn't solve" outcome,
    but there's nothing safe to build a result out of either way.
    """
    with fits.open(wcs_path) as hdul:
        header = hdul[0].header
        try:
            return PlateSolveResult(
                raDegJ2000=float(header["CRVAL1"]),
                decDegJ2000=float(header["CRVAL2"]),
                crpix1=float(header["CRPIX1"]),
                crpix2=float(header["CRPIX2"]),
                ctype1=str(header["CTYPE1"]),
                ctype2=str(header["CTYPE2"]),
                cd1_1=float(header["CD1_1"]),
                cd1_2=float(header["CD1_2"]),
                cd2_1=float(header["CD2_1"]),
                cd2_2=float(header["CD2_2"]),
            )
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning(
                "solve-field's .wcs at %s is missing an expected keyword: %s", wcs_path, exc
            )
            return None


async def write_wcs_headers(frame_id: str, frame_path: Path, result: PlateSolveResult) -> None:
    """Best-effort: convert `result`'s CD-matrix WCS into this project's own `CRVAL`/`CTYPE`/
    `CRPIX`/`CDELT`/`CROTA`/`SECPIX`/`RADECSYS`/`EQUINOX` convention
    (`fits_headers.wcs_fields_from_cd_matrix`, INDIMCP-69) and merge it into `frame_id`'s own
    FITS header in place, via `frame_store.update_frame_data` (keeps `size_bytes` in sync
    with the header rewrite).

    Not fatal if this fails — a solve that succeeded but couldn't be written back still
    counts as an overall success for the caller (`script_engine._execute_plate_solve`,
    `solve_uploaded_frame` below), it's only the persisted header that's missing. Catches
    `OSError` (a missing/unreadable frame file, a full disk on rewrite), `sqlite3.Error`
    (`update_frame_data`'s own `UPDATE`, e.g. a locked/corrupt db file — not an `OSError`
    subclass, so it needs its own arm here), and `frame_store.FrameNotFoundError` (the frame
    row vanishing between capture and this write, e.g. a concurrent `delete_frame`) — every
    realistic failure mode of this best-effort write, without masking an actual bug behind a
    bare `except Exception`. `wcs_fields_from_cd_matrix` itself never raises (a degenerate CD
    matrix returns `{}`, treated by `write_fits_headers` as nothing to do), so this doesn't
    need to guard against that too.
    """
    try:
        fields = fits_headers.wcs_fields_from_cd_matrix(
            ra_deg_j2000=result.raDegJ2000,
            dec_deg_j2000=result.decDegJ2000,
            crpix1=result.crpix1,
            crpix2=result.crpix2,
            ctype1=result.ctype1,
            ctype2=result.ctype2,
            cd1_1=result.cd1_1,
            cd1_2=result.cd1_2,
            cd2_1=result.cd2_1,
            cd2_2=result.cd2_2,
        )
        data = await asyncio.to_thread(frame_path.read_bytes)
        updated = fits_headers.write_fits_headers(data, fields)
        if updated is not None:
            await asyncio.to_thread(frame_store.update_frame_data, frame_id, updated)
    except (OSError, sqlite3.Error, frame_store.FrameNotFoundError) as exc:
        logger.warning("plate_solve: failed to write WCS headers onto frame %s: %s", frame_id, exc)


class UploadedFrameSolveResult(TypedDict):
    """`solve_uploaded_frame`'s return shape: the saved frame's id plus the solved position."""

    frameId: str
    raDegJ2000: float
    decDegJ2000: float


async def solve_uploaded_frame(
    data: bytes,
    *,
    ra_hint_hours: float | None = None,
    dec_hint_deg: float | None = None,
    scale_low_arcsec: float | None = None,
    scale_high_arcsec: float | None = None,
    timeout_seconds: float | None = None,
) -> UploadedFrameSolveResult:
    """Save `data` (a client-supplied FITS file, INDIMCP-76) as a new frame, plate-solve it,
    and best-effort write the solved WCS back onto it — the same solve+write-back sequence
    `script_engine`'s `plate_solve` step runs, but for a frame with no rig, run, or mount
    behind it at all (there's nothing to read a position/scale hint from automatically, and
    nothing to sync afterward — a caller who wants a hint passes one directly).

    Saved via `frame_store.save_frame` with `device="uploaded"` and no `run_id` (an ad hoc
    capture, per that module's own "`run_id=None` for a frame captured outside any script
    run" convention) — this makes the uploaded frame a first-class citizen of the frame
    store: it shows up in `list_frames`, is retrievable via `frame://{frameId}` once solved
    (WCS included), and is subject to the same explicit-confirm-then-delete lifecycle as any
    other frame. Kept even if the solve fails, so the caller can still retrieve/inspect
    what they uploaded.

    Raises `ValueError` if `data` isn't a file `astropy.io.fits` can open, or if `solve-field`
    doesn't solve it within `timeout_seconds` — a plain `ValueError` rather than plumbing this
    through the script-engine's own `ScriptExecutionError`/`ScriptFailed` vocabulary, since
    this never runs as a script (no rig, no run, nothing script-shaped about it at all); a
    `FastMCP` tool raising is the ordinary way to surface a tool-call failure.
    """
    try:
        with fits.open(io.BytesIO(data)):
            pass
    except OSError as exc:
        raise ValueError(f"not a valid FITS file: {exc}") from exc

    metadata = await asyncio.to_thread(
        frame_store.save_frame, data, device="uploaded", extension=".fits"
    )
    frame_id = metadata["frameId"]
    frame_path = await asyncio.to_thread(frame_store.get_frame_path, frame_id)

    result = await solve(
        frame_path,
        ra_hint_hours=ra_hint_hours,
        dec_hint_deg=dec_hint_deg,
        scale_low_arcsec=scale_low_arcsec,
        scale_high_arcsec=scale_high_arcsec,
        timeout_seconds=timeout_seconds,
    )
    if result is None:
        raise ValueError(
            f"solve-field did not solve the uploaded frame (saved as frameId {frame_id!r}; "
            "not deleted, retrievable via the frame:// resource without WCS headers)"
        )

    await write_wcs_headers(frame_id, frame_path, result)
    return {
        "frameId": frame_id,
        "raDegJ2000": result.raDegJ2000,
        "decDegJ2000": result.decDegJ2000,
    }
