# Sensor calibration analysis: bias/dark/flat sweeps (INDIMCP-81/101)

Design for restructuring sensor calibration capture around a photon-transfer-curve (PTC)
analysis: why flat frames are needed at all, why they can't stay bundled with bias/dark the way
`capture_sensor_calibration_set` (INDIMCP-81) currently does, and why sweeping gain/offset/
exposure needs a new MCP tool rather than a new script step. This is the design; INDIMCP-102
(bias/flat-dark script + sweep function) and INDIMCP-103 (flat script + sweep function) are the
implementation work that follows from it, and [SensorAnalysis.md](SensorAnalysis.md)
(INDIMCP-104) is the companion theory document — what the PTC analysis measures, the math
behind each quantity, and how the resulting sensor profile is used.

## Background: why flats are captured at all

A PTC characterizes a sensor's read noise and gain (e⁻/ADU) at a given gain/offset setting.
Bias and dark frames alone can't produce this:

* **Bias** (zero exposure, no light) gives the read-noise floor.
* **Dark** (no light, real exposure) gives dark current, once compared against bias or fit
  across multiple exposure lengths.
* **Flat** (uniform illumination, known exposure) is the one frame type that contains **photon
  shot noise**, which scales with signal in a way that lets a PTC solve for sensor gain. Without
  an illuminated frame there is nothing to measure that dependency against — dark current alone
  is far too small a signal range over typical calibration exposures to fit a usable slope.

The noise that matters is extracted by differencing a *pair* of flats at the same exposure: any
spatial pattern imprinted by the optical train (vignetting, dust, pixel-response
non-uniformity) is identical in both frames and cancels out in the difference, leaving only
random shot + read noise. This is why a flat's absolute spatial uniformity — the thing that
depends on the rest of the imaging train — doesn't matter for sensor characterization, as long
as pairs/sequences are captured back-to-back at a fixed exposure and gain/offset. A matching
**flat-dark** (same exposure as the flat, no light) is captured alongside it to subtract out the
dark-current/read-noise contribution before the shot-noise term is isolated.

## Decision: split capture into a bias/flat-dark script and a separate flat script

**Decision:** trim `capture_sensor_calibration_set` down to bias + flat-dark only (both need no
external light source and can run fully unattended), and keep flats on the already-existing
[`capture_flat_sequence.yaml`](../scripts/capture_flat_sequence.yaml) — extended if needed —
rather than one bundled script.

Capturing a flat requires a flat panel (or equivalent light source) physically placed in front
of the optics — an action outside the script engine's control (see [Design.md § INDI scripting
layer](Design.md#architecture-overview): scripts only ever issue INDI commands and wait on
INDI property state, they have no way to prompt for or verify a physical setup step). Bundling
flats into the same script as bias/dark (as `capture_sensor_calibration_set` currently does)
means a fully-unattended script silently stalls or produces garbage flat data mid-run if the
operator hasn't staged the panel — the script has no way to detect this. Splitting the two:

* Lets the bias + flat-dark portion run completely unattended, any time, with no manual
  precondition.
* Makes the flat-panel requirement an explicit, separate invocation the caller only reaches once
  they've actually staged the light source — matching `capture_flat_sequence`'s existing design,
  which already documents (see its own `description`) that it "deliberately skips mount
  positioning and camera cooling" and only handles filter/focus, leaving illumination to the
  caller.

This is INDIMCP-102 (bias/flat-dark side) and INDIMCP-103 (flat side).

## Why the sweep can't be expressed as script `parameters`

