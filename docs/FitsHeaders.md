# FITS Headers

`capture_frame` (the step primitive and the built-in script — see [ScriptSchema.md](ScriptSchema.md#capture_frame))
saves each captured frame's bytes essentially as received from the camera driver: whatever
the driver itself already wrote into the FITS header (`EXPTIME`, `CCD-TEMP`, and similar —
driver-dependent, not something this project controls) is preserved as-is, and indi-mcp adds
its own enrichment keywords on top. This is implemented in
[`fits_headers.py`](../src/indi_mcp/fits_headers.py) plus `script_engine._add_fits_header_fields`
(INDIMCP-60).

## Keywords written

### Every frame type

This tier is about the capture itself — camera, filter, gain, offset, when — meaningful for
a calibration frame exactly as much as a Light frame (a Dark's gain/offset needs to match the
Lights it calibrates; a Flat needs to record which filter it was taken through).

| Keyword | Meaning | Comment written | Written when |
|---|---|---|---|
| `DATE-OBS` | UTC date/time the exposure command was sent | `UTC date/time of exposure start` | Always |
| `INSTRUME` | Camera used (its INDI device name) | `Camera (INDI device name)` | Always |
| `GAIN` | Sensor gain, if `capture_frame`'s `gain` was set | `Camera gain` | Only if `gain` was set — omitting it means "leave the device's current setting alone", not "gain unknown" |
| `OFFSET` | Sensor offset, if `capture_frame`'s `offset` was set | `Camera offset` | Only if `offset` was set, same reasoning as `GAIN` |
| `FILTER` | Currently selected filter's name | `Filter name` | Only if the rig has a resolvable `"filterWheel"` component reporting a slot that's in the rig's own `slots` map |

`DATE-OBS` is always overwritten with indi-mcp's own authoritative timestamp (taken right
before `CCD_EXPOSURE` is sent) rather than only filled in if missing — this server knows
precisely when it commanded the exposure to start, at least as accurately as whatever the
driver itself would stamp.

### `Light` frames only

A Dark/Flat/Bias calibration frame isn't captured "of" anything at the mount's current
pointing in any meaningful sense — the mount can be tracking, parked, or capped during a
calibration sequence — so telescope position and celestial context computed from wherever it
happens to be pointed would be misleading rather than useful, not just unnecessary work.

| Keyword | Meaning | Comment written | Written when |
|---|---|---|---|
| `RA` | Telescope's right ascension at exposure start, in decimal hours | `[h] Telescope RA (EOD) at obs time` | The rig has a resolvable `"mount"` component reporting a parseable `EQUATORIAL_EOD_COORD` |
| `DEC` | Telescope's declination at exposure start, in decimal degrees | `[deg] Telescope Dec (EOD) at obs time` | Same as `RA` |
| `SUNALT` | Sun's altitude above the horizon, in degrees, at the observatory's location | `[deg] Sun altitude at obs time` | `RA`/`DEC` available **and** a `location_id` was given for the run |
| `MOONSEP` | Angular separation between the target and the Moon, in degrees | `[deg] Moon-target angular separation` | Same as `SUNALT` |
| `MOONPHSE` | Moon's illuminated fraction, `0` (New) to `1` (Full) | `Moon illumination fraction [0-1]` | Same as `SUNALT` |
| `ELONGAT` | Angular separation between the target and the Sun (solar elongation), in degrees | `[deg] Sun-target elongation` | Same as `SUNALT` |

Note the two-stage gating: `RA`/`DEC` only need a resolvable, reporting mount — they're
available even without a `location_id`. `SUNALT`/`MOONSEP`/`MOONPHSE`/`ELONGAT` additionally
need an observer location to compute an Alt-Az frame (`SUNALT`) or to stay consistent with
it, so they're only written when both a mount pointing *and* a `location_id` are available.

Every celestial-context value is rounded to 4 decimal places before being written (arcmin-
level precision — this is descriptive metadata, not precision astrometry — and keeps the
FITS card well within its 80-character limit).

`SUNALT`/`MOONSEP`/`MOONPHSE` match the keyword conventions already used by AstroKit's
`cfitsio_wrapper.c` celestial-context writer and read back by Navi, so frames captured by
this project stay consistent with the rest of that ecosystem. `ELONGAT` has no prior
convention in either — chosen to match their 8-character keyword style.

## Best-effort, not required

Adding any of these headers is **best-effort**. All of the following are legitimate,
unremarkable reasons a captured frame won't have some (or all) of them — none of them fail
the capture itself:

- **The frame isn't a `Light` frame** — skips the whole "Light frames only" tier above.
- **No `location_id` given for the run** — skips `SUNALT`/`MOONSEP`/`MOONPHSE`/`ELONGAT`
  specifically (`RA`/`DEC` are unaffected).
- **The rig has no resolvable `"mount"` component**, or the mount isn't reporting a
  parseable `EQUATORIAL_EOD_COORD` (e.g. disconnected) — skips `RA`/`DEC` and everything
  that depends on them.
- **The rig has no resolvable `"filterWheel"` component**, `FILTER_SLOT` is undefined, or
  the current slot isn't in the rig's own `slots` map — skips `FILTER`.
- **`gain`/`offset` weren't set on the `capture_frame` call** — skips `GAIN`/`OFFSET`
  respectively (this one's intentional even when available, not a fallback).
- **The captured data isn't a FITS file at all.** Not every INDI camera driver necessarily
  streams FITS.

In every one of these cases the frame is still captured and saved normally — just with
fewer (or none) of the extra headers.

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
