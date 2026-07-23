import io
from datetime import UTC, datetime

import numpy as np
import pytest
from astropy.io import fits

from indi_mcp import fits_headers
from indi_mcp.observatory_store import Observatory

_OBSERVATORY = Observatory(
    id="test-observatory",
    name="Test Observatory",
    latitudeDeg=60.369722,
    longitudeDeg=11.363611,
    elevationMeters=350,
)


def _minimal_fits_bytes() -> bytes:
    hdu = fits.PrimaryHDU(data=np.zeros((4, 4), dtype=np.uint16))
    buffer = io.BytesIO()
    hdu.writeto(buffer)
    return buffer.getvalue()


def test_compute_celestial_context_returns_all_seven_fields_within_valid_ranges() -> None:
    context = fits_headers.compute_celestial_context(
        ra_hours=2.767,
        dec_deg=62.52,
        observatory=_OBSERVATORY,
        at=datetime(2026, 1, 15, 20, 0, 0, tzinfo=UTC),
    )

    assert set(context) == {
        "targetAltitudeDeg",
        "targetAzimuthDeg",
        "airmass",
        "sunAltitudeDeg",
        "moonSeparationDeg",
        "moonIlluminationFraction",
        "elongationDeg",
    }
    assert -90 <= context["targetAltitudeDeg"] <= 90
    assert 0 <= context["targetAzimuthDeg"] <= 360
    assert context["airmass"] > 0
    assert -90 <= context["sunAltitudeDeg"] <= 90
    assert 0 <= context["moonSeparationDeg"] <= 180
    assert 0 <= context["moonIlluminationFraction"] <= 1
    assert 0 <= context["elongationDeg"] <= 180


def test_compute_celestial_context_rounds_every_value_to_4_decimal_places() -> None:
    context = fits_headers.compute_celestial_context(
        ra_hours=2.767,
        dec_deg=62.52,
        observatory=_OBSERVATORY,
        at=datetime(2026, 1, 15, 20, 0, 0, tzinfo=UTC),
    )

    assert round(context["targetAltitudeDeg"], 4) == context["targetAltitudeDeg"]
    assert round(context["targetAzimuthDeg"], 4) == context["targetAzimuthDeg"]
    assert round(context["airmass"], 4) == context["airmass"]
    assert round(context["sunAltitudeDeg"], 4) == context["sunAltitudeDeg"]
    assert round(context["moonSeparationDeg"], 4) == context["moonSeparationDeg"]
    assert round(context["moonIlluminationFraction"], 4) == context["moonIlluminationFraction"]
    assert round(context["elongationDeg"], 4) == context["elongationDeg"]


def test_compute_celestial_context_airmass_matches_simple_secant_formula() -> None:
    """Self-consistency check: airmass should match the simple `1 / sin(altitude)`
    approximation applied to the target altitude this same call computed."""
    import math

    context = fits_headers.compute_celestial_context(
        ra_hours=2.767,
        dec_deg=62.52,
        observatory=_OBSERVATORY,
        at=datetime(2026, 1, 15, 20, 0, 0, tzinfo=UTC),
    )

    expected = round(1 / math.sin(math.radians(max(context["targetAltitudeDeg"], 0.1))), 4)
    assert context["airmass"] == expected


def test_compute_celestial_context_moon_illumination_near_full_moon() -> None:
    """2026-01-03 is close to a full moon — illumination should be near 1.0."""
    context = fits_headers.compute_celestial_context(
        ra_hours=2.767,
        dec_deg=62.52,
        observatory=_OBSERVATORY,
        at=datetime(2026, 1, 3, 12, 0, 0, tzinfo=UTC),
    )

    assert context["moonIlluminationFraction"] > 0.95


def test_compute_celestial_context_moon_illumination_near_new_moon() -> None:
    """2026-01-18 is close to a new moon — illumination should be near 0.0."""
    context = fits_headers.compute_celestial_context(
        ra_hours=2.767,
        dec_deg=62.52,
        observatory=_OBSERVATORY,
        at=datetime(2026, 1, 18, 12, 0, 0, tzinfo=UTC),
    )

    assert context["moonIlluminationFraction"] < 0.05


def test_compute_celestial_context_raises_no_warnings() -> None:
    """Regression guard: astropy's NonRotationTransformationWarning (mixing topocentric and
    geocentric frames) and fits.card's VerifyWarning (an unrounded value overflowing an
    80-char FITS card) were both real bugs caught during development — see the module's own
    docstrings for why they don't apply here. `-W error` turns either back into a hard
    failure if either regresses.
    """
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        fits_headers.compute_celestial_context(
            ra_hours=2.767,
            dec_deg=62.52,
            observatory=_OBSERVATORY,
            at=datetime(2026, 1, 15, 20, 0, 0, tzinfo=UTC),
        )


_TEST_CONTEXT: fits_headers.CelestialContext = {
    "targetAltitudeDeg": 45.6789,
    "targetAzimuthDeg": 210.1234,
    "airmass": 1.4142,
    "sunAltitudeDeg": -37.0113,
    "moonSeparationDeg": 137.1356,
    "moonIlluminationFraction": 0.0856,
    "elongationDeg": 114.9043,
}