A PTC needs several flat exposure levels at each gain/offset setting (multiple points to fit a
line through, not just one), and a full sensor characterization needs bias+dark+flat-dark
+flat at *each* gain/offset combination in a sweep. `capture_sensor_calibration_set`'s own
description already states sweeping gain/offset is the caller's job — "invoking this script once
per setting" — because the script schema deliberately has no loop-index arithmetic or computed
iteration (see [ScriptSchema.md § Execution model](ScriptSchema.md#execution-model-generic-vs-engine-implemented-primitives),
line ~179: "a script that needs to loop on a computed result needs that computation exposed as
its own engine-implemented step first").

The `plate_solve` step (INDIMCP-27/45/47, see [PlateSolve.md](PlateSolve.md)) is the existing
precedent for exactly that pattern — a retry loop moved into the step's own Python handler
(`_execute_plate_solve` in `script_engine.py`) instead of a schema-level `repeat`/`until`. It
does **not**, however, solve this case, and it's worth being explicit about why:

* `plate_solve`'s own step fields (`toleranceArcsec`, `maxAttempts`, ...) are still plain
  scalars — the *loop* is computed server-side, but nothing about the step's *input shape*
  needed to change.
* A calibration sweep needs the caller to hand in **lists** — `gains: [50, 100, 150]`,
  `offsets: [10, 30]`, a range of flat exposure times — and the schema's `Parameter.type` is a
  closed, deliberately small vocabulary: `"string"`, `"integer"`, `"number"`, `"boolean"`
  ([ScriptSchema.md § Parameter fields](ScriptSchema.md#parameter-fields): "no nested
  objects/arrays as parameter types, keeping validation simple and the substitution mechanism
  below unambiguous"). That restriction applies to every script parameter, at every level —
  `run_script`'s own `parameters` argument is validated against the exact same `Parameter`
  schema of whatever it calls, all the way up to the top-level MCP `run_script` tool. There is
  no way, today, for a caller to hand a script a list of values through the normal parameter
  mechanism, regardless of where the looping happens.

So a new `capture_sensor_calibration_sweep` **step type** could technically be given a
Python-level `gains: list[float]` field directly on its own pydantic model (nothing stops an
individual step's own fields from using a richer shape than the generic `Parameter` vocabulary —
`SlewTarget`, `Condition` and `PlateSolveStep`'s fields already aren't restricted to
string/integer/number/boolean). But doing that would only let the *list itself* be hardcoded
into a YAML file — it still couldn't be supplied at call time by whatever is driving a sweep
(an agent, a UI, a script run from the Client Computer), since there'd be no way to pass a list
into that step from a `run_script` call. That defeats the actual point of a sweep tool, which is
letting the caller choose the gain/offset/exposure range per invocation.

## Decision: a dedicated MCP tool, not a script step

**Decision:** implement the sweep as a plain MCP tool (like `list_frames` or
`purge_transferred_frames` — see [server.py](../src/indi_mcp/server.py)), not as a new script
step or a new script. Implemented for the bias/flat-dark side as
`run_sensor_calibration_sweep` (INDIMCP-102; `server.py` + `sensor_calibration_sweep.py`):

```python
@mcp.tool()
async def run_sensor_calibration_sweep(
    rig_id: str,
    gains: list[float],
    offsets: list[float],
    flatExposureSecondsList: list[float],
    biasCount: int,
    darkCount: int,
    biasExposureSeconds: float = 0.0,
    location_id: str | None = None,
) -> SensorCalibrationSweepStarted:
    ...
```

Paired with `get_sensor_calibration_sweep_status(sweep_id)` and
`cancel_sensor_calibration_sweep(sweep_id)`, mirroring `run_script`/`get_script_status`/
`cancel_script`'s own three-tool shape. The flat side (INDIMCP-103) gets its own equivalent
tool once the flat script split and panel-staging design below are settled — two tools, not one
with a branch, per the "Open items" resolution below.

MCP tool parameters are ordinary typed Python/pydantic inputs, not bound by
`ScriptSchema.md`'s closed step vocabulary — that vocabulary exists specifically to keep
*uploaded YAML* inert declarative data (see [Design.md § INDI scripting
layer](Design.md#architecture-overview): "a script is then just declarative data, not
executable code, it can safely be authored on the controlling computer and uploaded to the MCP
server to run"). A server-side tool's own implementation was never part of that constraint —
`configuration`'s `action="save"` validation, cycle detection, etc. are exactly the safety mechanism that only
applies to *script* content; a fixed, non-uploadable Python function driving `list[float]`
inputs doesn't touch it at all.

**Underlying mechanism:** the tool loops over the gain/offset (× exposure, for flats)
combinations and calls `script_runs.start_script(...)` once per combination against the already
existing (post-split) bias/flat-dark script or the flat script — reusing `execute_script`
exactly as a human/agent driving `run_script` in a loop would today, just moved server-side.
Each individual `start_script` call still returns its own `runId` and reports through the
existing `scriptStarted`/`scriptProgress`/`scriptCompleted` event envelope
([Design.md § Calling scripts and script results](Design.md#calling-scripts-and-script-results));
the sweep tool's own job is purely sequencing those calls (awaiting each run's terminal state
before starting the next — `start_script` itself returns immediately) and aggregating an overall
status, not reimplementing capture logic.

**Resolved (INDIMCP-102): `sweepId`, not a bare `runId` list — and not just for a nicer client
API.** It turns out the bare-list option was never actually viable: `run_script` never blocks
its caller, and a full sweep can run far longer than any single script, so the sweep tool can't
block either — but unlike a caller looping `run_script` itself, the tool can't hand back every
combination's `runId` up front, because combinations are deliberately sequenced one at a time
(concurrent captures would race commands against the same camera), so a later combination's
`runId` doesn't exist until every earlier one has finished. A `sweepId`-tracked background task
is the only shape that works at all, not merely the more convenient one. Implemented in
`sensor_calibration_sweep.py`, mirroring `script_runs.py`'s own `_Run`/`_runs` pattern
(`_Sweep`/`_sweeps`, a `cancel_event`, a `latest_status` polled by `get_sweep_status`). This
module doesn't publish its own `sensorCalibrationSweep*` events to `event_streams` — a caller
polls `get_sensor_calibration_sweep_status` instead of subscribing — but see "Retrieving a
sweep's frames" below for how the *existing* `indi://mcp-server/scripts/{runId}` stream ends up scoped to a
sweep for free anyway.

## Retrieving a sweep's frames

**Decision: every combination in a sweep is started with `run_id=sweep_id`** (a caller-supplied
override added to `script_runs.start_script`), not a fresh `run_id` per combination. Since every
frame `capture_frame` saves is tagged with whatever `run_id` its enclosing script run was given,
this means every frame captured anywhere in a sweep — across every gain/offset/exposure
combination — shares one `run_id`, and `list_frames(run_id=sweepId)` retrieves all of them in a
single call. No `frame_store` schema change, no new `sweepId` column, no new `list_frames`
filter parameter.

This was a deliberate id-space unification, not an overload of an unrelated field — it was
seriously considered and rejected first: a caller passing an arbitrary UUID that could mean
*either* a `frameId`, a `runId`, or a `sweepId`, resolved by whichever table happens to match, is
exactly the kind of ambiguity this codebase's `kind`/`type`-tagged envelope convention exists to
avoid — a stale or mistyped id would silently resolve against the wrong thing instead of failing
clearly. Sharing `run_id` across a sweep's combinations is different: there's no guessing
involved, `list_frames(run_id=...)` keeps meaning exactly what it always has (frames from the
run(s) tagged with this id), a sweep's combinations just legitimately share one id by
construction. No information is lost by giving up *per-combination* `run_id` granularity either
— a frame's own FITS headers (`CCD_GAIN`/`CCD_OFFSET`/`CCD_EXPOSURE`) already record which
combination produced it, so `run_id` was never the only way to recover that.

Two consequences worth knowing, both accepted rather than mitigated:

* **The `indi://mcp-server/scripts/{runId}`-scoped event stream doubles as a per-sweep feed for free** —
  every combination's `scriptStarted`/`scriptProgress`/`scriptCompleted` events publish under
  the same id, so a client subscribing to `indi://mcp-server/scripts/{sweepId}` sees the whole sweep's
  blow-by-blow without this module needing its own event-publishing story.
* **`get_script_status`/`cancel_script`/`pause_script` also resolve against a `sweepId`** — it's
  a real key in `script_runs`'s own `_runs` dict for as long as a combination is in flight under
  it. They just answer about whichever single combination currently occupies that slot, not the
  sweep as a whole, so calling the wrong tool on a sweep id gives a differently-grained (if
  plausible-looking) answer rather than an error. Not a correctness bug — `script_runs` and
  `sensor_calibration_sweep` remain independent tracking systems that happen to share an id
  value — but worth knowing before reaching for `get_script_status` out of habit.

Safe only because a sweep's combinations run strictly sequentially, never concurrently (see
`start_script`'s own docstring for the collision this relies on not happening) — a design
constraint this module already had for the `sweepId`-vs-bare-`runId`-list reason above.

INDIMCPKit's own frame-retrieval wrapper for a sweep (tracked alongside IMCPKIT-32/33) is just
`list_frames(runId: sweepId)` under the hood — no new server-side tool needed for it.

**Also resolved: fail-fast, not best-effort.** If one combination's run doesn't end in
`scriptCompleted`, the sweep stops rather than continuing to later combinations — a failure
partway through usually means something about the rig/settings needs attention before spending
more capture time on likely-bad data. `results` still reports every combination that reached an
outcome before the stop (including the one that failed, or — if stopped via `cancel_sweep` — the
in-flight combination's own `scriptCancelled` outcome), not just the successful ones.

## Manual flat-panel staging within a flat sweep

**Resolved (INDIMCP-103): documented precondition, not in-band pause/confirmation** — option 2
from the two originally sketched here, not the pause-for-confirmation option this section
initially leaned toward. The actual deciding factor wasn't sweep length after all: this server
has no visibility into who or what is actually driving `run_flat_calibration_sweep` — it could be
a client app with a human operator in the loop (the expected case, and the one this decision is
made for) or a fully autonomous agent, and either way the server can't tell the difference or
verify a panel is genuinely staged. Given that, an in-band
`flatCalibrationSweepAwaitingConfirmation`-style pause would only add protocol surface (a new
status kind, a new confirm tool) without adding real safety — the client app is expected to
confirm panel placement with its human
operator *before* ever calling the tool, the same precondition `capture_flat_sequence` already
carries today. `flat_calibration_sweep.py`'s shape is consequently identical to
`sensor_calibration_sweep.py`'s (no pause/resume, `start_sweep`/`get_sweep_status`/`cancel_sweep`
only) rather than diverging for a confirmation step.

If a rig has a queryable/controllable INDI flat-panel device (e.g. an Alnitak Flip-Flat), driving
it automatically — turning it on before flats and off before flat-darks, verifying its state
rather than trusting the operator — is real future work, but deliberately not part of this pass;
tracked separately as INDIMCP-106.

## Flat-dark: no new manual action, but an ordering assumption to document

A flat-dark is just a dark frame at the flat's exposure length — it inherits the same "no light
entering the optical path" precondition every dark frame already assumes
(`capture_dark_sequence`'s own description already states "shutter/optical path irrelevant"
without ever verifying it; flat-dark adds nothing new there). So capturing it needs no
additional user action *beyond* what dark/bias already require, as long as it's captured before
the flat panel is ever staged — which is exactly the order the INDIMCP-102/103 split enforces
(bias+flat-dark ships as its own unattended script/sweep; the flat panel is only ever staged for
the separate flat-side invocation).

The one real risk is **sequencing**: a flat-dark captured *after* a flat, with the panel still
on/in place, is contaminated — it needs to be genuinely dark, not "panel off but nearby." Rather
than adding runtime detection (most rigs have no controllable/queryable flat-panel device to
check against) or a required confirmation pause, this is handled the same way
`capture_dark_sequence`/`capture_bias_sequence` already handle their own "assumes no stray
light" precondition: **documented only**, in both the bias/flat-dark script's own `description`
and INDIMCP-104's user-facing docs — flat-dark should be captured before the flat panel is ever
staged for that gain/offset/exposure combination, not after.

## Bias/dark side: no flat-panel dependency, no sweep-flexibility problem beyond parameters

The bias+flat-dark sweep tool (INDIMCP-102) has none of the manual-staging complexity above — it
only needs the same list-typed `gains`/`offsets` sweep mechanism, looping
`script_runs.start_script` calls against the trimmed `capture_sensor_calibration_set` (bias +
flat-dark only, exposure fixed per call to match whatever flat exposure it's paired with on the
flat side). It's listed as a separate INDIMCPKit/todo track (INDIMCP-102 vs. INDIMCP-103)
specifically because it has no manual precondition and can be built/shipped independently of the
flat side's staging-confirmation design.

## INDIMCPKit (Swift client) equivalents

IMCPKIT-32/33 mirror this split on the client side: typed Swift wrapper functions over the two
new MCP tools (`run_sensor_calibration_sweep`-for-bias-dark and its flat counterpart), following
the existing pattern of typed device-type abstractions built on top of the raw MCP tool-call
layer (see INDIMCPKit's own device-type abstractions for `Mount`/`Camera`/`FilterWheel`/
`Focuser`). Out of scope for this doc; tracked as their own todos once the server-side tool
shapes above are finalized, since the Swift signatures follow directly from them.

## INDIMCP-103: implemented

* **Script**: `capture_flat_sequence.yaml` extended with optional `gain`/`offset` parameters
  (same "omit to leave the device's current setting alone" convention as
  `capture_sensor_calibration_set`) — ordinary flat-field calibration callers leave them unset,
  a sweep always supplies both.
* **Sweep tool**: `run_flat_calibration_sweep`/`get_flat_calibration_sweep_status`/
  `cancel_flat_calibration_sweep` (`flat_calibration_sweep.py`) — a second, separate tool
  alongside `run_sensor_calibration_sweep`, not a branch on the same one, matching the
  two-script split.
* **Combination order**: cartesian product of `gains × offsets × exposureSecondsList`
  (`itertools.product`, gains outermost), matching INDIMCP-102's own resolution — kept
  consistent rather than reconsidered, since nothing about real-world flat-sweep sizes turned
  out to demand a different shape.
* **Flat-panel staging**: resolved as documented precondition only, not pause/resume — see
  "Manual flat-panel staging within a flat sweep" above for the reasoning (the server can't tell
  who/what is driving the tool or verify a panel is staged either way, so an in-band
  confirmation step would add surface without adding real safety). INDI-panel auto-control is
  tracked separately as INDIMCP-106.
* **Counts**: `filterName`/`focusPosition`/`count` are fixed per sweep, shared across every
  combination — same "not itself swept" treatment as INDIMCP-102's `biasCount`/`darkCount`.
