"""Tests for `plate_solver.solve` against a fake `solve-field` executable.

No real astrometry.net install/index files are available in CI, so these exercise the actual
subprocess-invocation/timeout/parsing logic against a small fake script standing in for
`solve-field` — its behavior (solve/fail/hang) and, for the argv-inspection test, where it
writes what it was called with, are both controlled via env vars the fake script reads back
out of its own inherited environment (subprocesses inherit the parent's env by default).
"""

import asyncio
import io
import json
import stat
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from indi_mcp import astrometry_index, db, frame_store, plate_solver

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


@pytest.fixture()
def frame_store_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point `frame_store`'s env-configured frames dir/db path at `tmp_path`, so
    `solve_uploaded_frame`'s own (unparameterized) `frame_store.save_frame`/`get_frame_path`/
    `update_frame_data` calls land somewhere real and isolated per test."""
    monkeypatch.setenv(frame_store.FRAMES_DIR_ENV, str(tmp_path / "frames"))
    monkeypatch.setenv(db.DB_PATH_ENV, str(tmp_path / "indi_mcp.sqlite3"))
    return tmp_path


def _real_fits_bytes() -> bytes:
    hdu = fits.PrimaryHDU(data=np.zeros((4, 4), dtype=np.uint16))
    buffer = io.BytesIO()
    hdu.writeto(buffer)
    return buffer.getvalue()


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


async def test_solve_passes_config_pointing_at_the_managed_index_dir_when_set(
    fake_solve_field: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args_file = tmp_path / "args.json"
    monkeypatch.setenv("FAKE_SOLVE_FIELD_ARGS_FILE", str(args_file))
    index_dir = tmp_path / "index"
    monkeypatch.setenv(astrometry_index.INDEX_DIR_ENV, str(index_dir))
    fits_path = tmp_path / "frame.fits"
    fits_path.write_bytes(b"not-really-fits")

    await plate_solver.solve(fits_path, timeout_seconds=5)

    args = json.loads(args_file.read_text())
    assert "--config" in args
    config_path = Path(args[args.index("--config") + 1])
    assert config_path == index_dir / ".indi-mcp-astrometry.cfg"
    config_content = await asyncio.to_thread(config_path.read_text)
    assert f"add_path {index_dir.resolve()}" in config_content


async def test_solve_omits_config_when_index_dir_env_not_set(
    fake_solve_field: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(astrometry_index.INDEX_DIR_ENV, raising=False)
    args_file = tmp_path / "args.json"
    monkeypatch.setenv("FAKE_SOLVE_FIELD_ARGS_FILE", str(args_file))
    fits_path = tmp_path / "frame.fits"
    fits_path.write_bytes(b"not-really-fits")

    await plate_solver.solve(fits_path, timeout_seconds=5)

    args = json.loads(args_file.read_text())
    assert "--config" not in args


async def test_solve_uploaded_frame_saves_solves_and_writes_wcs_headers(
    fake_solve_field: Path, frame_store_dirs: Path
) -> None:
    result = await plate_solver.solve_uploaded_frame(_real_fits_bytes(), timeout_seconds=5)

    assert result["raDegJ2000"] == 150.25
    assert result["decDegJ2000"] == 20.5
    metadata = frame_store.get_frame_metadata(result["frameId"])
    assert metadata["device"] == "uploaded"
    assert metadata["runId"] is None
    frame_path = frame_store.get_frame_path(result["frameId"])
    with fits.open(frame_path) as hdul:
        header = hdul[0].header
        assert header["CRVAL1"] == pytest.approx(150.25, abs=1e-6)
        assert "CDELT1" in header
        assert "CROTA2" in header
        assert header["RADECSYS"] == "FK5"


async def test_solve_uploaded_frame_raises_for_invalid_fits_data(
    fake_solve_field: Path, frame_store_dirs: Path
) -> None:
    with pytest.raises(ValueError, match="not a valid FITS file"):
        await plate_solver.solve_uploaded_frame(b"this is not a fits file", timeout_seconds=5)


async def test_solve_uploaded_frame_rejects_an_oversized_upload(
    fake_solve_field: Path, frame_store_dirs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(plate_solver.MAX_UPLOADED_FRAME_BYTES_ENV, "10")

    with pytest.raises(ValueError, match="exceeding the 10-byte limit"):
        await plate_solver.solve_uploaded_frame(_real_fits_bytes(), timeout_seconds=5)

    assert frame_store.list_frames(device="uploaded") == []


async def test_solve_uploaded_frame_keeps_the_frame_when_solve_fails(
    fake_solve_field: Path, frame_store_dirs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_SOLVE_FIELD_MODE", "fail")

    with pytest.raises(ValueError, match="did not solve"):
        await plate_solver.solve_uploaded_frame(_real_fits_bytes(), timeout_seconds=5)

    frames = frame_store.list_frames(device="uploaded")
    assert len(frames) == 1
    assert frames[0]["runId"] is None


async def test_solve_uploaded_frame_passes_hints_through_to_solve(
    fake_solve_field: Path, frame_store_dirs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args_file = frame_store_dirs / "args.json"
    monkeypatch.setenv("FAKE_SOLVE_FIELD_ARGS_FILE", str(args_file))

    await plate_solver.solve_uploaded_frame(
        _real_fits_bytes(),
        ra_hint_hours=10.0,
        dec_hint_deg=20.0,
        scale_low_arcsec=1.0,
        scale_high_arcsec=2.0,
        timeout_seconds=5,
    )

    args = json.loads(args_file.read_text())
    assert args[args.index("--ra") + 1] == str(10.0 * 15.0)
    assert args[args.index("--scale-low") + 1] == "1.0"
