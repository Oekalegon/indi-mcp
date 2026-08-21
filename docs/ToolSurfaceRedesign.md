# MCP Tool Surface Redesign

## Why

The server currently registers 68 individual `@mcp.tool()` entries in `src/indi_mcp/server.py`. LLM clients select which tool to call from the full list of names/descriptions/schemas on every turn; past roughly 30-40 tools, accuracy drops and near-duplicate names (`start_indi_server`/`stop_indi_server`/`restart_indi_server`, or the fifteen single-verb rig actions like `park`/`unpark`/`slew`/`cool_camera`/...) become genuinely ambiguous to pick between. This document defines a consolidated tool surface, replacing many single-purpose tools with fewer tools parameterized by a `kind`/`action`/`component` discriminator — the same design move the project already made for the INDI message envelope (`kind`/`type`, see [MessageSchema.md](MessageSchema.md)).

Target: **68 → ~22 tools** (a ~68% reduction), while keeping enough schema structure per tool that a client can still validate parameters against JSON Schema rather than having to infer valid shapes from a description string.

This document is the reference for **both** sides of the change:
- the INDI MCP server implementation (`src/indi_mcp/server.py`), tracked in INDIMCP-113
- the INDIMCPKit client's server-facing interface, tracked in IMCPKIT-52 — INDIMCPKit's public `Mount`/`Camera`/`FilterWheel`/`Focuser` API is expected to keep its current shape; only the translation layer that maps those calls onto MCP tool calls needs to target the new tool names/signatures below.

## Design principle: when to merge tools

Two tools are merged into one parameterized tool only when they are the **same operation shape acting on a discriminator** (e.g. start/stop/restart all take the same "which lifecycle transition" shape; get/save/draft all take the same CRUD shape across rig/observatory/script). Tools are **not** merged when their parameters are fundamentally different in kind, or when the discriminator and the valid-parameter set stop moving independently of each other, even if the tools are topically related — that would force a union-typed schema that stops discriminating valid parameters per call, trading the tool-selection problem for a parameter-selection problem. This is also why the redesign stops at ~22 tools rather than collapsing further into one or two mega dispatch tools; the Configuration entities group below (§ Configuration entities) is a worked example of where that line was drawn.

