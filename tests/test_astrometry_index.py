import asyncio
import io
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from indi_mcp import astrometry_index, rig_store


def _rig_with_optics(
    *, focal_length_mm: float | None = 750.0, pixel_size_micron: float | None = 3.76
) -> rig_store.Rig:
    return rig_store.Rig(
        id="test-rig",
        name="Test rig",
        components=[
            rig_store.Component(role="telescope", id="scope-1", focalLengthMm=focal_length_mm),
            rig_store.Component(
                role="camera",
                id="cam-1",
                device="CCD Simulator",
                pixelSizeMicron=pixel_size_micron,
                pixelsX=6248,
                pixelsY=4176,
            ),
        ],
    )


def test_field_of_view_arcmin_for_rig_computes_from_optics() -> None:
    rig = _rig_with_optics(focal_length_mm=750.0, pixel_size_micron=3.76)

    min_arcmin, max_arcmin = astrometry_index.field_of_view_arcmin_for_rig(rig)

    scale_arcsec_per_pixel = 206.265 * 3.76 / 750.0
    expected_width = 6248 * scale_arcsec_per_pixel / 60.0
    expected_height = 4176 * scale_arcsec_per_pixel / 60.0
    assert min_arcmin == pytest.approx(min(expected_width, expected_height))
    assert max_arcmin == pytest.approx(max(expected_width, expected_height))


def test_field_of_view_arcmin_for_rig_raises_without_telescope_focal_length() -> None:
    rig = rig_store.Rig(
        id="test-rig",
        name="Test rig",
        components=[
            rig_store.Component(
                role="camera",
                id="cam-1",
                device="CCD Simulator",
                pixelSizeMicron=3.76,
                pixelsX=6248,
                pixelsY=4176,
            )
        ],
    )

    with pytest.raises(ValueError, match="telescope"):
        astrometry_index.field_of_view_arcmin_for_rig(rig)


def test_field_of_view_arcmin_for_rig_raises_without_camera_pixel_geometry() -> None:
    rig = rig_store.Rig(
        id="test-rig",
        name="Test rig",
        components=[
            rig_store.Component(role="telescope", id="scope-1", focalLengthMm=750.0),
            rig_store.Component(role="camera", id="cam-1", device="CCD Simulator"),
        ],
    )

    with pytest.raises(ValueError, match="camera"):
        astrometry_index.field_of_view_arcmin_for_rig(rig)


@pytest.mark.parametrize(
    "min_arcmin,max_arcmin,expected",
    [
        (23.0, 29.0, [7]),  # fully inside index 7's own range, no boundary touch
        (20.0, 65.0, [7, 8, 9, 10]),
        (1005.0, 1395.0, [18]),  # fully inside index 18's own range
        (0.5, 1.0, []),
    ],
)
def test_index_numbers_for_field_of_view_selects_overlapping_ranges(
    min_arcmin: float, max_arcmin: float, expected: list[int]
) -> None:
    assert astrometry_index.index_numbers_for_field_of_view(min_arcmin, max_arcmin) == expected


def test_list_index_files_reports_installed_and_missing(tmp_path: Path) -> None:
    (tmp_path / "index-4107.fits").write_bytes(b"x" * 100)

    statuses = astrometry_index.list_index_files(directory=tmp_path)

    by_number = {s["indexNumber"]: s for s in statuses}
    assert set(by_number) == set(range(7, 20))
    assert by_number[7]["installed"] is True
    assert by_number[7]["sizeBytes"] == 100
    assert by_number[7]["filename"] == "index-4107.fits"
    assert by_number[8]["installed"] is False
    assert by_number[8]["sizeBytes"] is None
    assert by_number[7]["neededForRig"] is None


def test_list_index_files_flags_needed_for_rig(tmp_path: Path) -> None:
    rig = _rig_with_optics(focal_length_mm=750.0, pixel_size_micron=3.76)

    statuses = astrometry_index.list_index_files(directory=tmp_path, rig=rig)

    min_arcmin, max_arcmin = astrometry_index.field_of_view_arcmin_for_rig(rig)
    expected_needed = set(astrometry_index.index_numbers_for_field_of_view(min_arcmin, max_arcmin))
    by_number = {s["indexNumber"]: s for s in statuses}
    for index_number, status in by_number.items():
        assert status["neededForRig"] == (index_number in expected_needed)


def test_list_index_files_neededForRig_is_none_when_rig_has_no_optics(tmp_path: Path) -> None:
    rig = rig_store.Rig(
        id="test-rig", name="Test rig", components=[rig_store.Component(role="mount", id="m-1")]
    )

    statuses = astrometry_index.list_index_files(directory=tmp_path, rig=rig)

    assert all(status["neededForRig"] is None for status in statuses)


def test_ensure_astrometry_config_writes_add_path_and_autoindex(tmp_path: Path) -> None:
    config_path = astrometry_index.ensure_astrometry_config(tmp_path)

    content = config_path.read_text()
    assert f"add_path {tmp_path.resolve()}" in content
    assert "autoindex" in content


async def test_download_index_files_skips_already_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "index-4107.fits").write_bytes(b"already here")
    urlopen = MagicMock()
    monkeypatch.setattr(astrometry_index.urllib.request, "urlopen", urlopen)

    downloaded = await astrometry_index.download_index_files([7], directory=tmp_path)

    assert downloaded == []
    urlopen.assert_not_called()


async def test_download_index_files_downloads_missing_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_urlopen(url: str, timeout: float):
        assert url == "http://data.astrometry.net/4100/index-4109.fits"
        return io.BytesIO(b"fake-index-data")

    monkeypatch.setattr(astrometry_index.urllib.request, "urlopen", fake_urlopen)

    downloaded = await astrometry_index.download_index_files([9], directory=tmp_path)

    assert downloaded == [9]
    dest = tmp_path / "index-4109.fits"
    assert dest.read_bytes() == b"fake-index-data"
    assert not (tmp_path / "index-4109.fits.part").exists()


async def test_download_index_files_serializes_concurrent_requests_for_the_same_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two callers racing the same missing index (e.g. a client retrying a timed-out
    download while the first attempt is still in flight) must not both start downloading —
    the second should see the first's completed file and skip, not corrupt it."""
    call_count = 0

    def fake_urlopen(url: str, timeout: float):
        nonlocal call_count
        call_count += 1
        time.sleep(0.05)  # long enough that a second, unlocked caller would overlap it
        return io.BytesIO(b"fake-index-data")

    monkeypatch.setattr(astrometry_index.urllib.request, "urlopen", fake_urlopen)

    first, second = await asyncio.gather(
        astrometry_index.download_index_files([9], directory=tmp_path),
        astrometry_index.download_index_files([9], directory=tmp_path),
    )

    assert call_count == 1
    assert {tuple(first), tuple(second)} == {(9,), ()}
    assert (tmp_path / "index-4109.fits").read_bytes() == b"fake-index-data"


async def test_download_index_files_rejects_unknown_index_numbers(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown 4100-series index number"):
        await astrometry_index.download_index_files([3], directory=tmp_path)


async def test_download_index_files_cleans_up_part_file_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_urlopen(url: str, timeout: float):
        raise OSError("network unreachable")

    monkeypatch.setattr(astrometry_index.urllib.request, "urlopen", failing_urlopen)

    with pytest.raises(OSError, match="network unreachable"):
        await astrometry_index.download_index_files([9], directory=tmp_path)

    assert not (tmp_path / "index-4109.fits").exists()
    assert not (tmp_path / "index-4109.fits.part").exists()
