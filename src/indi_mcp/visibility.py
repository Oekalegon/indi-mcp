"""Object-above-horizon check for a timespan (INDIMCP-29).

Given a target's coordinates, an `Observatory` location, and a timespan, `compute_visibility`
answers "when, if at all, is this target above the horizon (or some higher minimum altitude)
during this window?" — a building block for the upcoming scripting/scheduling layer
(INDIMCP-6/7/8/32) to gate or schedule imaging around target visibility, e.g. "only start this
sequence once M31 is above 20 degrees altitude", or reject/warn on a script targeting an object
that never clears the horizon during the requested window.

Coordinates are ICRS (J2000), matching the convention this codebase already writes to FITS
headers (`fits_headers.TargetPosition`) — not INDI's EOD convention `fits_headers.
compute_celestial_context` takes, since this module has no mount in the loop to report an
epoch-of-date position from.

This is a sampled, not analytic, check: it evaluates altitude at a fixed step across the
timespan and reports the above-threshold intervals between samples, so a target's actual
rise/set moment can be off from the reported interval boundary by up to `step_minutes` — fine
for scheduling/gating decisions, not for precision rise/set timing.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import TypedDict

import astropy.units as u
from astropy.coordinates import ICRS, AltAz, EarthLocation, SkyCoord
from astropy.time import Time

from indi_mcp.observatory_store import Observatory

__all__ = ["VisibilityInterval", "compute_visibility"]


class VisibilityInterval(TypedDict):
    """One contiguous stretch of the requested timespan where the target's altitude was at or
    above `min_altitude_deg`, bounded by the sample times that were found above/below it —
    see `compute_visibility`'s sampling-precision caveat."""

    start: datetime
    end: datetime


def compute_visibility(
    *,
    ra_deg: float,
    dec_deg: float,
    observatory: Observatory,
    start: datetime,
    end: datetime,
    min_altitude_deg: float = 0.0,
    step_minutes: float = 5.0,
) -> list[VisibilityInterval]:
    """Return the above-horizon intervals for target (`ra_deg`, `dec_deg`, ICRS/J2000) as seen
    from `observatory`, between `start` and `end`.

    Empty if the target never reaches `min_altitude_deg` during the timespan; a single interval
    spanning the whole `[start, end]` if it stays above it throughout.

    `min_altitude_deg` defaults to the geometric horizon (0 degrees) but is commonly set higher
    to also exclude altitudes still too low for useful imaging (atmospheric extinction,
    horizon obstructions).

    `start`/`end` should be timezone-aware; `astropy.time.Time` is given whatever it is
    directly, so naive `datetime`s would be silently treated as UTC by astropy's own default —
    callers should always pass aware values (same requirement as `fits_headers.
    compute_celestial_context`).

    Sampling runs across `[start, end]` at intervals of *at most* `step_minutes`, always
    including both endpoints exactly: the actual spacing is narrowed just enough to divide the
    span into a whole number of equal steps, so it only equals `step_minutes` exactly when the
    span is itself a whole multiple of it. Either way, a rise/set moment falling between two
    samples is only known to within one step — pass a smaller `step_minutes` for a tighter (at
    the cost of more computation) boundary. Raises `ValueError` if `end` is not after `start`,
    if `step_minutes` is not positive, or if `dec_deg` is outside astropy's own -90..90 valid
    range (propagated from constructing the target `SkyCoord`).
    """
    if end <= start:
        raise ValueError(f"end ({end!r}) must be after start ({start!r})")
    if step_minutes <= 0:
        raise ValueError(f"step_minutes must be positive, got {step_minutes!r}")

    location = EarthLocation.from_geodetic(
        lon=observatory.longitudeDeg * u.deg,
        lat=observatory.latitudeDeg * u.deg,
        height=observatory.elevationMeters * u.m,
    )
    target = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg, frame=ICRS())

    span_seconds = (end - start).total_seconds()
    sample_count = max(2, math.ceil(span_seconds / (step_minutes * 60)) + 1)
    actual_step_seconds = span_seconds / (sample_count - 1)
    sample_times = [start + timedelta(seconds=i * actual_step_seconds) for i in range(sample_count)]

    altitudes_deg = target.transform_to(
        AltAz(obstime=Time(sample_times), location=location)
    ).alt.to_value(u.deg)

    intervals: list[VisibilityInterval] = []
    interval_start: datetime | None = None
    for altitude_deg, sample_time in zip(altitudes_deg, sample_times, strict=True):
        is_above = altitude_deg >= min_altitude_deg
        if is_above and interval_start is None:
            interval_start = sample_time
        elif not is_above and interval_start is not None:
            intervals.append({"start": interval_start, "end": sample_time})
            interval_start = None
    if interval_start is not None:
        intervals.append({"start": interval_start, "end": sample_times[-1]})

    return intervals
