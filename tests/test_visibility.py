import math
from datetime import UTC, datetime, timedelta

import pytest

from indi_mcp import visibility
from indi_mcp.observatory_store import HorizonPoint, Observatory

_OBSERVATORY = Observatory(
    id="test-observatory",
    name="Test Observatory",
    latitudeDeg=52.3676,
    longitudeDeg=4.9041,
    elevationMeters=4,
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
    """A target culminating at ~37.6 degrees (dec=0 seen from ~52.37 degrees latitude) never
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


# --- _horizon_altitude_at (INDIMCP-135) ---------------------------------------------------


def test_horizon_altitude_at_returns_exact_value_at_a_defined_point() -> None:
    profile = [
        HorizonPoint(azimuthDeg=0, altitudeDeg=5),
        HorizonPoint(azimuthDeg=90, altitudeDeg=15),
        HorizonPoint(azimuthDeg=200, altitudeDeg=10),
    ]

    assert visibility._horizon_altitude_at(profile, 90) == pytest.approx(15)


def test_horizon_altitude_at_interpolates_linearly_between_two_points() -> None:
    profile = [
        HorizonPoint(azimuthDeg=0, altitudeDeg=0),
        HorizonPoint(azimuthDeg=100, altitudeDeg=10),
    ]

    assert visibility._horizon_altitude_at(profile, 50) == pytest.approx(5)


def test_horizon_altitude_at_interpolates_across_the_0_360_wrap() -> None:
    profile = [
        HorizonPoint(azimuthDeg=10, altitudeDeg=20),
        HorizonPoint(azimuthDeg=350, altitudeDeg=0),
    ]

    assert visibility._horizon_altitude_at(profile, 0) == pytest.approx(10)
    assert visibility._horizon_altitude_at(profile, 355) == pytest.approx(5)
    assert visibility._horizon_altitude_at(profile, 5) == pytest.approx(15)


def test_horizon_altitude_at_single_point_profile_is_constant_everywhere() -> None:
    profile = [HorizonPoint(azimuthDeg=123, altitudeDeg=7)]

    assert visibility._horizon_altitude_at(profile, 0) == pytest.approx(7)
    assert visibility._horizon_altitude_at(profile, 359.9) == pytest.approx(7)


# --- compute_visibility with observatory.horizonProfile (INDIMCP-135) --------------------


def test_compute_visibility_flat_horizon_profile_matches_equivalent_min_altitude_deg() -> None:
    """A flat (uniform) horizon profile should behave exactly like passing the same value as
    `min_altitude_deg` directly — the simplest correctness check for the interpolation path,
    exercised through real astropy azimuth values rather than synthetic ones."""
    flat_profile = [
        HorizonPoint(azimuthDeg=0, altitudeDeg=60),
        HorizonPoint(azimuthDeg=180, altitudeDeg=60),
    ]
    observatory_with_profile = _OBSERVATORY.model_copy(update={"horizonProfile": flat_profile})

    intervals_from_profile = visibility.compute_visibility(
        ra_deg=45.0, dec_deg=75.0, observatory=observatory_with_profile, start=_START, end=_ONE_DAY
    )
    intervals_from_min_altitude = visibility.compute_visibility(
        ra_deg=45.0,
        dec_deg=75.0,
        observatory=_OBSERVATORY,
        start=_START,
        end=_ONE_DAY,
        min_altitude_deg=60.0,
    )

    assert intervals_from_profile == intervals_from_min_altitude
    # Sanity: this target is circumpolar (see the plain-horizon test above), so a 60 degree
    # threshold should carve out a genuine partial-visibility window, not all-or-nothing.
    assert len(intervals_from_profile) > 0
    total_visible = sum((i["end"] - i["start"]).total_seconds() for i in intervals_from_profile)
    assert 0 < total_visible < (_ONE_DAY - _START).total_seconds()


def test_compute_visibility_horizon_profile_combines_with_min_altitude_deg_via_max() -> None:
    """`min_altitude_deg` and the horizon profile should combine as `max(...)` at each sample:
    a profile lower than `min_altitude_deg` is dominated by it (same result as profile-less),
    and a profile higher than `min_altitude_deg` dominates instead."""
    low_profile = [
        HorizonPoint(azimuthDeg=0, altitudeDeg=5),
        HorizonPoint(azimuthDeg=180, altitudeDeg=5),
    ]
    observatory_low_profile = _OBSERVATORY.model_copy(update={"horizonProfile": low_profile})

    dominated_by_min_altitude = visibility.compute_visibility(
        ra_deg=90.0,
        dec_deg=0.0,
        observatory=observatory_low_profile,
        start=_START,
        end=_ONE_DAY,
        min_altitude_deg=40.0,
    )
    plain_min_altitude_only = visibility.compute_visibility(
        ra_deg=90.0,
        dec_deg=0.0,
        observatory=_OBSERVATORY,
        start=_START,
        end=_ONE_DAY,
        min_altitude_deg=40.0,
    )
    assert dominated_by_min_altitude == plain_min_altitude_only == []

    high_profile = [
        HorizonPoint(azimuthDeg=0, altitudeDeg=60),
        HorizonPoint(azimuthDeg=180, altitudeDeg=60),
    ]
    observatory_high_profile = _OBSERVATORY.model_copy(update={"horizonProfile": high_profile})

    dominated_by_profile = visibility.compute_visibility(
        ra_deg=45.0,
        dec_deg=75.0,
        observatory=observatory_high_profile,
        start=_START,
        end=_ONE_DAY,
        min_altitude_deg=0.0,
    )
    plain_profile_equivalent = visibility.compute_visibility(
        ra_deg=45.0,
        dec_deg=75.0,
        observatory=_OBSERVATORY,
        start=_START,
        end=_ONE_DAY,
        min_altitude_deg=60.0,
    )
    assert dominated_by_profile == plain_profile_equivalent


def test_compute_visibility_directional_horizon_profile_blocks_only_part_of_the_azimuth_range() -> (
    None
):
    """A profile that only obstructs the northern half of the sky (azimuth 0-179) should
    exclude roughly the half of the day this circumpolar target spends crossing that half —
    not all of it (like a uniform high profile) and not none of it (like no profile)."""
    directional_profile = [
        HorizonPoint(azimuthDeg=0, altitudeDeg=80),
        HorizonPoint(azimuthDeg=179, altitudeDeg=80),
        HorizonPoint(azimuthDeg=180, altitudeDeg=0),
        HorizonPoint(azimuthDeg=359, altitudeDeg=0),
    ]
    observatory_with_profile = _OBSERVATORY.model_copy(
        update={"horizonProfile": directional_profile}
    )

    intervals = visibility.compute_visibility(
        ra_deg=45.0, dec_deg=75.0, observatory=observatory_with_profile, start=_START, end=_ONE_DAY
    )

    total_visible = sum((i["end"] - i["start"]).total_seconds() for i in intervals)
    span_seconds = (_ONE_DAY - _START).total_seconds()
    assert 0.25 * span_seconds < total_visible < 0.75 * span_seconds
