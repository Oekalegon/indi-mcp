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
`rig_store.py`, `db.py`), `plate_solver.py`/`astrometry_index.py` get their own
env-configurable constants:

| Env var | Default | Purpose |
|---|---|---|
| `INDI_MCP_ASTROMETRY_BIN` | `"solve-field"` (resolved via `PATH`) | Path to the `solve-field` binary. |
| `INDI_MCP_ASTROMETRY_INDEX_DIR` | unset (`solve-field`'s own system-default index search) | Directory `astrometry_index.py` checks/downloads index files into, and `solve()` points `solve-field` at via a generated config (INDIMCP-77 — see below). |
| `INDI_MCP_ASTROMETRY_TIMEOUT_SECONDS` | `60` | Hard timeout for one solve attempt; see below. |
| `INDI_MCP_MAX_UPLOADED_FRAME_BYTES` | `200 MiB` | Upper bound on a client-uploaded frame's decoded size (INDIMCP-76). |

No API key needed for the local-only design.

## Managing index files (INDIMCP-77)

`solve-field` needs local index files installed before it can solve anything — a one-time
setup cost, not something indi-mcp can do without (see [Deployment.md](Deployment.md)).
Checking what's installed and downloading what's missing is `astrometry_index.py`'s job,
exposed as two MCP tools:

- **`list_astrometry_index_files(rig_id=None)`** — every known 4100-series index file
  (`index-4107.fits` through `index-4119.fits`, covering 22 arcmin to 33 degrees of field
  diameter) and whether it's installed under `INDI_MCP_ASTROMETRY_INDEX_DIR`. Pass `rig_id`
  to also get a `neededForRig` flag per entry, computed from that rig's own configured
  optics (`telescope.focalLengthMm` + `camera.pixelSizeMicron`/`pixelsX`/`pixelsY` — the
  same numbers `plate_solve`'s own scale hint already uses) — so "missing but irrelevant to
  this rig" can be told apart from "missing and actually needed."
- **`download_astrometry_index_files(indexNumbers=None, minArcmin=None, maxArcmin=None,
  rig_id=None)`** — downloads whichever aren't already installed. Pass exactly one
  selector: explicit index numbers, an explicit field-of-view range, or a `rig_id` (computes
  the range from its optics, same as `list_astrometry_index_files`'s `neededForRig`).
  Streamed to disk in chunks (never buffered whole in memory — files run up to ~165 MB) to a
  `.part` temp name, renamed only once complete, so an interrupted download is never
  mistaken for a valid index the next time it's checked.

**Deliberately scoped to the 4100-series (Tycho-2) only.** That series is a single file per
scale, hosted directly under `data.astrometry.net/4100/` with a simple, predictable URL —
and it happens to cover the field-of-view range the overwhelming majority of amateur setups
actually need. The narrower-field 5200-series is sharded into many per-healpix files and
hosted on a different server (`portal.nersc.gov`); a setup narrow enough to need it (very
long focal length + small pixels, sub-22-arcmin fields) still works with `plate_solve`, it
just needs those index files installed by some other means for now — a real, known
limitation, not an oversight, and worth revisiting if it turns out to matter in practice.

`solve()` only *uses* whatever ends up in the configured directory: if
`INDI_MCP_ASTROMETRY_INDEX_DIR` is set, it points `solve-field` there via a small generated
`astrometry.cfg` (`astrometry_index.ensure_astrometry_config`) — `solve-field` has no
`--index-dir` flag of its own; index-file location is only configurable through a config
file's `add_path` directives, passed with `--config` (`--dir`, used elsewhere in this same
`solve()` call, is unrelated — that's `solve-field`'s *output* directory, not where it looks
for indices). Left unset (the default), `solve-field` falls back to whatever indices its own
system-default config already knows about, exactly as before this env var existed — fully
backward compatible with an operator who already has indices installed system-wide.

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

## Plate-solving a client-uploaded frame (INDIMCP-76)

Not every frame worth solving was captured by this server — a client may already have a FITS
file from elsewhere (a previous session, another tool, a frame downloaded then re-uploaded for
re-solving) and just wants it solved. This has no rig, no run, and no mount behind it at all,
so it doesn't fit the `plate_solve` step (which is fundamentally about a step in a script
running against a resolved rig) — it's a plain, standalone MCP tool instead:
`plate_solve_uploaded_frame(fitsDataBase64, ...)` in `server.py`.

The save/solve/write-back sequence is identical to what `_execute_plate_solve` does once it
has a frame in hand, so that sequence is factored into `plate_solver.py` itself
(`write_wcs_headers`, `solve_uploaded_frame`) rather than duplicated — both the script step
and the upload tool call the same shared functions. `plate_solver.py` doing this (rather than,
say, `frame_store.py`) keeps the "given a frame and a solve result, write the WCS back" logic
next to the `PlateSolveResult` shape it operates on.

Differences from the `plate_solve` step, driven entirely by there being no rig/mount:

- **No automatic position/scale hint.** `plate_solve` reads both from the rig's configured
  optics and the mount's live coordinates; an uploaded frame has neither, so
  `plate_solve_uploaded_frame` accepts `raHintHours`/`decHintDeg`/`scaleLowArcsecPerPixel`/
  `scaleHighArcsecPerPixel` directly as tool parameters instead — the caller supplies
  whatever it happens to know, or omits them for an unhinted (slower) solve.
- **No mount sync** — there's nothing to sync.
- **No script-engine error vocabulary.** This never runs as a script, so a failure is a plain
  `ValueError` (the ordinary way a `FastMCP` tool call signals failure), not
  `ScriptExecutionError`/`scriptFailed`.
- **The frame is kept even if the solve fails.** Saved (`device="uploaded"`, `run_id=None`)
  before solving, not after, so a failed solve doesn't lose what was uploaded — the caller
  can still retrieve it (without WCS headers) via `list_frames`/`frame://{frameId}`, matching
  `frame_store`'s existing "ad hoc frame, no run" convention for a capture outside any script
  run.
- **Base64 in, not a resource.** MCP tool-call arguments are JSON; there's no binary parameter
  type, so the FITS bytes travel as a base64 string — the mirror image of `frame://{frameId}`
  already returning frame bytes as a base64 blob resource content in the download direction.

## Retrying toward a tolerance (INDIMCP-47)

`toleranceArcsec`/`maxAttempts` land on `PlateSolveStep` itself, per the design above — the
retry loop lives in `_execute_plate_solve`'s own handler, not in YAML. The one thing this
design left unresolved when INDIMCP-45 shipped was *what* "the mount's target coordinate" (the
thing angular separation is measured against) actually is, and *how* a retry can possibly
converge at all if nothing physically moves between attempts. Both are resolved the same way:

- **The target is `TARGET_EOD_COORD`** — the standard INDI property recording the last
  coordinate a `slew` (or any other `EQUATORIAL_EOD_COORD` command) commanded the mount to go
  to, distinct from `EQUATORIAL_EOD_COORD` itself (where the mount currently reports actually
  being). Already a live property on any real mount driver — no new state, no new schema
  mechanism, consistent with every other hint this step already reads directly off the mount.
  Read once, at the start of the loop, and reused for every attempt (nothing in the loop other
  than a `slew` step changes it, and a `slew` step never runs mid-loop).
- **Convergence comes from re-slewing between attempts, not from sync alone.** A sync
  recalibrates the mount's *internal* pointing model — it doesn't move the telescope. Solving
  the same static pointing over and over would report the same separation forever. So each
  retry (attempt 2 onward): re-slews to `TARGET_EOD_COORD` using the model the *previous*
  attempt's sync just corrected (mirrors `slew` exactly — `_check_not_parked`,
  `_ensure_track_on_slew`, then the coordinate command and its `Busy`→`Ok` wait) — landing
  closer than the previous, uncorrected attempt did — then captures and solves again. This is
  the actual mechanism that makes the loop converge at all.