def test_celestial_context_fields_maps_context_to_keywords_values_and_comments() -> None:
    fields = fits_headers.celestial_context_fields(_TEST_CONTEXT)

    assert fields == {
        "OBJCTALT": (45.6789, "[deg] Target altitude at obs time"),
        "OBJCTAZ": (210.1234, "[deg] Target azimuth at obs time"),
        "AIRMASS": (1.4142, "Airmass (approx., sec(zenith angle))"),
        "SUNALT": (-37.0113, "[deg] Sun altitude at obs time"),
        "MOONSEP": (137.1356, "[deg] Moon-target angular separation"),
        "MOONPHSE": (0.0856, "Moon illumination fraction [0-1]"),
        "ELONGAT": (114.9043, "[deg] Sun-target elongation"),
    }


def test_write_fits_headers_writes_all_celestial_context_keywords_with_comments() -> None:
    updated = fits_headers.write_fits_headers(
        _minimal_fits_bytes(), fits_headers.celestial_context_fields(_TEST_CONTEXT)
    )

    assert updated is not None
    with fits.open(io.BytesIO(updated)) as hdul:
        header = hdul[0].header
        assert header["OBJCTALT"] == 45.6789
        assert header["OBJCTAZ"] == 210.1234
        assert header["AIRMASS"] == 1.4142
        assert header["SUNALT"] == -37.0113
        assert header.comments["SUNALT"] == "[deg] Sun altitude at obs time"
        assert header["MOONSEP"] == 137.1356
        assert header.comments["MOONSEP"] == "[deg] Moon-target angular separation"
        assert header["MOONPHSE"] == 0.0856
        assert header.comments["MOONPHSE"] == "Moon illumination fraction [0-1]"
        assert header["ELONGAT"] == 114.9043
        assert header.comments["ELONGAT"] == "[deg] Sun-target elongation"


def test_compute_target_position_converts_eod_to_j2000() -> None:
    """M31 (Andromeda Galaxy)-ish coordinates — regression guard that the EOD -> J2000
    conversion moves the position by a plausible amount (arcmin-to-degree scale for a
    current-epoch date, not zero, not wildly wrong) rather than checking exact values,
    since there's no independent ground truth for a synthetic EOD input in this test."""
    position = fits_headers.compute_target_position(
        ra_hours=0.712,
        dec_deg=41.27,
        at=datetime(2026, 1, 15, 20, 0, 0, tzinfo=UTC),
    )

    assert set(position) == {
        "raDegJ2000",
        "decDegJ2000",
        "raSexagesimalJ2000",
        "decSexagesimalJ2000",
    }
    assert abs(position["raDegJ2000"] - 0.712 * 15) < 1
    assert abs(position["decDegJ2000"] - 41.27) < 1
    assert position["raSexagesimalJ2000"].count(" ") == 2
    assert position["decSexagesimalJ2000"].count(" ") == 2
    assert position["decSexagesimalJ2000"][0] in "+-"


def test_compute_target_position_raises_no_warnings() -> None:
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        fits_headers.compute_target_position(
            ra_hours=0.712, dec_deg=41.27, at=datetime(2026, 1, 15, 20, 0, 0, tzinfo=UTC)
        )


def test_target_position_fields_matches_ekos_keyword_convention() -> None:
    position: fits_headers.TargetPosition = {
        "raDegJ2000": 304.0072,
        "decDegJ2000": 43.66526,
        "raSexagesimalJ2000": "20 16 01.73",
        "decSexagesimalJ2000": "+43 39 54.93",
    }

    fields = fits_headers.target_position_fields(position)

    assert fields == {
        "OBJCTRA": ("20 16 01.73", "Object J2000 RA in Hours"),
        "OBJCTDEC": ("+43 39 54.93", "Object J2000 DEC in Degrees"),
        "RA": (304.0072, "Object J2000 RA in Degrees"),
        "DEC": (43.66526, "Object J2000 DEC in Degrees"),
        "EQUINOX": (2000.0, "Equinox"),
    }


def test_write_fits_headers_preserves_existing_data_and_headers() -> None:
    hdu = fits.PrimaryHDU(data=np.arange(16, dtype=np.uint16).reshape(4, 4))
    hdu.header["EXPTIME"] = 5.0
    buffer = io.BytesIO()
    hdu.writeto(buffer)
    context = _TEST_CONTEXT

    updated = fits_headers.write_fits_headers(
        buffer.getvalue(), fits_headers.celestial_context_fields(context)
    )

    assert updated is not None
    with fits.open(io.BytesIO(updated)) as hdul:
        assert hdul[0].header["EXPTIME"] == 5.0
        np.testing.assert_array_equal(hdul[0].data, np.arange(16, dtype=np.uint16).reshape(4, 4))


@pytest.mark.parametrize("data", [b"", b"not a fits file", b"\x00\x01\x02\x03"])
def test_write_fits_headers_returns_none_for_non_fits_data(data: bytes) -> None:
    fields: fits_headers.FitsHeaderFields = {"SUNALT": (0.0, "test")}

    assert fits_headers.write_fits_headers(data, fields) is None


def test_write_fits_headers_returns_none_for_empty_fields() -> None:
    assert fits_headers.write_fits_headers(_minimal_fits_bytes(), {}) is None
