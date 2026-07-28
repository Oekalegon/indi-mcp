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
        (500.0, 501.0, [16]),  # fully inside index 16's own range
    ],
)
def test_index_numbers_for_field_of_view_selects_overlapping_ranges_for_tycho2(
    min_arcmin: float, max_arcmin: float, expected: list[int]
) -> None:
    assert (
        astrometry_index.index_numbers_for_field_of_view(min_arcmin, max_arcmin, catalog="tycho2")
        == expected
    )


def test_index_numbers_for_field_of_view_finds_nothing_below_tycho2s_range() -> None:
    # tycho2 only publishes scales 7-19 (22 arcmin+) -- a narrower field has no match at all
    assert astrometry_index.index_numbers_for_field_of_view(0.5, 1.0, catalog="tycho2") == []


def test_index_numbers_for_field_of_view_covers_narrow_fields_for_2mass() -> None:
    # 2mass publishes the full 0-19 range, unlike tycho2
    assert astrometry_index.index_numbers_for_field_of_view(2.1, 2.7, catalog="2mass") == [0]


def test_index_numbers_for_field_of_view_defaults_to_tycho2() -> None:
    assert astrometry_index.index_numbers_for_field_of_view(23.0, 29.0) == [7]


@pytest.mark.parametrize(
    "margin_scales,expected",
    [
        (0, [9]),
        (1, [8, 9, 10]),
        (2, [7, 8, 9, 10, 11]),
    ],
)
def test_index_numbers_for_field_of_view_margin_extends_on_both_sides(
    margin_scales: int, expected: list[int]
) -> None:
    result = astrometry_index.index_numbers_for_field_of_view(
        45.0, 50.0, catalog="tycho2", margin_scales=margin_scales
    )
    assert result == expected


def test_index_numbers_for_field_of_view_margin_clamps_at_catalogs_own_edges() -> None:
    # tycho2's lowest published scale is 7 -- a large margin must not go below that
    result = astrometry_index.index_numbers_for_field_of_view(
        23.0, 29.0, catalog="tycho2", margin_scales=5
    )
    assert result == [7, 8, 9, 10, 11, 12]


def test_list_index_files_reports_installed_and_missing_for_tycho2(tmp_path: Path) -> None:
    (tmp_path / "index-4107.fits").write_bytes(b"x" * 100)

    statuses = astrometry_index.list_index_files(catalog="tycho2", directory=tmp_path)

    by_number = {s["indexNumber"]: s for s in statuses}
    assert set(by_number) == set(range(7, 20))
    assert by_number[7]["catalog"] == "tycho2"
    assert by_number[7]["installed"] is True
    assert by_number[7]["sizeBytes"] == 100
    assert by_number[7]["filenames"] == ["index-4107.fits"]
    assert by_number[7]["installedFileCount"] == 1
    assert by_number[8]["installed"] is False
    assert by_number[8]["sizeBytes"] is None
    assert by_number[8]["installedFileCount"] == 0
    assert by_number[7]["neededForRig"] is None


def test_list_index_files_reports_sharded_2mass_scales(tmp_path: Path) -> None:
    # scale 0 has 48 shards; install just one of them
    (tmp_path / "index-4200-00.fits").write_bytes(b"x" * 50)

    statuses = astrometry_index.list_index_files(catalog="2mass", directory=tmp_path)

    by_number = {s["indexNumber"]: s for s in statuses}
    assert set(by_number) == set(range(0, 20))
    assert len(by_number[0]["filenames"]) == 48
    assert by_number[0]["installedFileCount"] == 1
    assert by_number[0]["installed"] is False  # only 1 of 48 shards present
    assert by_number[0]["sizeBytes"] == 50
    # a single-file scale (8-19 aren't sharded for 2mass)
    assert len(by_number[8]["filenames"]) == 1
    assert by_number[8]["filenames"] == ["index-4208.fits"]


def test_list_index_files_flags_needed_for_rig_with_margin(tmp_path: Path) -> None:
    rig = _rig_with_optics(focal_length_mm=750.0, pixel_size_micron=3.76)

    statuses = astrometry_index.list_index_files(catalog="tycho2", directory=tmp_path, rig=rig)

    min_arcmin, max_arcmin = astrometry_index.field_of_view_arcmin_for_rig(rig)
    expected_needed = set(
        astrometry_index.index_numbers_for_field_of_view(
            min_arcmin,
            max_arcmin,
            catalog="tycho2",
            margin_scales=astrometry_index.DEFAULT_RIG_MARGIN_SCALES,
        )
    )
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
        assert url == "https://data.astrometry.net/4100/index-4109.fits"
        return io.BytesIO(b"fake-index-data")

    monkeypatch.setattr(astrometry_index.urllib.request, "urlopen", fake_urlopen)

    downloaded = await astrometry_index.download_index_files([9], directory=tmp_path)

    assert downloaded == [9]
    dest = tmp_path / "index-4109.fits"
    assert dest.read_bytes() == b"fake-index-data"
    assert not (tmp_path / "index-4109.fits.part").exists()


async def test_download_index_files_downloads_every_shard_of_a_sharded_2mass_scale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    urls_requested: list[str] = []

    def fake_urlopen(url: str, timeout: float):
        urls_requested.append(url)
        return io.BytesIO(b"fake-shard-data")

    monkeypatch.setattr(astrometry_index.urllib.request, "urlopen", fake_urlopen)

    # scale 6 has 12 shards for 2mass
    downloaded = await astrometry_index.download_index_files(
        [6], catalog="2mass", directory=tmp_path
    )

    assert downloaded == [6]
    assert len(urls_requested) == 12
    for shard in range(12):
        assert (tmp_path / f"index-4206-{shard:02d}.fits").read_bytes() == b"fake-shard-data"


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


async def test_download_index_files_rejects_unpublished_scale_numbers(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="doesn't publish scale"):
        await astrometry_index.download_index_files([3], catalog="tycho2", directory=tmp_path)


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
