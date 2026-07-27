# Plate-solving design (INDIMCP-27)

How astrometry.net gets wired into indi-mcp: solver choice, config, the new `plate_solve`
script step, mount-sync behaviour, and how the result feeds `capture_frame`'s FITS headers
(INDIMCP-69). This document is the design; INDIMCP-45 (step + built-in `plate_solve` script),
INDIMCP-47 (`plate_solve_until_precision`), and INDIMCP-69 (WCS FITS headers) are the
implementation work that follows from it.

## Solver backend: local `solve-field`, not the web API

**Decision: shell out to astrometry.net's local `solve-field` CLI, with local index files.**
Not the `nova.astrometry.net` web API.

This server's deployment target is a Pi running headless via systemd
([Deployment.md](Deployment.md)) — a plate-solve step that depends on outbound internet and a
third-party service's availability/rate limits isn't acceptable for something a capture
sequence blocks on (`plate_solve_until_precision` sits in the middle of an unattended imaging
run). `solve-field` also returns a result in a couple of seconds on a Pi 4/5 once given a
tight scale/position hint (see below), whereas the web API's queue/upload/poll round-trip is
seconds-to-minutes and unbounded. Index file storage (a few GB for a useful magnitude/field-size
range) is a one-time setup cost on the Pi's SD card/SSD, not a per-run one.

The design still isolates the backend behind a small `plate_solver.py` module with a single
`solve(...)` async function, so a web-API backend could be added later without touching
`script_engine.py` or the schema — but that's explicitly out of scope for INDIMCP-27/45.

## New dependency

