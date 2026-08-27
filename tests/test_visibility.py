import math
from datetime import UTC, datetime, timedelta

import pytest

from indi_mcp import visibility
from indi_mcp.observatory_store import Observatory

_OBSERVATORY = Observatory(
    id="test-observatory",
    name="Test Observatory",
    latitudeDeg=60.369722,
    longitudeDeg=11.363611,
    elevationMeters=350,
)

_START = datetime(2026, 1, 15, 0, 0, 0, tzinfo=UTC)
_ONE_DAY = _START + timedelta(hours=24)
_TWO_DAYS = _START + timedelta(hours=48)


def test_compute_visibility_raises_when_end_not_after_start() -> None:
    with pytest.raises(ValueError, match="must be after"):
        visibility.compute_visibility(
            ra_deg=10.0, dec_deg=0.0, observatory=_OBSERVATORY, start=_START, end=_START
        )


def test_compute_visibility_raises_when_step_minutes_not_positive() -> None:
    with pytest.raises(ValueError, match="step_minutes must be positive"):
        visibility.compute_visibility(
            ra_deg=10.0,
            dec_deg=0.0,
            observatory=_OBSERVATORY,
            start=_START,
            end=_ONE_DAY,
            step_minutes=0,
        )


def test_compute_visibility_empty_for_a_target_that_never_rises() -> None:
    """A target with `dec < -(90 - |lat|)` never crosses the horizon at this latitude,
    regardless of RA or time — a deterministic "never visible" case."""
    intervals = visibility.compute_visibility(
        ra_deg=180.0,
        dec_deg=-80.0,
        observatory=_OBSERVATORY,
        start=_START,
        end=_ONE_DAY,
    )

    assert intervals == []


def test_compute_visibility_spans_the_whole_window_for_a_circumpolar_target() -> None:
    """A target with `dec > (90 - |lat|)` never sets at this latitude, so the whole
    requested window should come back as a single above-horizon interval."""
    intervals = visibility.compute_visibility(
        ra_deg=45.0,
        dec_deg=75.0,
        observatory=_OBSERVATORY,
        start=_START,
        end=_ONE_DAY,
    )

    assert len(intervals) == 1
    assert intervals[0]["start"] == _START
    assert intervals[0]["end"] == _ONE_DAY


def test_compute_visibility_is_empty_when_min_altitude_exceeds_the_targets_peak() -> None:
    """A target culminating at ~29.6 degrees (dec=0 seen from ~60.37 degrees latitude) never
    reaches a 40 degree minimum altitude."""
    intervals = visibility.compute_visibility(
        ra_deg=90.0,
        dec_deg=0.0,
        observatory=_OBSERVATORY,
        start=_START,
        end=_ONE_DAY,
        min_altitude_deg=40.0,
    )

    assert intervals == []


def test_compute_visibility_reports_partial_daily_visibility() -> None:
    """A non-circumpolar target rises and sets roughly once a day, so over a two-day window
    it should be above the horizon for less than the whole span but more than none of it,
    across more than one interval."""
    intervals = visibility.compute_visibility(
        ra_deg=90.0,
        dec_deg=0.0,
        observatory=_OBSERVATORY,
        start=_START,
        end=_TWO_DAYS,
        step_minutes=10,
    )

    assert len(intervals) >= 2
    total_visible = sum(
        (interval["end"] - interval["start"]).total_seconds() for interval in intervals
    )
    assert 0 < total_visible < (_TWO_DAYS - _START).total_seconds()


def test_compute_visibility_interval_boundaries_are_sample_times() -> None:
    """Interval boundaries land exactly on the fixed sampling grid (`step_minutes` apart,
    starting at `start`) — the documented sampling-precision contract, not interpolated."""
    step_minutes = 10.0
    intervals = visibility.compute_visibility(
        ra_deg=45.0,
        dec_deg=75.0,
        observatory=_OBSERVATORY,
        start=_START,
        end=_ONE_DAY,
        step_minutes=step_minutes,
    )

    for interval in intervals:
        for boundary in (interval["start"], interval["end"]):
            offset_minutes = (boundary - _START).total_seconds() / 60
            assert offset_minutes % step_minutes == pytest.approx(0, abs=1e-6)


def test_compute_visibility_narrows_the_step_for_a_non_exact_multiple_span() -> None:
    """When `[start, end]` isn't an exact multiple of `step_minutes`, actual sample spacing is
    narrowed (never widened) so the span still divides into whole steps and both endpoints are
    included exactly — the documented "at most step_minutes" contract, not a fixed grid."""
    step_minutes = 10.0
    end = _START + timedelta(hours=24, minutes=5)
    intervals = visibility.compute_visibility(
        ra_deg=45.0,
        dec_deg=75.0,
        observatory=_OBSERVATORY,
        start=_START,
        end=end,
        step_minutes=step_minutes,
    )

    assert len(intervals) == 1
    assert intervals[0]["start"] == _START
    assert intervals[0]["end"] == end

    span_minutes = (end - _START).total_seconds() / 60
    sample_count = math.ceil(span_minutes / step_minutes) + 1
    actual_step_minutes = span_minutes / (sample_count - 1)
    assert actual_step_minutes < step_minutes