**Accepted trade-off: conditional defaults become docstring-only.** A single-purpose tool like the old `start_indi_server(port: int = INDI_PORT)` has a real default in its JSON Schema — a schema-driven client sees `port` defaults to `INDI_PORT` without reading prose. Once `port` is shared across a discriminator (e.g. `manage_indi_infra`'s `component`/`action`), its *effective* default becomes conditional — `INDI_PORT` for `component="server"`/`"messaging"` starts, meaningless for `component="driver"` — and JSON Schema has no way to express a default that depends on another parameter's value. The information doesn't disappear, but it moves from a schema-level default into docstring prose, which a caller reasoning purely from the parameter schema (rather than the full description text) won't see the same way. This is accepted as an inherent, unavoidable cost of every group that merges a parameter with a conditional default — not something to solve per group with a sentinel value (there is no real unconditional default to fall back to), and not worth re-litigating each time it recurs (it already does in `manage_indi_infra`, INDIMCP-114, and will again wherever a merged tool's parameter defaults change meaning by discriminator). The compensating control is that the docstring always states the effective default per branch explicitly, as `manage_indi_infra`'s does.

**Resolved, not accepted: a payload merged across `kind` can keep its structural schema via `json_schema_extra`.** The same mechanism above has a more severe-looking instance wherever a merged parameter's *entire shape* — not just a default — varies by discriminator. `configuration`'s `config: dict[str, Any]` (INDIMCP-115) is the first case: the old `save_rig(rig: Rig, ...)`/`save_observatory`/`save_script` each had FastMCP auto-generate their full pydantic model's JSON Schema (every required/optional field, nested shapes) because the parameter was typed as the actual model; `configuration` has to represent `Rig`/`Observatory`/`Script` in one parameter depending on the sibling `kind` argument, and JSON Schema can't make a parameter's shape conditional on another parameter's value any more than it can make a default conditional. This was initially recorded as an accepted trade-off (PR #90's review), but turned out to be recoverable: `configuration`'s `config` parameter carries a `schemaByKind` entry (via `Annotated[..., Field(json_schema_extra=...)]`) with the real `Rig`/`Observatory`/`Script` JSON Schemas keyed by `kind` — see `_CONFIG_SCHEMA_BY_KIND`/`_defuse_schema_refs` in `server.py`. `config` itself still stays a plain `dict[str, Any]` at runtime (no discriminated union, no double-dispatch risk, no change to the on-disk `Rig`/`Observatory`/`Script` YAML schemas); only its *declared* schema gained the real structure, as pure documentation `json_schema_extra` doesn't affect FastMCP's own validation. Getting there took two false starts worth remembering if this pattern recurs: embedding a model's raw `model_json_schema()` output (with its own `$defs`/`$ref`) three levels deep breaks pydantic's schema generation for the *whole* `configuration` tool, not just `config` — pydantic's `GenerateJsonSchema` walks the entire final document for any literal `$ref` key, including inert data inside an unrelated field's `json_schema_extra`, and tries to resolve it against its own internal definitions regardless of what it actually points to; and inlining `$defs` away instead of rewriting `$ref` doesn't work either, since `Script`'s schema is genuinely self-referential (an `if`/`repeat` step's own steps are `Step`s again) and inlining recurses infinitely. The fix that worked: rename the reserved `$ref`/`$defs` keys to the non-reserved `x-ref`/`x-defs` (the standard JSON Schema vendor-extension prefix) before embedding, so pydantic's walker doesn't recognize them at all — a reader of `schemaByKind` just needs to know that substitution to reconstruct the original schema. A discriminated union (`config: Rig | Observatory | Script`, keyed by a shared field) was also investigated and rejected regardless of the `json_schema_extra` fix working: `Rig`/`Observatory`/`Script` share no discriminator field today, and adding one would mean a new required field on the *on-disk YAML schema* for every existing `rigs/*.yaml`/`observatories/*.yaml`/`scripts/*.yaml` file; a plain (non-discriminated) union would also have FastMCP auto-resolve `config` via pydantic's own union matching *before* the tool body runs, a second, independent type-resolution mechanism running alongside the tool's own explicit `kind` dispatch with no guarantee the two agree. Worth trying the same `json_schema_extra` approach wherever this recurs (a candidate to watch for in Direct device actions, INDIMCP-116, e.g. `camera_action`'s `capture` action carrying many action-specific optional fields no other action uses) before accepting the loss as permanent.

## Target tool inventory

### INDI infrastructure (was 15 tools → 5) — implemented (INDIMCP-114)

| Tool | Replaces | Signature |
|---|---|---|
| `get_server_info` | *(unchanged)* | `()` |
| `manage_indi_infra` | `start/stop/restart_indi_server`, `start/stop_indi_driver`, `start/stop_indi_messaging` | `(component: "server"\|"driver"\|"messaging", action: "start"\|"stop"\|"restart", label?: str, host?: str, port?: int)` |
| `get_indi_status` | `get_indi_server_status`, `get_indi_messaging_status` | `(component: "server"\|"messaging")` |
| `list_indi_drivers` | `list_indi_driver_catalog`, `list_running_indi_drivers` | `(scope: "catalog"\|"running")` |
| `indi_property` | `get_device_properties`, `send_indi_property` | `(action: "get"\|"set", device: str, name?: str, elements?: dict[str, str])` |

### Configuration entities — rig / observatory / script (was 16 tools → 4) — implemented (INDIMCP-115)

Generic CRUD collapses across entity kinds:

| Tool | Replaces | Signature |
|---|---|---|
| `list_config` | `list_rigs`, `list_observatories`, `list_scripts` | `(kind: "rig"\|"observatory"\|"script")` |
| `configuration` | `get_rig`/`get_observatory`/`get_script`, `save_rig`/`save_observatory`/`save_script`, `draft_rig`/`draft_observatory` | `(action: "get"\|"save"\|"draft", kind: "rig"\|"observatory"\|"script", id?: str, config?: dict, overwrite: bool = False)`. `draft` is invalid for `kind="script"` — no `draft_script` exists; the implementation must reject that combination rather than silently no-op. |

`get`/`save`/`draft` genuinely share one shape (an entity keyed by `kind`, optionally identified by `id`, optionally carrying a `config` payload), which is why they collapse cleanly into `configuration`. `check_rig`, `suggest_rig`, `sync_filter_names`, and `run_script` were considered for the same tool and **rejected** — see rationale below.

Kept separate:

| Tool | Notes |
|---|---|
| `rig_diagnostics` | merges `check_rig`, `suggest_rig`, and `sync_filter_names`/`adopt_filter_names_from_driver` — `(action: "check"\|"suggest"\|"sync", rig_id?: str, role?: str, direction?: "to_driver"\|"from_driver")`. `rig_id` is required for `check`/`sync`, absent for `suggest` (which enumerates candidate rigs); `role`/`direction` only apply to `sync`. All three are rig-only, synchronous, and read/analysis-flavored (even `sync` returns a reconciliation report, `FilterSyncOutcome`, rather than mutating anything itself), which is what makes them a coherent single tool unlike the rejected wider merge. |
| `run_script` | unchanged — starts an async run, returns `runId`. Kept separate from `configuration`/`rig_diagnostics` because it's the only asynchronous, run-handle-returning operation in this group; its required params (`script`, `rig_id`, `parameters`) and return shape don't share a discriminator with the synchronous CRUD/diagnostic tools. |

**Why `check`/`suggest`/`sync`/`run` don't belong in `configuration`:** a wider merge was proposed and rejected during design. The blocker is that `action` and `kind` stop moving independently — `check`/`suggest`/`sync` only ever pair with `kind="rig"`, `run` only with `kind="script"` and needs a `parameters` dict instead of `config`, and `run`'s return type (an async run handle) differs structurally from every other action's return type (a config object). At that point the tool schema can no longer express which parameters are valid for a given `action`/`kind` pair via `oneOf`/required-field constraints alone, so validity would fall back to hand-written runtime checks (asking the tool to "return errors for wrong combinations") instead of a schema the client — or the model — can reason about before calling. That's the same trade the design principle above warns against: it trades the tool-*selection* problem for a parameter-*selection* problem. `kind="filters"` was also proposed for this tool and rejected for a related reason: `rig`/`observatory`/`script` are config *entity types*, while "filters" names an *operation on a rig*, not a fourth entity — conflating the two muddies what the discriminator means.

### Direct device actions (was 15 tools → 5) — implemented (INDIMCP-116)

Grouped by device role rather than by verb — matches how a rig user already thinks about their gear:

| Tool | Replaces | Actions |
|---|---|---|
| `mount_action` | `park`, `unpark`, `slew`, `track_off`, `set_track_mode`, `set_custom_tracking_rate` | `(rig_id, action: "park"\|"unpark"\|"slew"\|"track_off"\|"set_track_mode"\|"set_custom_tracking_rate", ...action-specific params)` |
| `camera_action` | `cool_camera`, `cooler_on`, `cooler_off`, `abort_exposure`, `capture_frame` | `(rig_id, action: "cool"\|"cooler_on"\|"cooler_off"\|"abort_exposure"\|"capture_frame", ...action-specific params)` |
| `filter_wheel_action` | `select_filter` | `(rig_id, action: "select", filterName)` |
| `focuser_action` | `set_focus_position` | `(rig_id, action: "set_position", position)` |
| `set_connection` | `connect`, `disconnect` | `(rig_id, role, connected: bool)` |

### Script run control (was 4 tools → 1)

| Tool | Replaces | Signature |
|---|---|---|
| `manage_script_run` | `get_script_status`, `cancel_script`, `pause_script`, `resume_script` | `(run_id, action: "status"\|"cancel"\|"pause"\|"resume")` |

### Calibration sweeps (was 6 tools → 2)

Sensor and flat sweeps are structurally identical (run / status / cancel) for two sweep types:

| Tool | Replaces | Signature |
|---|---|---|
| `run_calibration_sweep` | `run_sensor_calibration_sweep`, `run_flat_calibration_sweep` | `(kind: "sensor"\|"flat", rig_id, ...kind-specific params)` |
| `manage_calibration_sweep` | `get_sensor/flat_calibration_sweep_status`, `cancel_sensor/flat_calibration_sweep` | `(sweep_id, action: "status"\|"cancel")` |

**Considered and rejected: exposing this as a script step instead of a tool.** [docs/SensorCalibration.md § Why the sweep can't be expressed as script `parameters`](SensorCalibration.md#why-the-sweep-cant-be-expressed-as-script-parameters) already analyzed this exact idea and rejected it: a `calibration_sweep` step type's own fields *could* be list-typed (the same way `PlateSolveStep`'s fields already aren't restricted to scalars), but there's no way to get a dynamic value into that field from a caller — `run_script`'s `parameters` mechanism is deliberately restricted to a closed scalar vocabulary (`string`/`integer`/`number`/`boolean`, explicitly no arrays), to keep script validation and substitution unambiguous for uploaded YAML. A script wrapping such a step could only run with whatever `gains`/`offsets`/exposure list was hardcoded into the YAML at authoring time — worse than the tool, not equivalent to it, since the entire point of a sweep is choosing the range per invocation. Unlike the plate-solving case below, this isn't a matter of the tool duplicating logic that already lives in the script engine; the sweep's list-valued, per-invocation-configurable inputs are precisely what the script schema was designed to exclude. Keep it a dedicated tool.

### Plate solving (was 5 tools → 2)

| Tool | Replaces | Signature |
|---|---|---|
| `plate_solve_uploaded_frame` | `plate_solve_uploaded_frame` | `(frame_upload, mountRole?, ...)` — kept as its own tool; unlike the rig-based case below, there's no rig/mount role to run a script against here, just a client-supplied frame, so this isn't script-shaped at all. |
| `manage_astrometry_index` | `list_astrometry_index_files`, `download_astrometry_index_files` | `(action: "list"\|"download", ...)` |

The rig-based cases — old `plate_solve` and `plate_solve_until_precision` — are **dropped as tools entirely**, not merged. `script_store.py`'s `PlateSolveStep` already exists as a script step primitive and already implements retry-toward-a-tolerance internally (`toleranceArcsec`/`maxAttempts`, resolved server-side in `_execute_plate_solve` rather than via YAML `repeat`/`until` — see [PlateSolve.md](PlateSolve.md)). All of that step's fields (`role`, `mountRole`, `exposureSeconds`, `toleranceArcsec`, `maxAttempts`, ...) are plain scalars, so — unlike the calibration sweep case above — there's no list-valued-parameter obstacle to supplying them through `run_script`'s ordinary `parameters` mechanism. A canonical built-in script (e.g. `plate_solve_rig.yaml`) containing one `PlateSolveStep`, invoked via the already-kept `run_script`/`manage_script_run` tools, does everything the two dropped tools did — including the "until precision" retry loop, via `toleranceArcsec` — without a dedicated tool.

### Frames (was 5 tools → 2)

| Tool | Replaces | Signature |
|---|---|---|
| `frames` | `list_frames`, `get_frame_metadata` | `(action: "list"\|"get", frame_id?, ...list filters)` |
| `manage_frame` | `confirm_frame_transfer`, `delete_frame`, `purge_transferred_frames` | `(action: "confirm_transfer"\|"delete"\|"purge", frame_id?, older_than_days?)` |

### Events (unchanged — 1 tool)

| Tool | Notes |
|---|---|
| `get_events` | Unchanged. Confirmed (INDIMCP-114) that `get_events(stream="messages", device=...)` already subsumes `list_indi_messages` — same durable event log, same filter — so `list_indi_messages` is dropped with no replacement, not folded into `indi_property`/`list_indi_drivers`. |

## Tally

| Group | Before | After |
|---|---|---|
| INDI infrastructure | 15 | 5 |
| Configuration entities | 16 | 4 |
| Direct device actions | 15 | 5 |
| Script run control | 4 | 1 |
| Calibration sweeps | 6 | 2 |
| Plate solving | 5 | 2 |
| Frames | 5 | 2 |
| Events | 1 | 1 |
| **Total** | **~67*** | **~22** |

\* Sums to slightly less than 68 because the exact split between `get_events`/`list_indi_messages` is pending confirmation (see Events, above).

## Decisions

- **Every `action`/`kind`/`component`/`scope` discriminator in the tables above is a typed enum, not a free-form string.** This is what keeps per-call parameter validation meaningful — a client (or the model) can see the closed set of valid values from the tool schema itself rather than having to infer them from a description. `camera_action`'s `action` values are `"cool"|"cooler_on"|"cooler_off"|"abort_exposure"|"capture_frame"`, matching every other discriminator's pipe-separated set above.
- **Rollout is incremental, one group per PR**, not a single refactor. Each group in this doc is independently shippable and independently consumable by INDIMCPKit's translation layer, so each has its own tracked todo rather than one monolithic implementation task:
  - INDI infrastructure — INDIMCP-114
  - Configuration entities — INDIMCP-115
  - Direct device actions — INDIMCP-116
  - Script run control — INDIMCP-117
  - Calibration sweeps — INDIMCP-118
  - Plate solving — INDIMCP-119 (depends on INDIMCP-121, authoring the canonical script)
  - Frames — INDIMCP-120
  - Author canonical `plate_solve_rig.yaml` script — INDIMCP-121

  INDIMCP-113 itself now tracks the umbrella (`dependsOn` all of the above) rather than being implemented directly.

## Open decisions (not yet finalized)

1. **Whether pure-read tools (`list_config`, `configuration` with `action="get"`, `list_indi_drivers`, `frames` list mode, etc.) should additionally move to MCP *resources* instead of tools.**

   Resources (`resources/list`/`resources/read`, optionally URI-templated per-id and subscribable via `resources/subscribe` + `notifications/resources/updated`) are a separate MCP primitive from tools. The protocol-level difference that matters: tools are designed to be invoked by the model as part of its own reasoning (`tools/call`, mid-task, model-initiated); nothing in the spec requires the model to be the one deciding to read a resource, or even to know one exists, unless the client surfaces it into context.

   This question splits differently for the two consumers of this server:
   - **INDIMCPKit** is a deterministic Swift SDK — `Mount.park()` calls a fixed translation layer, not an LLM picking from a tool list. Moving reads to resources costs nothing here (no tool-selection is happening on this path, and the kit's MCP client library calls `resources/read` instead of `tools/call` at the same implementation cost either way). The tool-count motivation for this whole document is irrelevant on this path.
   - **A direct LLM/agent client** (Claude Desktop, Claude Code, or another agent talking straight to the Pi's streamable-http endpoint) is where the tool-count motivation actually lives, and where the risk is real: as of current behavior in Claude's own apps, MCP resources are mostly surfaced to the *human user* as something they manually attach, not something the model autonomously decides to fetch mid-conversation the way it decides to call a tool. If a user asks "what rigs do I have configured?" and that data now lives behind a resource nothing has attached, the assistant either doesn't know to look or the user has to manually attach it first — worse than today, even though it's a net win for tool-list size in the abstract.

   **Validation plan, two separate checks:** MCP Inspector confirms *protocol correctness* (`resources/list`/`resources/read` implemented right, templates resolve) but says nothing about autonomous-fetch behavior, since a human always explicitly triggers reads there. The actual production host (whatever ends up pointed at the Pi's streamable-http endpoint for conversational use) needs to be separately checked for whether it lists resources into the model's context automatically, requires manual user attachment, or exposes an implicit "read this resource" affordance the model can call on its own — this is host-specific behavior the MCP spec doesn't pin down, so it has to be observed rather than assumed.

   **A reason to want this independent of tool count:** subscriptions are something tools structurally can't offer. If `configuration`'s `save` action changes a rig config, a resource-based `rig` could push `notifications/resources/updated` to a subscribed client, letting a live UI auto-refresh without polling — worth it only if a real client actually uses subscriptions, which again needs confirming rather than assuming.

   **Current lean:** don't move these to resources for the agent-facing path unless autonomous-fetch behavior has actually been observed in the host being deployed against — the downside (assistant can't look things up unprompted) outweighs the upside (fewer tools) for a "control your telescope by talking to it" use case. For INDIMCPKit, the choice is close to free either way, so it shouldn't be the deciding factor. Still open pending that observation.

## Implications for INDIMCPKit (IMCPKIT-52)

INDIMCPKit's user-facing device API (`Mount`, `Camera`, `FilterWheel`, `Focuser`) should not change shape from this refactor — e.g. `Mount.park()` keeps its existing call signature. Only the internal translation layer that turns that call into an MCP tool invocation changes, e.g. from calling a dedicated `park` tool to calling `mount_action(rig_id, action: "park")`. The tables above are the authoritative mapping of old tool name → new tool name/action value for that translation layer to implement against, once the open decisions section is resolved.

One mapping is not a tool→tool rename: whatever in INDIMCPKit currently calls the rig-based `plate_solve`/`plate_solve_until_precision` tools needs to instead call `run_script`/`manage_script_run` against the fixed `plate_solve_rig.yaml` script id (authored by INDIMCP-121), passing its parameters (`toleranceArcsec`, `maxAttempts`, ...) through `run_script`'s `parameters` argument rather than as direct tool arguments. `plate_solve_uploaded_frame` is unaffected — it stays a direct tool call.