- **The separation comparison happens in one consistent frame.** `TARGET_EOD_COORD` is
  epoch-of-date; a solve result is J2000. `_sync_mount_to_solved_position` can send EOD
  directly and ignore the EOD/J2000 offset, because a sync only needs to be coarse
  (arcmin-level) accurate. This loop's `toleranceArcsec` is typically much tighter, so the
  target is converted to J2000 first (`fits_headers.compute_target_position`, the same
  EOD→ICRS transform already used for `capture_frame`'s `OBJCTRA`/`OBJCTDEC`) before comparing
  with `astropy.coordinates.SkyCoord.separation()`.
- **A failed solve doesn't abort immediately** (unlike single-attempt mode) — it consumes an
  attempt and retries, since the next attempt's re-slew might put a star field back in frame
  that the previous attempt's pointing missed entirely.
- **Every attempt that solves gets its WCS written**, regardless of whether that specific
  attempt happens to meet tolerance — each attempt is a distinct, real, persisted frame, and
  deserves a correct header regardless of what the *loop* ultimately decides.
- **`toleranceArcsec` requires `syncMount=true` and `exposureSeconds`** — without syncing the
  model never improves (nothing to converge), and without a fresh capture every attempt would
  just re-solve the same stale frame. Enforced by `PlateSolveStep`'s own validator for a
  literal misconfiguration, and again in the engine handler for a parameterized one (a
  `"{{ param }}"` reference's value isn't known until execution).

`plate_solve_until_precision` (`scripts/plate_solve_until_precision.yaml` + a typed
`@mcp.tool()` wrapper, matching the INDIMCP-49 pattern) is a thin one-step wrapper around
`plate_solve` with `toleranceArcsec`/`maxAttempts` filled in — kept as a separate script from
`plate_solve.yaml` (which stays single-attempt-only) rather than folding tolerance support into
the base wrapper's own parameters, for the clearest separation between "solve once" and "solve
to a tolerance" as two distinct, independently discoverable tools.

## Open items resolved during implementation

- Exact `frame_store` query for "most recent frame for run_id + device": `frame_store.list_frames`
  already returns most-recently-captured-first, so no new query was needed.
- `mountRole` is always explicit, never defaulted to "the rig's only mount" — see
  `PlateSolveStep`'s own docstring for why.
- `scripts/plate_solve.yaml` and `scripts/plate_solve_until_precision.yaml`, plus typed
  `@mcp.tool()` wrappers and wrapper/script parameter-parity test coverage (`89f7271`'s
  pattern), all shipped.
- `docs/ScriptSchema.md` updated with the step's full reference table.
