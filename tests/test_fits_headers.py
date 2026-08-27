import io
import math
from datetime import UTC, datetime

import numpy as np
import pytest
from astropy.io import fits

from indi_mcp import fits_headers
from indi_mcp.observatory_store import Observatory

_OBSERVATORY = Observatory(
    id="test-observatory",
    name="Test Observatory",
    latitudeDeg=52.3676,
    longitudeDeg=4.9041,
    elevationMeters=4,
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


def _cd_matrix_from_cdelt_crota(
    cdelt1: float, cdelt2: float, crota2_deg: float
) -> tuple[float, float, float, float]:
    """The standard AIPS-convention construction, used here only to build a CD matrix with a
    known cdelt/crota for round-trip testing `wcs_fields_from_cd_matrix`'s own inverse of it."""
    rho = math.radians(crota2_deg)
    return (
        cdelt1 * math.cos(rho),
        -cdelt2 * math.sin(rho),
        cdelt1 * math.sin(rho),
        cdelt2 * math.cos(rho),
    )


@pytest.mark.parametrize(
    "cdelt1,cdelt2,crota2",
    [
        (-0.0002, 0.0002, 15.0),
        (-0.0002, 0.0002, 0.0),
        (-0.0002, 0.0002, 90.0),
        (-0.0002, 0.0002, -30.0),
        (-0.00015, 0.00021, 160.0),
    ],
)
def test_wcs_fields_from_cd_matrix_round_trips_cdelt_and_crota(
    cdelt1: float, cdelt2: float, crota2: float
) -> None:
    """A CD matrix built from a known (cdelt1, cdelt2, crota2) via the standard AIPS
    construction must recover the same values (crota2 modulo 360, since it's an angle) —
    this is the actual astrometric correctness of the CD-matrix -> CDELT/CROTA conversion
    INDIMCP-69 exists to get right."""
    cd1_1, cd1_2, cd2_1, cd2_2 = _cd_matrix_from_cdelt_crota(cdelt1, cdelt2, crota2)

    fields = fits_headers.wcs_fields_from_cd_matrix(
        ra_deg_j2000=150.25,
        dec_deg_j2000=20.5,
        crpix1=512.0,
        crpix2=512.0,
        ctype1="RA---TAN",
        ctype2="DEC--TAN",
        cd1_1=cd1_1,
        cd1_2=cd1_2,
        cd2_1=cd2_1,
        cd2_2=cd2_2,
    )

    assert float(fields["CDELT1"][0]) == pytest.approx(cdelt1)
    assert float(fields["CDELT2"][0]) == pytest.approx(cdelt2)
    recovered_crota2 = float(fields["CROTA2"][0])
    angle_difference = (recovered_crota2 - crota2 + 180) % 360 - 180
    assert angle_difference == pytest.approx(0.0, abs=1e-6)


def test_wcs_fields_from_cd_matrix_maps_every_expected_keyword() -> None:
    fields = fits_headers.wcs_fields_from_cd_matrix(
        ra_deg_j2000=150.25,
        dec_deg_j2000=20.5,
        crpix1=512.0,
        crpix2=512.0,
        ctype1="RA---TAN",
        ctype2="DEC--TAN",
        cd1_1=-0.0002,
        cd1_2=0.0,
        cd2_1=0.0,
        cd2_2=0.0002,
    )

    assert set(fields) == {
        "CRVAL1",
        "CRVAL2",
        "CTYPE1",
        "CTYPE2",
        "CRPIX1",
        "CRPIX2",
        "CDELT1",
        "CDELT2",
        "CROTA1",
        "CROTA2",
        "SECPIX1",
        "SECPIX2",
        "RADECSYS",
        "EQUINOX",
    }
    assert fields["CRVAL1"][0] == 150.25
    assert fields["CRVAL2"][0] == 20.5
    assert fields["CTYPE1"][0] == "RA---TAN"
    assert fields["CTYPE2"][0] == "DEC--TAN"
    assert fields["CRPIX1"][0] == 512.0
    assert fields["CRPIX2"][0] == 512.0
    assert fields["CROTA1"][0] == fields["CROTA2"][0]
    assert fields["RADECSYS"][0] == "FK5"
    assert fields["EQUINOX"][0] == 2000.0


def test_wcs_fields_from_cd_matrix_computes_secpix_from_cdelt() -> None:
    fields = fits_headers.wcs_fields_from_cd_matrix(
        ra_deg_j2000=150.25,
        dec_deg_j2000=20.5,
        crpix1=512.0,
        crpix2=512.0,
        ctype1="RA---TAN",
        ctype2="DEC--TAN",
        cd1_1=-0.0002,
        cd1_2=0.0,
        cd2_1=0.0,
        cd2_2=0.0002,
    )

    assert fields["SECPIX1"][0] == pytest.approx(0.0002 * 3600)
    assert fields["SECPIX2"][0] == pytest.approx(0.0002 * 3600)


def test_wcs_fields_from_cd_matrix_preserves_parity_of_a_mirrored_solve() -> None:
    """A positive-determinant CD matrix (an odd number of reflections somewhere in the optical
    path) must still round-trip to a positive CDELT1 rather than silently flipping parity."""
    cdelt1, cdelt2, crota2 = 0.0002, 0.0002, 15.0  # positive cdelt1: mirrored orientation
    cd1_1, cd1_2, cd2_1, cd2_2 = _cd_matrix_from_cdelt_crota(cdelt1, cdelt2, crota2)

    fields = fits_headers.wcs_fields_from_cd_matrix(
        ra_deg_j2000=150.25,
        dec_deg_j2000=20.5,
        crpix1=512.0,
        crpix2=512.0,
        ctype1="RA---TAN",
        ctype2="DEC--TAN",
        cd1_1=cd1_1,
        cd1_2=cd1_2,
        cd2_1=cd2_1,
        cd2_2=cd2_2,
    )

    assert float(fields["CDELT1"][0]) > 0


@pytest.mark.parametrize(
    "cd1_1,cd1_2,cd2_1,cd2_2",
    [
        (0.0, 0.0, 0.0, 0.0002),  # zero scale on axis 1
        (-0.0002, 0.0, 0.0, 0.0),  # zero scale on axis 2
    ],
)
def test_wcs_fields_from_cd_matrix_returns_empty_for_a_degenerate_matrix(
    cd1_1: float, cd1_2: float, cd2_1: float, cd2_2: float
) -> None:
    """A CD matrix with zero scale on either axis can't be inverted to CDELT/CROTA (would
    divide by zero) — shouldn't happen for a real optical system, but a corrupted .wcs file
    shouldn't crash this best-effort conversion either."""
    fields = fits_headers.wcs_fields_from_cd_matrix(
        ra_deg_j2000=150.25,
        dec_deg_j2000=20.5,
        crpix1=512.0,
        crpix2=512.0,
        ctype1="RA---TAN",
        ctype2="DEC--TAN",
        cd1_1=cd1_1,
        cd1_2=cd1_2,
        cd2_1=cd2_1,
        cd2_2=cd2_2,
    )

    assert fields == {}
