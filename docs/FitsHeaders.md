# FITS Headers

`capture_frame` (the step primitive and the built-in script — see [ScriptSchema.md](ScriptSchema.md#capture_frame))
saves each captured frame's bytes essentially as received from the camera driver, then adds
its own enrichment keywords on top. This page documents **every** FITS header property that
ends up in a captured frame — both the ones indi-mcp itself writes, and the ones typically
already present from the driver — so it's a complete reference for what to expect in a file,
not just a changelog of what INDIMCP-60 added.

indi-mcp's own enrichment is implemented in
[`fits_headers.py`](../src/indi_mcp/fits_headers.py) plus `script_engine._add_fits_header_fields`.

## Keywords indi-mcp writes

### Every frame type

This tier is about the capture and equipment setup itself — camera, gain, offset, optics,
focuser, site, when — meaningful for a calibration frame exactly as much as a Light frame (a
Dark's gain/offset needs to match the Lights it calibrates; the telescope/site didn't change
because this frame happens to be a Flat).

| Keyword | Meaning | Comment written | Written when |
|---|---|---|---|
| `DATE-OBS` | UTC date/time the exposure command was sent | `UTC date/time of exposure start` | Always |
| `INSTRUME` | Camera used (its INDI device name) | `Camera (INDI device name)` | Always |
| `GAIN` | Sensor gain, if `capture_frame`'s `gain` was set | `Camera gain` | Only if `gain` was set — omitting it means "leave the device's current setting alone", not "gain unknown" |
| `OFFSET` | Sensor offset, if `capture_frame`'s `offset` was set | `Camera offset` | Only if `offset` was set, same reasoning as `GAIN` |
| `FOCALLEN` | Telescope focal length, in mm | `[mm] Telescope focal length` | The rig has a resolvable `"telescope"` component with `focalLengthMm` set |
| `APTDIA` | Telescope aperture, in mm | `[mm] Telescope aperture` | Same rig component, `apertureMm` set |
| `TELESCOP` | Telescope make/model | `Telescope` | Same rig component, `make` and/or `model` set |
| `SCALE` | Plate scale, arcsec/pixel (`206.265 * pixel_size_um / focal_length_mm`) | `[arcsec/pixel] Plate scale` | Both `focalLengthMm` on the `"telescope"` component **and** `pixelSizeMicron` on the camera role's component are set |
| `FOCUSPOS` | Focuser position, in steps | `Focuser position in steps` | The rig has a resolvable `"focuser"` component reporting a parseable `ABS_FOCUS_POSITION` |
| `FOCUSTEM` | Focuser's own temperature sensor reading, in °C | `[C] Focuser temperature` | Same `"focuser"` component reporting a parseable `FOCUS_TEMPERATURE` — independent of `FOCUSPOS` (not every focuser has a temperature probe) |
| `SITELAT` | Observatory latitude, in degrees | `[deg] Observatory latitude` | A `location_id` was given for the run and resolves to a known observatory |
| `SITELONG` | Observatory longitude, in degrees | `[deg] Observatory longitude` | Same as `SITELAT` |

`DATE-OBS` is always overwritten with indi-mcp's own authoritative timestamp (taken right
before `CCD_EXPOSURE` is sent) rather than only filled in if missing — this server knows
precisely when it commanded the exposure to start, at least as accurately as whatever the
driver itself would stamp.

`FOCALLEN`/`APTDIA`/`TELESCOP`/`SCALE` are independently best-effort: a rig's `"telescope"`
component never has a `device` (it isn't an INDI driver — see
[RigSchema.md](RigSchema.md)), so these come purely from that component's own static fields,
not a live property read.

### `Light` and `Flat` frames

| Keyword | Meaning | Comment written | Written when |
|---|---|---|---|
| `FILTER` | Currently selected filter's name | `Filter name` | `frameType` is `Light` or `Flat`, **and** the rig has a resolvable `"filterWheel"` component reporting a slot that's in the rig's own `slots` map |

A Flat is taken *through* a specific filter, same as a Light — calibrating that filter's
illumination/vignetting pattern is the whole point of it, so its filter matters just as much.
A Dark/Bias is filter-independent (typically capped, sensor readout the same regardless of
the optical path) — recording a filter name on one would imply a dependency that doesn't
exist, even when the filter wheel is otherwise perfectly resolvable.

### `Light` frames only

A Dark/Flat/Bias calibration frame isn't captured "of" anything at the mount's current
pointing in any meaningful sense — the mount can be tracking, parked, or capped during a
calibration sequence — so telescope position and celestial context computed from wherever it
happens to be pointed would be misleading rather than useful, not just unnecessary work.

| Keyword | Meaning | Comment written | Written when |
|---|---|---|---|
| `OBJECT` | Caller-supplied label for what the frame targets (`capture_frame`'s `objectName`) | `Object` | `frameType` is `Light` **and** `objectName` was given — never resolved against a catalog, just recorded as given |
| `PIERSIDE` | Mount's pier side, `EAST` or `WEST` | `Mount pier side` | The rig has a resolvable `"mount"` component reporting `TELESCOPE_PIER_SIDE` — independent of whether `EQUATORIAL_EOD_COORD` is also available |
| `OBJCTRA` | Target right ascension, J2000, sexagesimal hours | `Object J2000 RA in Hours` | The rig has a resolvable `"mount"` component reporting a parseable `EQUATORIAL_EOD_COORD` |
| `OBJCTDEC` | Target declination, J2000, sexagesimal degrees | `Object J2000 DEC in Degrees` | Same as `OBJCTRA` |
| `RA` | Target right ascension, J2000, decimal degrees | `Object J2000 RA in Degrees` | Same as `OBJCTRA` |
| `DEC` | Target declination, J2000, decimal degrees | `Object J2000 DEC in Degrees` | Same as `OBJCTRA` |
| `EQUINOX` | Equinox of `OBJCTRA`/`OBJCTDEC`/`RA`/`DEC` — always `2000.0` | `Equinox` | Same as `OBJCTRA` |
| `OBJCTALT` | Target altitude above the horizon, in degrees, at exposure start | `[deg] Target altitude at obs time` | `OBJCTRA`/`OBJCTDEC` available **and** a `location_id` was given for the run |
| `OBJCTAZ` | Target azimuth, in degrees, at exposure start | `[deg] Target azimuth at obs time` | Same as `OBJCTALT` |
| `AIRMASS` | Approximate airmass (`1 / sin(altitude)`) | `Airmass (approx., sec(zenith angle))` | Same as `OBJCTALT` |
| `SUNALT` | Sun's altitude above the horizon, in degrees, at the observatory's location | `[deg] Sun altitude at obs time` | Same as `OBJCTALT` |
| `MOONSEP` | Angular separation between the target and the Moon, in degrees | `[deg] Moon-target angular separation` | Same as `OBJCTALT` |
| `MOONPHSE` | Moon's illuminated fraction, `0` (New) to `1` (Full) | `Moon illumination fraction [0-1]` | Same as `OBJCTALT` |
| `ELONGAT` | Angular separation between the target and the Sun (solar elongation), in degrees | `[deg] Sun-target elongation` | Same as `OBJCTALT` |

Note the two-stage gating: `OBJCTRA`/`OBJCTDEC`/`RA`/`DEC`/`EQUINOX` only need a resolvable,
reporting mount — they're available even without a `location_id`. `OBJCTALT`/`OBJCTAZ`/
`AIRMASS`/`SUNALT`/`MOONSEP`/`MOONPHSE`/`ELONGAT` additionally need an observer location to
compute an Alt-Az frame, so they're only written when both a mount pointing *and* a
`location_id` are available. `PIERSIDE` and `OBJECT` are independent of both — `PIERSIDE`
only needs the mount's own switch state, `OBJECT` only needs the caller-supplied `objectName`.

Every celestial-context value (`OBJCTALT`/`OBJCTAZ`/`AIRMASS`/`SUNALT`/`MOONSEP`/`MOONPHSE`/
`ELONGAT`) is rounded to 4 decimal places before being written (arcmin-level precision — this
is descriptive metadata, not precision astrometry — and keeps the FITS card well within its
80-character limit).

`OBJCTRA`/`OBJCTDEC`/`RA`/`DEC`/`EQUINOX` match Ekos's exact keyword/semantic convention
(verified against a real Ekos-captured frame): sexagesimal-hours/decimal-degrees J2000
coordinates, precessed from the mount's raw epoch-of-date reading, not the raw EOD values
themselves — see [Coordinate convention](#coordinate-convention) below. `SUNALT`/`MOONSEP`/
`MOONPHSE` match the keyword conventions already used by AstroKit's `cfitsio_wrapper.c`
celestial-context writer and read back by Navi, so frames captured by this project stay
consistent with the rest of that ecosystem. `ELONGAT` has no prior convention in either —
chosen to match their 8-character keyword style.

## Keywords the driver typically already writes

These are **not** written or controlled by indi-mcp — they come from the INDI camera driver
itself, before the BLOB ever reaches this server, and are preserved as-is (indi-mcp only ever
adds keywords, never removes or renames anything already present). Listed here for a
complete picture of what a captured frame usually contains, not as something this project
guarantees: exact keywords, presence, and values are driver-dependent, and this isn't
verified against every driver this project might run against.

Standard INDI CCD driver convention (`indibase`'s `CCD` class) typically includes:

| Keyword | Typical meaning |
|---|---|
| `EXPTIME` | Exposure length, in seconds |
| `DARKTIME` | For a Dark frame, the actual dark-current integration time |
| `PIXSIZE1` / `PIXSIZE2` | Pixel size, in microns |
| `XBINNING` / `YBINNING` | Pixel binning |
| `XPIXSZ` / `YPIXSZ` | Binned pixel size, in microns |
| `FRAME` / `IMAGETYP` | Frame type (`Light`/`Dark`/`Flat`/`Bias`) |
| `CCD-TEMP` | Sensor temperature, if the camera reports one |
| `FOCALLEN` | Telescope focal length, in mm (if configured on the driver side) |
| `APTDIA` | Telescope aperture, in mm (if configured on the driver side) |
| `BAYERPAT` | Bayer matrix pattern, for a one-shot-color sensor |
| `ROWORDER` | Pixel row readout order |

Where indi-mcp's own keywords overlap in *purpose* with one of these (`GAIN`/`OFFSET`
specifically — some drivers report their own; `FOCALLEN`/`APTDIA` too, if the driver has its
own optics configuration), indi-mcp's value reflects exactly what was commanded/configured on
the rig side, which is the more directly authoritative source for this specific exposure —
and it's written last, so it's the value that survives if both are present.

## Out of scope (for now)

The following properties from a real Ekos-captured frame were considered as part of this
feature but are explicitly **not** implemented, since they need capabilities this project
doesn't have yet rather than just missing plumbing:

- **WCS keywords** (`CRVAL1`/`CRVAL2`/`CTYPE1`/`CTYPE2`/`CRPIX1`/`CRPIX2`/`CDELT1`/`CDELT2`/
  `CROTA1`/`CROTA2`/`SECPIX1`/`SECPIX2`) — need a real plate-solve result, not just metadata
  already available at capture time. Tracked as INDIMCP-69, blocked on the astrometry.net
  integration (INDIMCP-27).
- **Star-detection quality keywords** (`NSTARS`/`SATSTARS`/`MEDFWHM`/`MEDECC`/`BACKNOIS`) —
  need real star-detection/image-analysis, not just header metadata. Tracked as INDIMCP-70.
- **Simbad object cross-ID** (catalog identifier, coordinates, magnitude for the current
  target) — deferred to INDIMCP-68 (a new `astroquery` dependency, how to resolve "what
  object is this" from just the mount's coordinates rather than a name, keyword naming, and
  doing a network lookup mid-capture without blocking or failing the capture if unavailable).

## Best-effort, not required

Adding any of indi-mcp's own headers is **best-effort**. All of the following are
legitimate, unremarkable reasons a captured frame won't have some (or all) of them — none of
them fail the capture itself:

- **The frame isn't `Light`/`Flat`** — skips `FILTER`.
- **The frame isn't `Light`** — skips the whole "Light frames only" tier (`OBJECT`,
  `PIERSIDE`, position, and celestial context).
- **No `location_id` given for the run** — skips `SITELAT`/`SITELONG` and, for a Light frame,
  `OBJCTALT`/`OBJCTAZ`/`AIRMASS`/`SUNALT`/`MOONSEP`/`MOONPHSE`/`ELONGAT` specifically
  (`OBJCTRA`/`OBJCTDEC`/`RA`/`DEC`/`EQUINOX` are unaffected).
- **The rig has no resolvable `"mount"` component**, or the mount isn't reporting a
  parseable `EQUATORIAL_EOD_COORD` (e.g. disconnected) — skips the J2000 position and
  everything that depends on it. `PIERSIDE` is independent — it only needs
  `TELESCOPE_PIER_SIDE`, not `EQUATORIAL_EOD_COORD`.
- **The rig has no resolvable `"filterWheel"` component**, `FILTER_SLOT` is undefined, or
  the current slot isn't in the rig's own `slots` map — skips `FILTER`.
- **The rig has no `"telescope"` component**, or it's missing `focalLengthMm`/`apertureMm`/
  `make`/`model` — skips the corresponding one of `FOCALLEN`/`APTDIA`/`TELESCOP`
  independently. `SCALE` additionally needs the camera role's component to have
  `pixelSizeMicron` set.
- **The rig has no resolvable `"focuser"` component**, or it isn't reporting
  `ABS_FOCUS_POSITION`/`FOCUS_TEMPERATURE` — skips `FOCUSPOS`/`FOCUSTEM` independently (not
  every focuser has a temperature probe).
- **`gain`/`offset` weren't set on the `capture_frame` call** — skips `GAIN`/`OFFSET`
  respectively (this one's intentional even when available, not a fallback).
- **`objectName` wasn't set on the `capture_frame` call** — skips `OBJECT`.
- **The captured data isn't a FITS file at all.** Not every INDI camera driver necessarily
  streams FITS.

In every one of these cases the frame is still captured and saved normally — just with
fewer (or none) of the extra headers.

## Coordinate convention

The mount's `EQUATORIAL_EOD_COORD` reports geocentric apparent ("epoch of date") RA/Dec —
matched here by astropy's `TETE` frame, not `ICRS`/J2000 directly. Before being written to
`OBJCTRA`/`OBJCTDEC`/`RA`/`DEC`, this is precessed to J2000 (`ICRS`) via
`fits_headers.compute_target_position`, matching Ekos's exact convention — an earlier version
of this feature wrote the raw EOD values under these keyword names, which is a real
interoperability bug against anything (Ekos, AstroKit, Navi) expecting J2000 there.

Sun and Moon positions used for `MOONSEP`/`MOONPHSE`/`ELONGAT` are computed geocentrically too
(not topocentric/parallax-corrected), to stay in the same reference frame as the EOD target —
the Moon's ~1° of topocentric parallax would otherwise leak in as error. `OBJCTALT`/`OBJCTAZ`/
`AIRMASS`/`SUNALT` are the topocentric quantities here, since "altitude/azimuth above the
horizon" is inherently relative to a specific location.
