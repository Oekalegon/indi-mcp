"""Computing and writing enrichment FITS headers for a captured frame (INDIMCP-60).

`indiweb`/the camera driver itself can't compute the astronomy-derived part of this — it has
no notion of the observatory's location, or of what the mount is currently pointed at (mount
and camera are separate INDI devices, and the driver only knows its own device's
properties). This module fills that gap in two parts:

- `compute_target_position` converts the mount's EOD (epoch-of-date) RA/Dec to J2000, needing
  only the time — no observer location required.
- `compute_celestial_context` additionally needs an `Observatory` location: the target's
  topocentric altitude/azimuth/airmass, the Sun's altitude, the target's angular separation
  from the Moon, the Moon's illumination fraction, and the target's solar elongation.

Both are best-effort from `script_engine._execute_capture_frame`'s perspective — neither an
observatory location nor a resolvable/connected mount is guaranteed to be available for
every run.

`write_fits_headers` itself is generic — it takes any `FitsHeaderFields` (keyword -> (value,
comment)) and writes them into the captured frame's FITS primary header, `None` if the data
isn't FITS at all. `target_position_fields`/`celestial_context_fields` convert this module's
own computed results into that shape; `script_engine` also builds its own fields directly
(camera/gain/offset/filter/telescope-optics/focuser — not computed by this module, since
they're read off already-known step/device/rig state, not derived astronomy) and merges
everything into one `write_fits_headers` call.

FITS keyword conventions matched here (`SUNALT`/`MOONSEP`/`MOONPHSE`) come from AstroKit's
`cfitsio_wrapper.c` celestial-context writer and Navi's corresponding reader, to stay
consistent with the rest of this project's ecosystem rather than inventing new ones.
`ELONGAT` has no prior convention in either — chosen to match their 8-character style.

See `docs/FitsHeaders.md` for the full list of keywords this module writes and what they mean.
"""

from __future__ import annotations

import io
import logging
import math
import warnings
from datetime import datetime
from typing import TypedDict

import astropy.units as u
from astropy.coordinates import ICRS, TETE, AltAz, EarthLocation, SkyCoord, get_body
from astropy.coordinates.errors import NonRotationTransformationWarning
from astropy.io import fits
from astropy.time import Time
from astropy.utils import iers

from indi_mcp.observatory_store import Observatory

logger = logging.getLogger(__name__)

__all__ = [
    "CelestialContext",
    "FitsHeaderFields",
    "TargetPosition",
    "celestial_context_fields",
    "compute_celestial_context",
    "compute_target_position",
    "target_position_fields",
    "write_fits_headers",
]

FitsHeaderFields = dict[str, tuple[float | str, str]]
"""FITS keyword -> `(value, comment)`, the shape `write_fits_headers` writes directly."""

# The Pi this runs on may have no internet access (see docs/Deployment.md's LAN-only
# framing), and IERS bulletin auto-download would otherwise be attempted lazily on first
# use of precise Earth-orientation data — disabled so a capture never blocks on, or fails
# because of, a network call. astropy falls back to its bundled (slightly less precise,
# arcsecond-level, entirely sufficient for altitude/separation/illumination) IERS data.
iers.conf.auto_download = False


class CelestialContext(TypedDict):
    """Astropy-computed celestial context for one capture, at one place and time.

    Field names match `_FITS_KEYWORDS`' keys one-to-one — see `write_fits_headers`.
    """

    targetAltitudeDeg: float
    targetAzimuthDeg: float
    airmass: float
    sunAltitudeDeg: float
    moonSeparationDeg: float
    moonIlluminationFraction: float
    elongationDeg: float


_FITS_KEYWORDS: dict[str, tuple[str, str]] = {
    "targetAltitudeDeg": ("OBJCTALT", "[deg] Target altitude at obs time"),
    "targetAzimuthDeg": ("OBJCTAZ", "[deg] Target azimuth at obs time"),
    "airmass": ("AIRMASS", "Airmass (approx., sec(zenith angle))"),
    "sunAltitudeDeg": ("SUNALT", "[deg] Sun altitude at obs time"),
    "moonSeparationDeg": ("MOONSEP", "[deg] Moon-target angular separation"),
    "moonIlluminationFraction": ("MOONPHSE", "Moon illumination fraction [0-1]"),
    "elongationDeg": ("ELONGAT", "[deg] Sun-target elongation"),
}
"""`CelestialContext` field -> `(FITS keyword, comment)`. The single source of truth for
which keywords `write_fits_headers` writes — see `docs/FitsHeaders.md` for the human-facing
version of this table."""


