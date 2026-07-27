"""Tests for `plate_solver.solve` against a fake `solve-field` executable.

No real astrometry.net install/index files are available in CI, so these exercise the actual
subprocess-invocation/timeout/parsing logic against a small fake script standing in for
`solve-field` — its behavior (solve/fail/hang) and, for the argv-inspection test, where it
writes what it was called with, are both controlled via env vars the fake script reads back
out of its own inherited environment (subprocesses inherit the parent's env by default).
"""

import asyncio
import json
import stat
from pathlib import Path

import pytest

from indi_mcp import plate_solver

_FAKE_SOLVE_FIELD = """#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path

args = sys.argv[1:]
mode = os.environ.get("FAKE_SOLVE_FIELD_MODE", "solve")
args_file = os.environ.get("FAKE_SOLVE_FIELD_ARGS_FILE")
if args_file:
    Path(args_file).write_text(json.dumps(args))

if mode == "fail":
    sys.exit(1)
if mode == "timeout":
    time.sleep(5)
    sys.exit(0)

from astropy.io import fits

work_dir = args[args.index("--dir") + 1]
fits_path = Path(args[-1])
wcs_path = Path(work_dir) / f"{fits_path.stem}.wcs"
header = fits.Header()
header["CRVAL1"] = 150.25
header["CRVAL2"] = 20.5
header["CRPIX1"] = 512.0
header["CRPIX2"] = 512.0
header["CD1_1"] = -0.0002
header["CD1_2"] = 0.0
header["CD2_1"] = 0.0
header["CD2_2"] = 0.0002
header["CTYPE1"] = "RA---TAN"
header["CTYPE2"] = "DEC--TAN"
fits.PrimaryHDU(header=header).writeto(wcs_path, overwrite=True)
sys.exit(0)
"""


@pytest.fixture()
def fake_solve_field(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    script = tmp_path / "fake-solve-field"
    script.write_text(_FAKE_SOLVE_FIELD)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv(plate_solver.ASTROMETRY_BIN_ENV, str(script))
    return script


async def test_solve_parses_the_wcs_file_on_success(fake_solve_field: Path, tmp_path: Path) -> None:
    fits_path = tmp_path / "frame.fits"
    fits_path.write_bytes(b"not-really-fits")

    result = await plate_solver.solve(fits_path, timeout_seconds=5)

    assert result is not None
    assert result.raDegJ2000 == 150.25
    assert result.decDegJ2000 == 20.5
    assert result.crpix1 == 512.0
    assert result.crpix2 == 512.0
    assert result.ctype1 == "RA---TAN"
    assert result.ctype2 == "DEC--TAN"
    assert result.cd1_1 == -0.0002
    assert result.cd1_2 == 0.0
    assert result.cd2_1 == 0.0
    assert result.cd2_2 == 0.0002


async def test_solve_returns_none_on_a_nonzero_exit(
    fake_solve_field: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_SOLVE_FIELD_MODE", "fail")
    fits_path = tmp_path / "frame.fits"
    fits_path.write_bytes(b"not-really-fits")

    result = await plate_solver.solve(fits_path, timeout_seconds=5)

    assert result is None


async def test_solve_returns_none_and_kills_the_process_on_timeout(
    fake_solve_field: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_SOLVE_FIELD_MODE", "timeout")
    fits_path = tmp_path / "frame.fits"
    fits_path.write_bytes(b"not-really-fits")

    result = await plate_solver.solve(fits_path, timeout_seconds=0.2)

    assert result is None


async def test_solve_raises_cancelled_error_and_kills_the_process_when_cancelled(
    fake_solve_field: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_SOLVE_FIELD_MODE", "timeout")  # sleeps 5s
    fits_path = tmp_path / "frame.fits"
    fits_path.write_bytes(b"not-really-fits")
    cancel_event = asyncio.Event()

    async def cancel_soon() -> None:
        await asyncio.sleep(0.1)
        cancel_event.set()

    start = asyncio.get_event_loop().time()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.gather(
            plate_solver.solve(fits_path, timeout_seconds=5, cancel_event=cancel_event),
            cancel_soon(),
        )
    elapsed = asyncio.get_event_loop().time() - start

    assert elapsed < 4  # well under the fake script's 5s sleep and the 5s timeout


async def test_solve_passes_position_and_scale_hints_as_cli_args(
    fake_solve_field: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args_file = tmp_path / "args.json"
    monkeypatch.setenv("FAKE_SOLVE_FIELD_ARGS_FILE", str(args_file))
    fits_path = tmp_path / "frame.fits"
    fits_path.write_bytes(b"not-really-fits")

    await plate_solver.solve(
        fits_path,
        ra_hint_hours=10.0,
        dec_hint_deg=20.0,
        radius_deg=3.0,
        scale_low_arcsec=1.0,
        scale_high_arcsec=2.0,
        timeout_seconds=5,
    )

    args = json.loads(args_file.read_text())
    assert "--ra" in args
    assert args[args.index("--ra") + 1] == str(10.0 * 15.0)
    assert args[args.index("--dec") + 1] == "20.0"
    assert args[args.index("--radius") + 1] == "3.0"
    assert args[args.index("--scale-low") + 1] == "1.0"
    assert args[args.index("--scale-high") + 1] == "2.0"


async def test_solve_omits_hint_args_when_not_given(
    fake_solve_field: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args_file = tmp_path / "args.json"
    monkeypatch.setenv("FAKE_SOLVE_FIELD_ARGS_FILE", str(args_file))
    fits_path = tmp_path / "frame.fits"
    fits_path.write_bytes(b"not-really-fits")

    await plate_solver.solve(fits_path, timeout_seconds=5)

    args = json.loads(args_file.read_text())
    assert "--ra" not in args
    assert "--scale-low" not in args
