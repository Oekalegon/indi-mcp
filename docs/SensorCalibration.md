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
`save_script`'s validation, cycle detection, etc. are exactly the safety mechanism that only
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
(`_Sweep`/`_sweeps`, a `cancel_event`, a `latest_status` polled by `get_sweep_status`) but
without its own `event_streams`/`indi://scripts` publishing — deliberately out of scope for this
pass; a caller polls `get_sensor_calibration_sweep_status` instead of subscribing.

**Also resolved: fail-fast, not best-effort.** If one combination's run doesn't end in
`scriptCompleted`, the sweep stops rather than continuing to later combinations — a failure
partway through usually means something about the rig/settings needs attention before spending
more capture time on likely-bad data. `results` still reports every combination that reached an
outcome before the stop (including the one that failed, or — if stopped via `cancel_sweep` — the
in-flight combination's own `scriptCancelled` outcome), not just the successful ones.

## Manual flat-panel staging within a flat sweep

The flat-side sweep tool still can't stage the panel itself. Two options, to be settled during
INDIMCP-103:

1. The tool starts, then blocks (as an async background task, not blocking the MCP call) at a
   confirmation point before the first flat-illuminated capture, surfaced via the sweep's status
   the same way `scriptPaused`/`pause_script` already communicate "waiting on something" —
   requiring an explicit `resume`-style call once the operator has staged the panel.
2. The tool documents the precondition ("flat panel must already be in place before calling
   this") and does no in-band staging/confirmation at all, mirroring how
   `capture_flat_sequence` already behaves today — simplest, but repeats the same "the caller
   has to know" gap this whole design started from.

Leaning toward (1) for the flat sweep specifically, since a sweep is long-running and
multi-combination — an operator who steps away expecting bias/dark automation to run unattended
should not have flats silently fail or produce garbage because the panel was never placed. This
still needs to be scoped in INDIMCP-103 alongside the flat script split itself.

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

## Open items for INDIMCP-103

INDIMCP-102 (bias/flat-dark side) is implemented; everything below is specific to the remaining
flat-side work:

* Exact flat sweep tool name — a second tool alongside `run_sensor_calibration_sweep`
  (`run_flat_calibration_sweep`, or similar), not a branch on the same tool, matching the
  two-script split and letting the bias/dark tool exist independently of the flat panel's
  staging-confirmation design.
* Cartesian product of `gains × offsets × exposures`, matching INDIMCP-102's own resolution
  (`itertools.product`, gains outermost) — likely the same choice for consistency, but worth
  confirming once real-world flat-sweep sizes are known (a flat sweep multiplies exposure levels
  in too, so combinatorial blow-up is a bigger risk here than on the bias/flat-dark side).
* Flat-panel staging mechanism (pause/resume vs. documented precondition) for the *flat* side —
  resolved as document-only for flat-*dark* (see "Flat-dark" section above); the flat side's own
  staging mechanism is still open, and INDIMCP-102's `_Sweep`/background-task shape is the
  natural place to hang a pause-for-confirmation step if that's the direction chosen.
* Whether `flatCount`/`biasCount`/`darkCount` are fixed per sweep or themselves swept — no known
  need for this yet, so not currently planned (INDIMCP-102 keeps `biasCount`/`darkCount` fixed
  per sweep, shared across every combination).