def compute_celestial_context(
    *, ra_hours: float, dec_deg: float, observatory: Observatory, at: datetime
) -> CelestialContext:
    """Compute Sun altitude, Moon separation, Moon illumination, and elongation for a target.

    `ra_hours`/`dec_deg` are the target's coordinates exactly as `EQUATORIAL_EOD_COORD`
    reports them — INDI's "EOD" (epoch of date) convention is geocentric apparent
    coordinates (precessed and nutated to the observation date), matched here by astropy's
    `TETE` frame ("True Equator, True Equinox"), not `ICRS`/J2000. Treating EOD coordinates
    as ICRS directly would introduce a real (if small at the current epoch — a few arcmin,
    growing with distance from J2000) systematic offset, and astropy also warns whenever a
    `.separation()` call has to transform between GCRS (what `get_body` returns) and TETE,
    which it can't generally guarantee is a pure rotation — suppressed below with an
    explanation of why it doesn't matter at this module's precision.

    `at` should be timezone-aware; `astropy.time.Time` is given whatever it is directly, so
    a naive `datetime` would be silently treated as UTC by astropy's own default — callers
    should always pass an aware value (all other callers in this codebase already use
    `datetime.now(tz=UTC)`, see `script_engine`).

    Moon illumination fraction is computed from the exact Sun-Moon-Earth phase angle (via
    geocentric Cartesian vectors, not the small-angle `elongation` approximation some tools
    use), so it stays accurate even close to New/Full Moon where that approximation breaks
    down most.

    Every returned value is rounded to 4 decimal places (0.0001 deg is already an order of
    magnitude tighter than this data's own precision — INDI mount pointing accuracy is
    typically arcmin-level, not arcsec) — a FITS header card is 80 characters total, and an
    un-rounded Python `float`'s full `repr` (15+ significant digits) can push a card past
    that limit and get its comment silently truncated by `astropy.io.fits`.
    """
    location = EarthLocation.from_geodetic(
        lon=observatory.longitudeDeg * u.deg,
        lat=observatory.latitudeDeg * u.deg,
        height=observatory.elevationMeters * u.m,
    )
    time = Time(at)
    target = SkyCoord(ra=ra_hours * u.hourangle, dec=dec_deg * u.deg, frame=TETE(obstime=time))

    # Geocentric (no `location`) — matches the target's own geocentric-apparent convention.
    # The Moon's ~1 degree of topocentric parallax (far larger than the Sun's, negligible for
    # a star but not for the Moon) would otherwise leak in as error — Moon separation/
    # illumination and solar elongation are conventionally geocentric quantities anyway,
    # independent of where on Earth the observer stands.
    sun = get_body("sun", time)
    moon = get_body("moon", time)

    # Altitude is inherently topocentric — "above *my* horizon" only means something relative
    # to a specific location — so this one genuinely needs the observer's position.
    sun_topocentric = get_body("sun", time, location)
    sun_altaz = sun_topocentric.transform_to(AltAz(obstime=time, location=location))
    # Unlike Sun/Moon (whose positions come from `get_body`, GCRS), the target itself is
    # already `TETE`, a geocentric apparent frame with no notion of an observer's position —
    # this transform is what actually applies topocentric parallax/geometry for it, not a
    # frame-basis change, so it doesn't hit the GCRS<->TETE warning suppressed below.
    target_altaz = target.transform_to(AltAz(obstime=time, location=location))

    # astropy warns on any GCRS<->TETE `.separation()` call, geocentric or not, since it can't
    # generally guarantee the transform is a pure rotation. It is one here to the precision
    # this module cares about (arcmin-level FITS metadata, not sub-arcsec astrometry) — the
    # residual is orders of magnitude below the 4-decimal-place rounding these values get
    # before being written to the header.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", NonRotationTransformationWarning)
        moon_separation = target.separation(moon)
        elongation = target.separation(sun)

    return {
        "targetAltitudeDeg": round(float(target_altaz.alt.to_value(u.deg)), 4),
        "targetAzimuthDeg": round(float(target_altaz.az.to_value(u.deg)), 4),
        "airmass": round(_airmass(float(target_altaz.alt.to_value(u.deg))), 4),
        "sunAltitudeDeg": round(float(sun_altaz.alt.to_value(u.deg)), 4),
        "moonSeparationDeg": round(float(moon_separation.to_value(u.deg)), 4),
        "moonIlluminationFraction": round(_moon_illumination_fraction(sun, moon), 4),
        "elongationDeg": round(float(elongation.to_value(u.deg)), 4),
    }


