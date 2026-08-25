# INDI MCP Server — Features for version 1.0

This document lists, per main function of the server, the features that should be
supported in version 1.0. Checked items (`[x]`) already exist on `develop`; unchecked
items (`[ ]`) still need to be built — where an `INDIMCP-nn` id is given, the item comes
from the INDIMCP todo project. A 📖 marker links to the *user documentation* covering
that feature — **items without a 📖 have no user documentation yet**. Design documents
([Design.md](Design.md), [PlateSolve.md](PlateSolve.md),
[SensorCalibration.md](SensorCalibration.md)) are developer-facing and do not count as
user documentation; the user-facing set is [Deployment.md](Deployment.md), the schema
references ([RigSchema.md](RigSchema.md), [ObservatorySchema.md](ObservatorySchema.md),
[ScriptSchema.md](ScriptSchema.md), [MessageSchema.md](MessageSchema.md)),
[FitsHeaders.md](FitsHeaders.md), and [SensorAnalysis.md](SensorAnalysis.md). Each
section ends with a **Candidates** list: ideas that have come up but are *not yet
decided* for 1.0 (no todo exists yet) — promote them into the feature list (or delete
them) as scope decisions are made.

## Server & infrastructure

- [x] MCP server over `stdio` and `streamable-http` transports (`streamable-http` is the
      production transport for the Pi systemd deployment) — 📖 [Deployment.md](Deployment.md)
- [x] `get_server_info` (version/build info) — 📖 [Deployment.md](Deployment.md)
- [x] SQLite-backed event log with `get_events` query tool
- [x] Event streams as MCP resources (INDI messages, script runs)
- [x] Plain-HTTP frame download endpoint (`GET /frames/{frameId}`) that streams from disk

**Candidates (not yet decided):**
- Authentication / hardening of the HTTP surface (currently unauthenticated by design,
  see Hardening notes in [Deployment.md](Deployment.md))

## INDI server & driver management

- [x] Start / stop / restart the INDI server, query its status
- [x] List the driver catalog installed on the machine
- [x] Start / stop individual drivers, list running drivers

- [ ] Auto-start the INDI server and a rig's drivers as one operation (INDIMCP-108)

*Only a high-level overview in the [README](../README.md) — the individual tools have
no user documentation yet.*

## INDI messaging & raw property access

- [x] Start / stop the INDI messaging client, query its status
- [x] List received INDI messages (optionally per device) —
      📖 [MessageSchema.md](MessageSchema.md)
- [x] Send a raw INDI property (`send_indi_property`) as an escape hatch —
      📖 [MessageSchema.md](MessageSchema.md)
- [x] Get all properties of a device (`get_device_properties`) —
      📖 [MessageSchema.md](MessageSchema.md)
- [x] Message event-stream resources (`indi://messages`, `indi://messages/{device}`) —
      📖 [MessageSchema.md](MessageSchema.md) (payload format only)
- [x] `connectionMade`/`connectionLost` events on the `indi://messages` stream
      (INDIMCP-57)

## Rig & observatory configuration

- [x] Rig store: list / get / save rigs (YAML in `rigs/`) —
      📖 [RigSchema.md](RigSchema.md)
- [x] `suggest_rig` and `draft_rig` — assist rig creation from connected devices
- [x] `check_rig` — verify a rig's devices are present
- [x] Connect / disconnect a rig device by role
- [x] Filter-name synchronisation between rig and driver (`sync_filter_names`,
      `adopt_filter_names_from_driver`) — 📖 [RigSchema.md](RigSchema.md)
- [x] Observatory store: list / get / save / draft observatories —
      📖 [ObservatorySchema.md](ObservatorySchema.md)

## Camera

