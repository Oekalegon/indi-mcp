# Script YAML Schema

A **script** is a declarative sequence of INDI steps — set a property, wait for a condition,
capture a frame, slew, or call another script — used to run imaging sequences on the INDI Device
without an embedded expression language. See
[Design.md § INDI scripting layer](Design.md#architecture-overview),
[§ Calling scripts and script results](Design.md#calling-scripts-and-script-results), and
[§ Composing scripts](Design.md#composing-scripts) for the background and rationale; this
document is the field-by-field reference for the YAML format itself (INDIMCP-6). The execution
engine that runs this schema is a separate, later task (INDIMCP-7).

Each script is one YAML file in the script library, named freely — the file's `id` field, not
its filename, is what `run_script` calls (and other scripts, see "Composing scripts" below)
reference. Files are loaded with `yaml.safe_load` and validated against the schema below; a file
that fails to parse or validate is logged and skipped rather than aborting the whole load
(matching `load_rigs`'s behavior — see
[RigSchema.md](RigSchema.md)). Unknown fields are rejected, not ignored. `id` must be unique
across the whole library, since `run_script` steps resolve by `id` within that same library —
see "Script composition" below.

This is deliberately a **closed, fixed vocabulary of step primitives** (`set_property`,
`wait_for`, `capture_frame`, `slew`, `run_script`, `repeat`, `if`) — unlike a rig component's
`role`, which accepts any string for extensibility, a step's `step` field must be one of these
exact values. There is no embedded expression language: conditionals are a fixed, closed set of
comparison operators over known INDI property state, not arbitrary code — see "Design notes"
below.

## Example

```yaml
id: capture_sequence_m101
name: Capture 20x5min frames of M101 with periodic refocus
pausable: true
parameters:
  targetTempC:
    type: number
    required: false
    default: -10
  exposureSeconds:
    type: number
    required: true
steps:
  - step: run_script
    script: cool_camera
    parameters: { targetTempC: "{{ targetTempC }}" }
  - step: slew
    role: mount
    target:
      objectName: M101
  - step: run_script
    script: plate_solve_until_precision
    parameters: { toleranceArcsec: 5 }
  - step: set_property
    role: filterWheel
    property: FILTER_SLOT
    elements: { FILTER_SLOT_VALUE: "1" }
  - step: repeat
    count: 20
    steps:
      - step: run_script
        script: focus
        every: 2
      - step: capture_frame
        role: camera
        exposureSeconds: "{{ exposureSeconds }}"
        frameType: Light
```

(`"{{ targetTempC }}"` denotes a reference to one of this script's own declared `parameters` —
see "Parameter references" below; this is substitution of an already-validated typed value, not
an embedded expression language.)

## Top-level fields

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | yes | Stable identifier for this script. Used by `run_script` calls (from the Client Computer or from another script's `run_script` step) to reference it. Must be unique across the whole library; a duplicate `id` is logged and skipped, keeping whichever file loaded first (files read in sorted filename order) — matching `load_rigs`'s policy. |
| `name` | string | yes | Human-readable display name. |
| `description` | string | no | Longer human-readable explanation, surfaced to clients listing available scripts. |
| `pausable` | boolean | yes | Whether `pause_script` can succeed on a run of this script (see [Design.md § Calling scripts and script results](Design.md#calling-scripts-and-script-results)). Required, not defaulted — every script author must explicitly decide this rather than the schema silently picking a default that might be unsafe (e.g. pausing mid-slew). |
| `parameters` | map of string → [Parameter](#parameter-fields) | no | Named, typed inputs this script accepts — from a top-level `run_script` MCP call, or from a `run_script` step in another script. Omit if the script takes none. |
| `steps` | list of [Step](#step-primitives) | yes (may be empty) | The script's body, executed in order (except where a step's own semantics say otherwise — `repeat`, `if`). |

## Parameter fields

Each entry in a script's `parameters` map declares one named input:

| Field | Type | Required | Description |
|---|---|---|---|
| `type` | string | yes | One of `"string"`, `"integer"`, `"number"`, `"boolean"`. A closed, small type vocabulary — no nested objects/arrays as parameter types, keeping validation simple and the substitution mechanism below unambiguous. |
| `required` | boolean | no (default `false`) | Whether the caller must supply this parameter. |
| `default` | matching `type` | no | Used when `required` is `false` and the caller omits this parameter. |
| `description` | string | no | Human-readable explanation, e.g. for a client to show in a script-picker UI. |

### Parameter references

A step field may reference one of the enclosing script's own declared `parameters` with
`"{{ paramName }}"` (a full-string match, not string interpolation into a larger string — the
whole field value is replaced with the parameter's typed value, so a `number` parameter
substitutes as a number, not a string). This is plain value substitution against an
already-validated, already-typed parameter — never an expression to evaluate — consistent with
the no-embedded-expression-language rule. A reference to an undeclared parameter name is a
validation error at load time, not a runtime failure.

## Step primitives

Every step is an object with a `step` field naming the primitive (matching the shape already
used in [Design.md § Composing scripts](Design.md#composing-scripts)'s examples), plus that
primitive's own fields below. Any step may also carry:

| Field | Type | Required | Description |
|---|---|---|---|
| `description` | string | no | Human-readable label for this step, surfaced in `scriptProgress`'s `message`. |
| `every` | integer | no | Only meaningful directly inside a `repeat` body: run this step only on every Nth iteration (1-indexed) — e.g. `every: 2` runs on iterations 2, 4, 6, .... Omit to run on every iteration. |

### Execution model: generic vs. engine-implemented primitives

The step vocabulary is closed rather than open-ended (unlike a rig component's `role`) because
each primitive falls into one of two tiers, and this is a real constraint on what a script author
can express in YAML alone — not just a stylistic choice:

* **Generic primitives** — `set_property`, `wait_for`, `run_script`, `repeat`, `if` — have a
  single, uniform execution-engine handler that works identically regardless of which device or
  script it's pointed at. `set_property`/`wait_for` are thin wrappers over the messaging layer
  (`send_property` / property polling, after `role` → `device` resolution); `run_script`/`repeat`/
  `if` are pure control flow with no INDI interaction of their own. Nothing about these needs
  device- or operation-specific code — the same handler serves every script.
* **Engine-implemented primitives** — `capture_frame`, `slew` — each bundle a *sequence* of INDI
  commands (and sometimes non-INDI work) that isn't reducible to a single `set_property`/
  `wait_for` pair. `capture_frame`, for example, is really "set frame type, set exposure, wait
  through the `Busy`→`Ok` transition, drain the BLOB, write it to frame storage, return metadata"
  — several INDI commands plus a file write. `slew` similarly bundles "set target coordinates,
  wait for the mount's `Busy`→`Ok` transition." Each of these has its own dedicated Python
  function in the execution engine (INDIMCP-7); the YAML step only declares *what* should happen
  (`role`, `exposureSeconds`, ...), never *how* — the handler owns the actual INDI command
  sequence.

This second tier is also where anything requiring computation beyond a raw property read has to
live — e.g. a future `plate_solve` step (see [Design.md § Composing scripts](Design.md#composing-scripts)'s
`plate_solve_until_precision` example, and INDIMCP-27) would need a handler that captures a
frame, calls out to a solver, computes the angular separation from the target (spherical
trigonometry — RA/Dec aren't comparable with a plain numeric `Condition`), and returns that
result. **Adding a new capability like this means adding both a new step type to this schema and
its handler in the execution engine — never something a script author can build unilaterally out
of `set_property`/`wait_for`.** A corollary: `Condition` (below) can only check *live INDI
property state*, not a computed value like a plate-solve separation — a script that needs to
loop on a computed result needs that computation exposed as its own engine-implemented step
first (out of scope for this schema revision; noted here so it isn't lost).

#### `set_property`

Sends a command to a device, without waiting for it to take effect — chain a `wait_for` step
after it if the script needs to block until the property reaches a target state. Mirrors
`send_property`'s `elements` shape (see [Design.md § MCP message format](Design.md#mcp-message-format)).

| Field | Type | Required | Description |
|---|---|---|---|
| `role` | string | yes | The rig component role (see "Resolving roles to devices" below) whose device this targets. |
| `property` | string | yes | The INDI property (vector) name, e.g. `"CCD_EXPOSURE"`. |
| `elements` | map of string → string | yes | Element name → value, as sent in a `new*Vector` command. |

#### `wait_for`

Blocks until `condition` is met or `timeoutSeconds` elapses, in which case the script run fails
(`scriptFailed`) at this step — there is no infinite wait, since an unreachable condition (e.g.
a disconnected device) would otherwise hang the run forever.

| Field | Type | Required | Description |
|---|---|---|---|
| `condition` | [Condition](#condition-fields) | yes | What to wait for. |
| `timeoutSeconds` | number | yes | Maximum time to wait before failing this step. |

#### `capture_frame`

| Field | Type | Required | Description |
|---|---|---|---|
| `role` | string | yes | Typically `"camera"` or `"guideCamera"`. |
| `exposureSeconds` | number | yes | Exposure length. |
| `frameType` | string | no (default `"Light"`) | One of `"Light"`, `"Dark"`, `"Flat"`, `"Bias"` — matches INDI's `CCD_FRAME_TYPE` values. |
| `binningX` / `binningY` | integer | no (default `1`) | Pixel binning, if the camera supports it. |

Captured frames are stored on the INDI Device and reported back through the script result — see
[Design.md § Frame storage metadata](Design.md#frame-storage-metadata) (a later, separate design
task, INDIMCP-10/11).

#### `slew`

| Field | Type | Required | Description |
|---|---|---|---|
| `role` | string | yes | Typically `"mount"`. |
| `target` | object | yes | Exactly one of `raDec` or `objectName` (below). |
| `target.raDec.ra` | number | one of `raDec`/`objectName` | Right ascension, in hours (matching INDI's `EQUATORIAL_EOD_COORD` `RA` element). |
| `target.raDec.dec` | number | one of `raDec`/`objectName` | Declination, in degrees (matching INDI's `EQUATORIAL_EOD_COORD` `DEC` element). |
| `target.objectName` | string | one of `raDec`/`objectName` | A named object (e.g. `"M101"`) for the execution engine to resolve to RA/Dec — mechanics (e.g. via `astropy`, see INDIMCP-29) are an execution-engine concern (INDIMCP-7), not fixed by this schema. |

#### `run_script`

Calls another script from the same library by `id` — see "Script composition" below for the
full mechanics (cycle detection, nested progress, cancellation, dynamic `pausable`).

| Field | Type | Required | Description |
|---|---|---|---|
| `script` | string | yes | The called script's `id`. |
| `parameters` | map of string → any | no | Arguments for the called script's own declared `parameters`; validated against that script's `parameters` schema at load time (see "Script composition"). |

#### `repeat`

A closed loop construct — either a fixed `count`, or a `until` condition — never an arbitrary
expression. `until` always requires `maxIterations` as a hard safety cap, since a condition that
never becomes true would otherwise loop forever.

| Field | Type | Required | Description |
|---|---|---|---|
| `count` | integer | one of `count`/`until` | Run `steps` exactly this many times. |
| `until` | [Condition](#condition-fields) | one of `count`/`until` | Run `steps` repeatedly, checking `until` after each iteration, stopping once it's met. |
| `maxIterations` | integer | required with `until` | Hard cap on iterations; reaching it without `until` becoming true fails the script (`scriptFailed`) rather than looping forever. |
| `steps` | list of [Step](#step-primitives) | yes | The loop body. |

#### `if`

| Field | Type | Required | Description |
|---|---|---|---|
| `condition` | [Condition](#condition-fields) | yes | What to check. |
| `then` | list of [Step](#step-primitives) | yes | Executed if `condition` is met. |
| `else` | list of [Step](#step-primitives) | no | Executed if `condition` is not met. Omit for a no-op else-branch. |

## Condition fields

Shared by `wait_for`, `repeat`'s `until`, and `if`. A condition compares one piece of known INDI
state against a fixed value — never an arbitrary expression.

| Field | Type | Required | Description |
|---|---|---|---|
| `role` | string | yes | The rig component role whose device this checks. |
| `property` | string | yes | The INDI property (vector) name. |
| `element` | string | no | The element within `property` to compare. Omit to compare the property's own `state` (`"Idle"`/`"Ok"`/`"Busy"`/`"Alert"`) instead of an element value. |
| `operator` | string | yes | One of `"equals"`, `"notEquals"`, `"greaterThan"`, `"lessThan"`, `"greaterThanOrEqual"`, `"lessThanOrEqual"`. A closed set — `greaterThan`/`lessThan`/... only apply to numeric element values. |
| `value` | string, number, or boolean | yes | The value to compare against. |

## Resolving roles to devices

Steps and conditions never name an INDI device directly — they reference a rig component
**role** (`"camera"`, `"focuser"`, `"filterWheel"`, `"mount"`, ... — the same roles as
[RigSchema.md](RigSchema.md#component-fields), including its `| any other string` escape hatch),
exactly the way a rig component's `role` field works. This is what lets the same script run
unchanged on different physical setups: a `run_script` MCP call (or `run_script` step, see
below) carries a `rigId` alongside `script`/`parameters` (extending
[Design.md § Calling scripts and script results](Design.md#calling-scripts-and-script-results)'s
"Starting a script" shape), and every `role` a step references is resolved against that rig's
`components` for the whole run — see `get_rig`/`check_rig` in [RigSchema.md](RigSchema.md). A
role with no matching component in the selected rig, or matching a component with no `device`
(e.g. a `telescope` component, which has no INDI device of its own), is a validation error at
run start, not a per-step runtime failure.

**A role must resolve to exactly one device-bearing component.** Unlike a rig, which allows
more than one component to share a role (disambiguated by `id` — e.g. two independently
addressed dew heater channels), a script's role reference has no `id` to disambiguate with: it
names only the role. If the selected rig has more than one device-bearing component for a role
a script references, that's also a validation error at run start (the same tier as a missing
role), rather than the engine silently picking one.

A nested `run_script` step (see "Script composition" below) does **not** repeat `rigId` — every
script in a single run, top-level and nested, resolves roles against the one rig selected when
the run started.

**A `role` field is a normal substitutable field, not a special case.** Like any other step
field, it may be a literal (`role: mount`) or a `"{{ paramName }}"` reference to one of the
script's own declared `parameters` (see "Parameter references" above) — for example, a single
generic script can take `role` itself as a `string` parameter instead of shipping one copy per
role. A parameterized role is resolved against the caller's actual argument for every
invocation in the run (including through nested `run_script` calls, each resolved against its
own arguments) up front, before any step runs, exactly like a literal role — a role parameter
that resolves to no matching component, an ambiguous component, or a non-`string` value is
still a validation error at run start, not a per-step runtime failure.

## Script composition

Reusing small primitive scripts (`cool_camera`, `slew`, `plate_solve`, `focus`, `capture_frame`,
...) to build realistic sequences, per
[Design.md § Composing scripts](Design.md#composing-scripts), means `run_script` steps must
resolve correctly and safely across the whole library:

* **Same library, resolved by `id`.** A `run_script` step's `script` is looked up from the exact
  same library a top-level `run_script` MCP call would use — there's no separate namespace of
  reusable fragments.
* **Cycle detection at load time.** Loading the library builds a call graph from every script's
  `run_script` steps (including ones nested inside `repeat`/`if` bodies) and rejects any script
  whose graph reaches a cycle back to itself — logged and skipped, like any other invalid script
  — rather than discovering infinite recursion at runtime.
* **Parameters are validated per call.** A `run_script` step's `parameters` are checked against
  the *called* script's own declared `parameters` schema, independently of the calling script's
  parameters (a step's fields may reference the calling script's parameters via `"{{ ... }}"`
  substitution — see "Parameter references" — but the called script never sees the caller's
  parameter names, only the resolved values passed to it).
* **Nested progress.** A sub-script run gets its own `runId`, tagged with a `parentRunId`, so
  `scriptProgress`/`scriptCompleted`/etc. form a walkable execution tree, per
  [Design.md § Composing scripts](Design.md#composing-scripts).
* **Cancellation cascades**; **`pausable` is dynamic** — the effective pausability of a composite
  run is whatever its currently-executing (sub-)script declares, not a single value fixed at
  `scriptStarted`. Both per [Design.md § Composing scripts](Design.md#composing-scripts).

## Uploading client-authored scripts

`save_script` (INDIMCP-9) lets a client write a script on the Client Computer and upload it to
the MCP server to run, per [Design.md](Design.md#architecture-overview)'s scripting-layer intro
and "YAML is loaded only with `yaml.safe_load`" above. Uploaded scripts are validated the same
way as built-in ones (schema, then the library-wide `run_script`/cycle/argument checks from
"Script composition") — a bad upload is rejected outright, before anything is written, rather
than being written and silently dropped at the next load.

* **Built-in and uploaded scripts live in separate directories on disk.** Built-in scripts ship
  in the repo checkout (`$INDI_MCP_SCRIPTS_DIR`, falling back to `./scripts`); uploaded scripts
  are written to a separate directory (`$INDI_MCP_USER_SCRIPTS_DIR`, falling back to
  `./user_scripts`). This is deliberate: on the deployed Raspberry Pi, `$INDI_MCP_SCRIPTS_DIR`
  points at the git checkout (see `deploy/indi-mcp.service`), so a redeploy overwrites it — an
  upload written there would be silently lost on the next deploy, and any file living there could
  be mistaken for a reviewed, version-controlled built-in.
* **One flat `id` namespace at runtime.** The two directories are merged into a single
  `id`-keyed library before validation, so a `run_script` step in an uploaded script can call a
  built-in one and vice versa — same as any other `run_script` reference in "Script composition".
* **A built-in `id` always wins.** If an uploaded script's `id` collides with a built-in one, the
  built-in is kept and the uploaded script is dropped (logged) at load time; `save_script` itself
  rejects an upload that reuses a built-in `id` outright, so a client can never shadow or
  override built-in behavior by choosing the same `id`.
* **Uploads are validated against the *whole* merged library, not just themselves.** Saving a
  script also re-checks any existing script whose `run_script` step calls into it (its parameter
  schema may have just changed under them) and whether it would close a call cycle — the same
  checks "Script composition" describes for load time, run before the write instead of after.

## Design notes

* **Fixed step vocabulary, no embedded expression language.** `step` is a closed enum
  (`set_property`, `wait_for`, `capture_frame`, `slew`, `run_script`, `repeat`, `if`); condition
  `operator`s are a closed enum; parameter substitution (`"{{ name }}"`) is plain value lookup,
  never code to evaluate. A script is declarative data, safe to author on the Client Computer and
  upload, consistent with [Design.md](Design.md#architecture-overview)'s scripting-layer intro.
  This is also why the vocabulary is closed rather than extensible: every primitive beyond
  `set_property`/`wait_for`/`run_script`/`repeat`/`if` needs a dedicated engine handler (see
  "Execution model" under [Step primitives](#step-primitives)), so new capabilities are a schema
  *and* engine change together, not something a script can invent on its own.
* **No unbounded waits or loops.** `wait_for` always requires `timeoutSeconds`; `repeat`'s
  `until` form always requires `maxIterations`. A script can still fail slowly (a generous
  timeout), but never hang the run indefinitely.
* **Unknown fields are rejected**, not ignored — same rule as [RigSchema.md](RigSchema.md#design-notes),
  so a typo'd field name fails loudly (a skipped file, logged) at load time.
* **YAML is loaded only with `yaml.safe_load`**, never `yaml.load`/`yaml.unsafe_load` or a custom
  `Loader` — scripts may be authored on the Client Computer and uploaded (INDIMCP-9), so this is
  a hard safety rule, not a style preference.
