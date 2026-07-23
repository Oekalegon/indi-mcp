# FITS Headers

`capture_frame` (the step primitive and the built-in script — see [ScriptSchema.md](ScriptSchema.md#capture_frame))
saves each captured frame's bytes essentially as received from the camera driver: whatever
the driver itself already wrote into the FITS header (`EXPTIME`, `CCD-TEMP`, `DATE-OBS`,
and similar — driver-dependent, not something this project controls) is preserved as-is.

On top of that, indi-mcp adds a small set of **celestial-context** keywords the driver has
no way to compute itself — it doesn't know the observatory's location or what the mount is
currently pointed at (the camera and mount are separate INDI devices). This is implemented
in [`fits_headers.py`](../src/indi_mcp/fits_headers.py) (INDIMCP-60).

## Keywords written

| Keyword | Meaning | Comment written |
|---|---|---|
| `SUNALT` | Sun's altitude above the horizon, in degrees, at the observatory's location | `[deg] Sun altitude at obs time` |
| `MOONSEP` | Angular separation between the target and the Moon, in degrees | `[deg] Moon-target angular separation` |
| `MOONPHSE` | Moon's illuminated fraction, `0` (New) to `1` (Full) | `Moon illumination fraction [0-1]` |
| `ELONGAT` | Angular separation between the target and the Sun (solar elongation), in degrees | `[deg] Sun-target elongation` |

Every value is rounded to 4 decimal places before being written (arcmin-level precision —
this is descriptive metadata, not precision astrometry — and keeps the FITS card well
within its 80-character limit).

`SUNALT`/`MOONSEP`/`MOONPHSE` match the keyword conventions already used by AstroKit's
`cfitsio_wrapper.c` celestial-context writer and read back by Navi, so frames captured by
this project stay consistent with the rest of that ecosystem. `ELONGAT` has no prior
convention in either — chosen to match their 8-character keyword style.

## When these are (and aren't) written

Adding these headers is **best-effort**, not a requirement of every capture. All of the
following are legitimate, unremarkable reasons a captured frame won't have them — none of
them fail the capture itself:

- **The frame isn't a `Light` frame.** A Dark/Flat/Bias calibration frame isn't captured "of"
  anything at the mount's current pointing in any meaningful sense — the mount can be
  tracking, parked, or capped during a calibration sequence — so celestial context computed
  from wherever it happens to be pointed would be misleading rather than useful.
- **No `location_id` given for the run.** `run_script`/`execute_script` take an optional
  `location_id` naming a saved [`Observatory`](ObservatorySchema.md); without one, there's
  no location to compute the Sun/Moon's position relative to, so nothing is attempted.
- **The rig has no resolvable `"mount"` component.** A camera-only test rig, or a rig where
  the `mount` role is ambiguous, means there's no current pointing to compute context for.
- **The mount isn't reporting `EQUATORIAL_EOD_COORD`** (undefined, or a value that doesn't
  parse as RA/Dec — e.g. disconnected).
- **The captured data isn't a FITS file at all.** Not every INDI camera driver necessarily
  streams FITS.

In every one of these cases the frame is still captured and saved normally — just without
the extra headers.

## Coordinate convention

The mount's `EQUATORIAL_EOD_COORD` reports geocentric apparent ("epoch of date") RA/Dec —
matched here by astropy's `TETE` frame, not `ICRS`/J2000. Sun and Moon positions used for
`MOONSEP`/`MOONPHSE`/`ELONGAT` are computed geocentrically too (not topocentric/parallax-
corrected), to stay in the same reference frame as the target — the Moon's ~1° of
topocentric parallax would otherwise leak in as error. `SUNALT` is the one topocentric
quantity here, since "altitude above the horizon" is inherently relative to a specific
location.

## Not yet included

A Simbad object cross-ID lookup (catalog identifier, coordinates, magnitude for the
current target) was considered as part of this feature but deferred — see INDIMCP-68 for
the open questions (a new `astroquery` dependency, how to resolve "what object is this"
from just the mount's coordinates rather than a name, keyword naming, and doing a network
lookup mid-capture without blocking or failing the capture if it's unavailable).
