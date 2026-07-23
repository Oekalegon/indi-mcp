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


def test_compute_celestial_context_returns_all_four_fields_within_valid_ranges() -> None:
    context = fits_headers.compute_celestial_context(
        ra_hours=2.767,
        dec_deg=62.52,
        observatory=_OBSERVATORY,
        at=datetime(2026, 1, 15, 20, 0, 0, tzinfo=UTC),
    )

    assert set(context) == {
        "sunAltitudeDeg",
        "moonSeparationDeg",
        "moonIlluminationFraction",
        "elongationDeg",
    }
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

    assert round(context["sunAltitudeDeg"], 4) == context["sunAltitudeDeg"]
    assert round(context["moonSeparationDeg"], 4) == context["moonSeparationDeg"]
    assert round(context["moonIlluminationFraction"], 4) == context["moonIlluminationFraction"]
    assert round(context["elongationDeg"], 4) == context["elongationDeg"]


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


def test_write_fits_headers_writes_all_four_keywords_with_comments() -> None:
    context: fits_headers.CelestialContext = {
        "sunAltitudeDeg": -37.0113,
        "moonSeparationDeg": 137.1356,
        "moonIlluminationFraction": 0.0856,
        "elongationDeg": 114.9043,
    }

    updated = fits_headers.write_fits_headers(_minimal_fits_bytes(), context)

    assert updated is not None
    with fits.open(io.BytesIO(updated)) as hdul:
        header = hdul[0].header
        assert header["SUNALT"] == -37.0113
        assert header.comments["SUNALT"] == "[deg] Sun altitude at obs time"
        assert header["MOONSEP"] == 137.1356
        assert header.comments["MOONSEP"] == "[deg] Moon-target angular separation"
        assert header["MOONPHSE"] == 0.0856
        assert header.comments["MOONPHSE"] == "Moon illumination fraction [0-1]"
        assert header["ELONGAT"] == 114.9043
        assert header.comments["ELONGAT"] == "[deg] Sun-target elongation"


def test_write_fits_headers_preserves_existing_data_and_headers() -> None:
    hdu = fits.PrimaryHDU(data=np.arange(16, dtype=np.uint16).reshape(4, 4))
    hdu.header["EXPTIME"] = 5.0
    buffer = io.BytesIO()
    hdu.writeto(buffer)
    context: fits_headers.CelestialContext = {
        "sunAltitudeDeg": 1.0,
        "moonSeparationDeg": 2.0,
        "moonIlluminationFraction": 0.5,
        "elongationDeg": 3.0,
    }

    updated = fits_headers.write_fits_headers(buffer.getvalue(), context)

    assert updated is not None
    with fits.open(io.BytesIO(updated)) as hdul:
        assert hdul[0].header["EXPTIME"] == 5.0
        np.testing.assert_array_equal(hdul[0].data, np.arange(16, dtype=np.uint16).reshape(4, 4))


@pytest.mark.parametrize("data", [b"", b"not a fits file", b"\x00\x01\x02\x03"])
def test_write_fits_headers_returns_none_for_non_fits_data(data: bytes) -> None:
    context: fits_headers.CelestialContext = {
        "sunAltitudeDeg": 0.0,
        "moonSeparationDeg": 0.0,
        "moonIlluminationFraction": 0.0,
        "elongationDeg": 0.0,
    }

    assert fits_headers.write_fits_headers(data, context) is None