def _airmass(altitude_deg: float) -> float:
    """Simple secant-of-zenith-angle airmass approximation: `1 / sin(altitude)`.

    Matches the simple formula most amateur imaging tools (including Ekos) use for this
    header — not a refined model (e.g. Kasten & Young 1989, which corrects for atmospheric
    curvature near the horizon), but well within the accuracy this metadata needs. Breaks
    down (grows without bound, or goes negative) at/below the horizon, same as the secant
    formula always does; `altitude_deg` clamped to a small positive floor to avoid a literal
    division by zero rather than trying to model "airmass of a target you can't see" at all.
    """
    return 1 / math.sin(math.radians(max(altitude_deg, 0.1)))


def _moon_illumination_fraction(sun: SkyCoord, moon: SkyCoord) -> float:
    """The Moon's illuminated fraction (0=New, 1=Full), from the exact phase angle at the
    Moon (the angle between the Moon-to-Sun and Moon-to-Earth directions).

    `sun`/`moon` must be geocentric (as `get_body` returns), so `-moon.cartesian` is the
    Moon-to-Earth vector. Plain Python `math`, not `numpy` — this is three scalar dot
    products, not worth astropy/numpy's array-broadcasting machinery, and keeps this module
    from needing a direct `numpy` import (it's already a transitive dependency of astropy).
    """
    sun_cart = sun.cartesian
    moon_cart = moon.cartesian
    moon_to_sun = sun_cart - moon_cart
    moon_to_earth = -moon_cart

    dot = (
        moon_to_sun.x * moon_to_earth.x
        + moon_to_sun.y * moon_to_earth.y
        + moon_to_sun.z * moon_to_earth.z
    )
    moon_to_sun_norm = (moon_to_sun.x**2 + moon_to_sun.y**2 + moon_to_sun.z**2) ** 0.5
    moon_to_earth_norm = (moon_to_earth.x**2 + moon_to_earth.y**2 + moon_to_earth.z**2) ** 0.5
    cos_phase_angle = float((dot / (moon_to_sun_norm * moon_to_earth_norm)).decompose().value)
    # Clamp against float rounding pushing an exact +/-1.0 case a hair outside acos's domain.
    cos_phase_angle = max(-1.0, min(1.0, cos_phase_angle))
    phase_angle = math.acos(cos_phase_angle)
    return (1 + math.cos(phase_angle)) / 2


class TargetPosition(TypedDict):
    """A target's coordinates converted from EOD (epoch of date) to J2000 (ICRS), in both
    decimal-degree and sexagesimal-string form — matches Ekos's own `OBJCTRA`/`OBJCTDEC`/
    `RA`/`DEC` convention exactly (see `compute_target_position`)."""

    raDegJ2000: float
    decDegJ2000: float
    raSexagesimalJ2000: str
    decSexagesimalJ2000: str