- [x] Capture a single frame (`capture_frame`) with frame type (Light / Dark / Bias /
      Flat), exposure, binning, gain, offset, and optional sub-frame (ROI) —
      📖 [ScriptSchema.md](ScriptSchema.md#capture_frame),
      [FitsHeaders.md](FitsHeaders.md)
- [x] Capture sequences of frames via built-in scripts: `capture_light_sequence`,
      `capture_dark_sequence`, `capture_bias_sequence`, `capture_flat_sequence`
- [x] Abort a running exposure
- [x] Cooling: `cool_camera` (to target temperature), `cooler_on`, `cooler_off` —
      📖 [ScriptSchema.md](ScriptSchema.md)
- [x] Sensor analysis: `run_sensor_calibration_sweep` over gain/offset combinations
      (with status / cancel tools), plus the `capture_sensor_calibration_set` script —
      📖 [SensorAnalysis.md](SensorAnalysis.md)
- [x] Flat calibration sweep (`run_flat_calibration_sweep` with status / cancel) —
      📖 [SensorAnalysis.md](SensorAnalysis.md) (theory/usage only)
- [x] FITS header enrichment with celestial context and observatory location —
      📖 [FitsHeaders.md](FitsHeaders.md)
- [x] Abort the exposure on `capture_frame` timeout, not just on cancellation
      (INDIMCP-92)
- [ ] Auto-exposure determination for flat frame capture (INDIMCP-105)
- [ ] Support INDI flat-panel devices (auto on/off) in the flat calibration sweep
      (INDIMCP-106)
- [ ] Automatic refocus step in `capture_light_sequence` instead of a fixed
      `focusPosition` (INDIMCP-72, depends on autofocus below)
- [ ] Simbad object cross-ID lookup in FITS headers (INDIMCP-68)
- [ ] Star-detection quality FITS headers (`NSTARS`/`SATSTARS`/`MEDFWHM`/`MEDECC`/
      `BACKNOIS`) (INDIMCP-70)

**Candidates (not yet decided):**
- Warm-up (controlled cool-down of the cooler before power-off)
- Exposure progress reporting during long exposures

## Filter wheel

- [x] Select a filter by name (`select_filter`) —
      📖 [ScriptSchema.md](ScriptSchema.md), [RigSchema.md](RigSchema.md)
- [ ] Support manual filter trays and wheels (INDIMCP-74)

## Focuser

- [x] Set an absolute focus position (`set_focus_position`) —
      📖 [ScriptSchema.md](ScriptSchema.md)
- [ ] Autofocus built-in script: FWHM sweep across focus positions, select best, set
      focuser (INDIMCP-43)
- [ ] Store last-known-good focus position per filter in the rig schema/store
      (INDIMCP-66)
- [ ] Write focuser min/max travel limits back to the INDI driver (`FOCUS_MAX`)
      (INDIMCP-75)

**Candidates (not yet decided):**
- Temperature-compensated focusing

## Mount

- [x] Park / unpark
- [x] Slew to RA/Dec coordinates — 📖 [ScriptSchema.md](ScriptSchema.md)
- [x] Tracking control: `track_off`, `set_track_mode`, `set_custom_tracking_rate`
- [x] Precise pointing via iterative solve-and-refine (`plate_solve_rig` script's
      `toleranceArcsec`, run via `run_script`/`manage_script_run`)
- [ ] Guiding: design how to connect the PHD2 guiding API to the MCP server
      (INDIMCP-26) — 📖 [Guiding.md](Guiding.md) (design done; implementation still open)
- [ ] Meridian-flip handling (design first) (INDIMCP-33)
- [ ] Nudge moves: move the mount a little in a direction (e.g. East) at a slew rate,
      rather than to a position (INDIMCP-55)
- [ ] Polar-alignment script: measure offset from the pole and report the required
      azimuth/elevation adjustment (INDIMCP-78)
- [ ] Simulate slew paths and reroute around (or reject) below-horizon dips
      (INDIMCP-39)
- [ ] Continuous horizon safety watchdog that aborts mount motion below the horizon
      (INDIMCP-40)

**Candidates (not yet decided):**
- Slew to a named object (catalog lookup)

## Plate solving & astrometry

- [x] Capture-and-solve on the rig's camera (`plate_solve_rig` script, run via
      `run_script`/`manage_script_run`)
- [x] Solve an uploaded frame (`plate_solve_uploaded_frame`)
- [x] Iterative solving until a pointing precision is reached
      (`plate_solve_rig`'s `toleranceArcsec`)
- [x] Astrometry index file management: list and download index files
      (`manage_astrometry_index`)

*Covered by [PlateSolve.md](PlateSolve.md), but that is a design document — no user
documentation yet.*

## Frame storage & transfer

- [x] Frame store with metadata
- [x] List frames with filters (`list_frames`), get metadata per frame
- [x] Streaming HTTP download of frame bytes
- [x] Transfer lifecycle: `confirm_frame_transfer`, `delete_frame` (guarded by
      transferred-state by default), `purge_transferred_frames`
- [x] SHA-256 checksum per captured frame so clients can verify transfers (INDIMCP-95)
- [x] Warn on null checksum for frames created before checksum support (INDIMCP-107)
- [x] Rework `downloadUrl`'s use of `socket.gethostname()` — the client already knows
      a working host (INDIMCP-96)

**Candidates (not yet decided):**
- Disk-space monitoring / low-space warnings on the Pi

## Session planning & scheduling

- [ ] Object-visibility check: is a target above the horizon over a given timespan
      (astropy-based) (INDIMCP-29)
- [ ] Scheduling for imaging sessions with multiple targets, e.g. mosaics
      (design first) (INDIMCP-32)

**Candidates (not yet decided):**
- Full imaging-session script (target list, filter sequences, meridian flip,
  autofocus interleaving) — depends on autofocus, meridian-flip handling, and
  guiding above

## Scripting

- [x] YAML script store: list / get / save scripts —
      📖 [ScriptSchema.md](ScriptSchema.md)
- [x] Run a script against a rig (`run_script`), with per-run status, cancel,
      pause and resume — 📖 [MessageSchema.md](MessageSchema.md) (status envelope only)
- [x] Script composition: scripts calling scripts, `repeat` and `if` steps —
      📖 [ScriptSchema.md](ScriptSchema.md)
- [x] Script run event-stream resources (`indi://scripts`, `indi://scripts/{runId}`)
- [x] Built-in script library covering all device operations above (`scripts/`) —
      📖 [ScriptSchema.md](ScriptSchema.md)
- [x] User scripts directory (`user_scripts/`)

## User-documentation gaps

Areas above that ship in 1.0 but currently have no user documentation (only design
docs or nothing at all):

- [ ] Getting started / overall user guide (what a first session looks like:
      connect, configure a rig, capture)
- [ ] INDI server & driver management tools
- [ ] Event streams and the event log (how a client subscribes and queries)
- [ ] Capture sequence scripts (light/dark/bias/flat) and their parameters
- [ ] Plate solving & astrometry usage (user-facing counterpart to PlateSolve.md)
- [ ] Frame retrieval & transfer workflow (list, download, confirm, delete)
- [ ] Rig creation assistance (`suggest_rig`, `draft_rig`, `check_rig`)