None. `solve-field` is invoked as a subprocess (`indi-mcp` doesn't link against
astrometry.net's C library); no new Python package is needed. `solve-field` itself and its
index files are a deployment-time prerequisite, documented in
[Deployment.md](Deployment.md) alongside the existing `indiserver`/driver package
prerequisites (out of scope for this doc, but noted so INDIMCP-45 doesn't drop it).

## Config

Following the existing per-module `<NAME>_ENV` + default convention (`frame_store.py`,
`rig_store.py`, `db.py`), a new `plate_solver.py` gets its own env-configurable constants:

| Env var | Default | Purpose |
|---|---|---|
| `INDI_MCP_ASTROMETRY_BIN` | `"solve-field"` (resolved via `PATH`) | Path to the `solve-field` binary. |
| `INDI_MCP_ASTROMETRY_INDEX_DIR` | unset (solve-field's own default, `/usr/share/astrometry`) | `--dir`/`--config`-equivalent override for index-file location, for non-standard installs. |
| `INDI_MCP_ASTROMETRY_TIMEOUT_SECONDS` | `60` | Hard timeout for one solve attempt; see below. |

No API key needed for the local-only design.

## Subprocess invocation

No existing async-subprocess pattern in this codebase (`indi_server.py`'s `indiserver`
management is sync `subprocess.call` + a background thread for a long-running daemon — not
applicable to a short-lived command whose exit code and stdout matter). `plate_solver.solve()`
uses `asyncio.create_subprocess_exec` + `asyncio.wait_for`, not `asyncio.to_thread` +
`subprocess.run`: unlike `frame_store.save_frame`'s brief blocking disk write, a solve attempt
can legitimately run the full `INDI_MCP_ASTROMETRY_TIMEOUT_SECONDS` (a hard, wide-field, or
low-star-count frame is slow), and other scripts/messaging must keep progressing on the same
event loop while it does — `asyncio.to_thread` would tie up a thread but still let a *hung*
process outlive the script run with no way to `kill()` it from `_check_cancelled`. Real
cancellation matters here specifically because this step can run inside a
`plate_solve_until_precision` loop mid-sequence; a user cancelling the run should kill an
in-flight `solve-field` process immediately, not wait out its timeout.

```python
async def solve(fits_path: Path, *, ra_hint=None, dec_hint=None, radius_deg=None,
                 scale_low_arcsec=None, scale_high_arcsec=None,
                 timeout_seconds=DEFAULT_TIMEOUT_SECONDS) -> PlateSolveResult | None:
    args = [_solve_field_bin(), "--overwrite", "--no-plots", "--new-fits", "none", str(fits_path)]
    if ra_hint is not None and dec_hint is not None and radius_deg is not None:
        args += ["--ra", str(ra_hint), "--dec", str(dec_hint), "--radius", str(radius_deg)]
    if scale_low_arcsec is not None and scale_high_arcsec is not None:
        args += ["--scale-units", "arcsecperpix",
                  "--scale-low", str(scale_low_arcsec), "--scale-high", str(scale_high_arcsec)]
    proc = await asyncio.create_subprocess_exec(*args, stdout=..., stderr=...)
    try:
        await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None  # timed out / no solve — not an exception, see below
    if proc.returncode != 0:
        return None
    return _parse_wcs(fits_path.with_suffix(".wcs"))
```

An unsolved field is expected, ordinary output (bad focus, clouds, too few stars, wrong scale
hint) — `solve()` returns `None` rather than raising, and the `plate_solve` step handler
decides what that means for its caller (see retry loop below), the same way
`_wait_for_property_state` distinguishes a real fault (raise) from "still working" (keep
polling) rather than treating every non-success as fatal.

**Scale and position hints are not optional extras — they're what makes this fast enough to
sit inside an imaging sequence.** A blind solve (no hints) over the full index range can take
tens of seconds to minutes on a Pi; a solve narrowed to the actual pixel scale and a small
search radius around the mount's current believed position typically resolves in 1-3 seconds.
Both hints are already computable from data this server has on hand:

- **Scale**: `rig.telescope.focalLengthMm` + `rig.camera.pixelSizeMicron` already feed the
  `SCALE` FITS header (`script_engine._add_telescope_optics_fields`,
  `scale = 206.265 * pixelSizeMicron / focalLengthMm`, arcsec/px) — the same value
  `solve-field --scale-low/--scale-high` wants, given a generous +/-10% band to tolerate
  binning and lens/optics imprecision.
- **Position**: the mount's live `EQUATORIAL_EOD_COORD` (`RA`/`DEC`), the same property
  `slew` and `_add_mount_derived_fields` already read, gives `--ra`/`--dec`; a
  fixed-for-now `--radius` (a few degrees, config-worthy but not blocking INDIMCP-45) covers
  ordinary mount-model/polar-alignment error.

If either hint is unavailable (no rig optics configured, mount reports no valid coordinate
yet), the step falls back to an unhinted (slower, still valid) solve rather than failing —
consistent with every other "best-effort, skip if not configured" property in this codebase.

## Schema: the `plate_solve` step

An **engine-implemented primitive** (`docs/ScriptSchema.md`'s second tier), for the same
reason `cool_camera`/`capture_frame` are: it bundles a multi-step sequence (capture-or-reuse a
frame → solve → optionally sync the mount → optionally retry) that isn't reducible to
`set_property`/`wait_for`, and — critically — a real numeric comparison (angular separation
between solved and target position) that the schema's `Condition` cannot express (per
`docs/ScriptSchema.md`'s own note flagging this as the open question INDIMCP-27 needs to
resolve).

```python
class PlateSolveStep(_StepBase):
    step: Literal["plate_solve"]
    role: str                                  # camera role to capture from (or reuse last frame of)
    mountRole: str | None = None                # mount role to sync/compare against; default: the rig's only mount
    exposureSeconds: float | int | str | None = None  # capture a fresh frame if set; else reuse the run's last frame for `role`
    syncMount: bool = True                       # after a successful solve, sync EQUATORIAL_EOD_COORD to the solved position
    toleranceArcsec: float | int | str | None = None  # if set, retry (re-solve after syncing) until within this separation of the mount's target, or maxAttempts is hit
    maxAttempts: int = 3
    timeoutSeconds: float | int | str = 60
```

**Design decision: the tolerance/retry loop lives inside this one step, not in YAML via
`repeat`/`until`.** This is the resolution to the `Condition`-can't-check-a-computed-value gap
called out in `docs/ScriptSchema.md`. It mirrors `cool_camera`, which already owns its own
internal wait-for-stabilization loop rather than exposing "keep checking `CCD_TEMPERATURE`" to
the script author — the engine-implemented tier exists precisely so a script author declares
*what* (`toleranceArcsec: 5`), never *how* (poll interval, retry count, when to give up).
Consequences:

- **INDIMCP-47's `plate_solve_until_precision` is not a `repeat`/`until` composition** — it's
  a thin one-step built-in script (`scripts/plate_solve_until_precision.yaml`, matching the
  `docs/Design.md` M101 example's call shape) that's just `plate_solve` with
  `toleranceArcsec`/`maxAttempts` filled in, plus (per the INDIMCP-49 wrapper pattern already
  established for `cool_camera`/`slew`/etc.) a typed `@mcp.tool()` wrapper in `server.py`.
  Whether a separate script is even worth keeping alongside the wrapper tool, or the wrapper
  should call `plate_solve` directly, is an INDIMCP-47 implementation detail, not blocked by
  this design.
- No new generic schema mechanism (variables, computed conditions, expression language) is
  needed — consistent with `docs/ScriptSchema.md`'s explicit no-embedded-expression-language
  rule.
- The tradeoff: a script author can't insert an arbitrary step *between* solve attempts
  (e.g. "nudge focus between retries"). Not a real use case for this primitive — a failed
  solve within one `plate_solve` call means "still pointed wrong" or "no stars found," not
  "something else needs to change first" — and if that changes later, it's a new step
  parameter, not a reason to move the loop into YAML.

## Handler behaviour (`_execute_plate_solve`, INDIMCP-45)

1. Resolve `role` → camera device, `mountRole` → mount device (`_resolve_device`, same as
   every other step).
2. Obtain a frame: if `exposureSeconds` is set, run the same capture sequence
   `_execute_capture_frame` already implements (factor its body so both call a shared
   `_capture_one_frame` helper, rather than duplicating it); otherwise, look up the most
   recently captured frame for this `run_id`/device via `frame_store` (`frame_store.py`
   currently has no "most recent for run+device" query — a small addition, filtering the
   existing `frames` table). No frame found is a `ScriptExecutionError` — a `plate_solve` step
   with no `exposureSeconds` and no prior `capture_frame` in the same run has nothing to solve.
3. Compute scale hint from the rig's `telescope`/`camera` config (if present) and position
   hint from the mount's live `EQUATORIAL_EOD_COORD` (if valid) — both best-effort, per above.
4. Loop up to `maxAttempts` times (1, if `toleranceArcsec` is unset):
   a. Call `plate_solver.solve(...)`. `None` → continue to next attempt (or exhaust and raise
      `ScriptExecutionError` on the last one — a failed solve is a real failure the script run
      should stop on, not a silent no-op, since everything downstream — sync, FITS headers,
      a caller relying on `plate_solve_until_precision` actually having solved — depends on it).
   b. If `syncMount`, send `ON_COORD_SET={SYNC: On}` then `EQUATORIAL_EOD_COORD` with the
      *solved* RA/Dec, and wait for its `Busy`→`Ok` transition (`_wait_for_property_state`,
      same primitive `slew` uses) — this is what makes a subsequent `slew` in the same
      sequence land accurately, and what INDIMCP-47's retry loop is actually correcting
      *against* (each retry solves the mount's new position after the sync, not the same
      stale frame).
   c. If `toleranceArcsec` is set, compute the angular separation between the solved position
      and the mount's target coordinate using `astropy.coordinates.SkyCoord.separation()` —
      the same call `fits_headers.py` already uses for Sun/Moon elongation
      (`target.separation(sun)`) — rather than hand-rolling a haversine, and stop retrying
      once it's within tolerance.
5. Attach the solved WCS (RA/Dec of center, `CD`/rotation, pixel scale) directly onto the frame
   just solved via `fits_headers.write_fits_headers` (INDIMCP-69's `CRVAL`/`CTYPE`/`CRPIX`/
   `CDELT`/`CROTA`/`SECPIX`/`RADECSYS`/`EQUINOX` keywords) — **done here, in the same handler,
   not as separate INDIMCP-69 work bolted on afterward.** The frame is already open/just
   written by this same step (or by the `capture_frame` this step ran internally); re-opening
   it from a different code path later to inject WCS would be strictly more complex than doing
   it inline, and INDIMCP-69's comment ("can be implemented on top of it") reads naturally as
   "once this step exists to produce a WCS result," not "as a structurally separate step."
   Best-effort, same as every other FITS enrichment: a `write_fits_headers` failure is logged,
   not fatal to the script run.
6. Report a `ScriptStatusMessage` (the `capture_frame` precedent) with the solved RA/Dec,
   attempt count, and final separation if `toleranceArcsec` was set — this is the only place
   the result becomes visible to a client, consistent with the "no step returns a value the
   YAML layer can consume" constraint above; a client wanting the numeric result reads it off
   `indi://scripts`, or off the frame's own FITS headers/`frame://{frameId}` resource once
   written.

## Open items for INDIMCP-45/47/69 to resolve during implementation

- Exact `frame_store` query for "most recent frame for run_id + device" (new, small).
- Whether `mountRole` truly defaults to "the rig's only mount" or must always be explicit —
  check how `slew`/other mount-touching steps already resolve this today before adding a new
  convention.
- `scripts/plate_solve.yaml` and `scripts/plate_solve_until_precision.yaml` built-in script
  definitions + typed `@mcp.tool()` wrappers (INDIMCP-49 pattern), and the wrapper/script
  parameter-parity test (`89f7271`) needs to cover both.
- `docs/ScriptSchema.md` and `docs/Deployment.md` updates once INDIMCP-45 lands (new step
  reference table entry; `solve-field` + index files as a deployment prerequisite).