def compute_target_position(*, ra_hours: float, dec_deg: float, at: datetime) -> TargetPosition:
    """Convert a target's EOD (epoch-of-date) coordinates to J2000 (ICRS).

    Unlike `compute_celestial_context`, this needs no `Observatory` — precession/nutation
    depend only on time, not observer location — so it's available whenever a run has a
    resolvable mount reporting `EQUATORIAL_EOD_COORD`, regardless of whether a `location_id`
    was given.

    Ekos (and by extension AstroKit/Navi, which read Ekos-captured frames) write target
    position under `OBJCTRA`/`OBJCTDEC`/`RA`/`DEC` as **J2000**, not EOD — confirmed by
    inspecting a real Ekos-captured frame's header. Writing indi-mcp's own EOD values under
    those same keyword names would be a real interoperability bug: same keys, different
    meaning, silently wrong for anything that reads FITS files from both tools. This performs
    the same EOD -> J2000 conversion Ekos does, so indi-mcp's output means the same thing.
    """
    time = Time(at)
    target = SkyCoord(ra=ra_hours * u.hourangle, dec=dec_deg * u.deg, frame=TETE(obstime=time))
    j2000 = target.transform_to(ICRS())
    return {
        "raDegJ2000": round(float(j2000.ra.to_value(u.deg)), 4),
        "decDegJ2000": round(float(j2000.dec.to_value(u.deg)), 4),
        "raSexagesimalJ2000": j2000.ra.to_string(unit=u.hourangle, sep=" ", precision=2, pad=True),
        "decSexagesimalJ2000": j2000.dec.to_string(
            unit=u.deg, sep=" ", precision=2, alwayssign=True, pad=True
        ),
    }


def target_position_fields(position: TargetPosition) -> FitsHeaderFields:
    """Convert a computed `TargetPosition` into `write_fits_headers`' generic field shape,
    matching Ekos's `OBJCTRA`/`OBJCTDEC`/`RA`/`DEC`/`EQUINOX` keywords exactly."""
    return {
        "OBJCTRA": (position["raSexagesimalJ2000"], "Object J2000 RA in Hours"),
        "OBJCTDEC": (position["decSexagesimalJ2000"], "Object J2000 DEC in Degrees"),
        "RA": (position["raDegJ2000"], "Object J2000 RA in Degrees"),
        "DEC": (position["decDegJ2000"], "Object J2000 DEC in Degrees"),
        "EQUINOX": (2000.0, "Equinox"),
    }


def celestial_context_fields(context: CelestialContext) -> FitsHeaderFields:
    """Convert a computed `CelestialContext` into `write_fits_headers`' generic field shape."""
    return {
        "OBJCTALT": (context["targetAltitudeDeg"], _FITS_KEYWORDS["targetAltitudeDeg"][1]),
        "OBJCTAZ": (context["targetAzimuthDeg"], _FITS_KEYWORDS["targetAzimuthDeg"][1]),
        "AIRMASS": (context["airmass"], _FITS_KEYWORDS["airmass"][1]),
        "SUNALT": (context["sunAltitudeDeg"], _FITS_KEYWORDS["sunAltitudeDeg"][1]),
        "MOONSEP": (context["moonSeparationDeg"], _FITS_KEYWORDS["moonSeparationDeg"][1]),
        "MOONPHSE": (
            context["moonIlluminationFraction"],
            _FITS_KEYWORDS["moonIlluminationFraction"][1],
        ),
        "ELONGAT": (context["elongationDeg"], _FITS_KEYWORDS["elongationDeg"][1]),
    }


def write_fits_headers(data: bytes, fields: FitsHeaderFields) -> bytes | None:
    """Return `data` with `fields`' keywords added to its primary FITS header.

    `None` if `data` isn't a FITS file at all — not every INDI camera driver necessarily
    streams FITS (some support XISF or a native raw format instead), and this is best-effort
    metadata enrichment, not a requirement that every captured frame be FITS. The caller
    (`script_engine._execute_capture_frame`) falls back to saving `data` unmodified in that
    case, exactly as it did before this module existed. `None` (never called at all) if
    `fields` is empty, since opening/rewriting the file would be pure overhead for nothing.
    """
    if not fields:
        return None
    try:
        with fits.open(io.BytesIO(data)) as hdul:
            header = hdul[0].header
            for keyword, (value, comment) in fields.items():
                header[keyword] = (value, comment)
            buffer = io.BytesIO()
            hdul.writeto(buffer)
            return buffer.getvalue()
    except OSError:
        logger.debug("Captured frame is not a FITS file; skipping header enrichment")
        return None
