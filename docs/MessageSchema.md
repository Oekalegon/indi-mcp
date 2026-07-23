# Property/Message JSON Payload Schema

This document is the field-by-field reference for the `kind`/`type`-tagged JSON payload
introduced in [Design.md § MCP message format](Design.md#mcp-message-format) (INDIMCP-12) — the
shape of one INDI property definition/update/command/deletion or message as carried inside an MCP
tool result or [event stream](Design.md#event-streams) resource. It also covers the scripting
layer's status envelope (`scriptStarted`/`scriptProgress`/...), already implemented by
`script_runs.py` per [Design.md § Calling scripts and script results](Design.md#calling-scripts-and-script-results),
since both share the same envelope convention and this is the schema's single source of truth for
that convention. It does not cover the YAML script language itself (see
[ScriptSchema.md](ScriptSchema.md)) or the MCP envelope (JSON-RPC, `tools/call`, etc. — fully
specified by the MCP protocol, not by this project).

## Envelope

Every payload is a flat JSON object with a `kind` field naming what it is, and (for the INDI
messaging layer) a `type` field naming the underlying INDI property type. `kind` values in use:

| `kind` | Layer | INDI wire origin | Meaning |
|---|---|---|---|
| `propertyDefinition` | messaging | `def*Vector` | A device has defined (or redefined) a property. |
| `propertyUpdate` | messaging | `set*Vector` | A property's state/element values changed. |
| `propertyCommand` | messaging | `new*Vector` | A command was sent to a property (client → server). |
| `propertyDeleted` | messaging | `delProperty` | A property (or, if `name` is absent, a whole device) was removed. |
| `message` | messaging | `message` | A driver/device log message, not tied to a specific property. |
| `scriptStarted`, `scriptProgress`, `scriptMessage`, `scriptCompleted`, `scriptFailed`, `scriptCancelled`, `scriptPaused`, `scriptResumed`, `scriptPauseRejected` | scripting | — | See [Design.md § Calling scripts and script results](Design.md#calling-scripts-and-script-results); field reference in [Scripting layer envelope](#scripting-layer-envelope) below. `scriptMessage` (INDIMCP-58) is never returned by `get_script_status` — see that row below. |

`type` (messaging `kind`s only) is one of `text`, `number`, `switch`, `light`, `blob`, matching
INDI's `Text`/`Number`/`Switch`/`Light`/`BLOB` vectors. It is `null` for `message` and
`propertyDeleted`, which aren't tied to one property type.

## Messaging-layer fields

| Field | Type | Present on | Description |
|---|---|---|---|
| `kind` | string | all | See table above. |
| `type` | string \| null | all | See above. |
| `device` | string \| null | all | INDI device name. `null` only for a server-wide `message` not attributed to a device. |
| `name` | string \| null | `propertyDefinition`, `propertyUpdate`, `propertyCommand`, `propertyDeleted` | The property (vector) name, e.g. `EQUATORIAL_EOD_COORD`. `null` for `message`, and for a `propertyDeleted` that removes an entire device rather than one property. |
| `label` | string \| null | `propertyDefinition` | Human-readable display label for the property, as reported by the driver. Absent (not just `null`) on `propertyUpdate`/`propertyCommand`/`propertyDeleted` — a property's label doesn't change after definition, so it is not repeated on every update. |
| `group` | string \| null | `propertyDefinition` | The driver-assigned tab/group name (INDI's `group` attribute), e.g. `"Main Control"`. Absent elsewhere, for the same reason as `label`. |
| `perm` | string \| null | `propertyDefinition` | One of `"ro"`, `"wo"`, `"rw"` (INDI's `perm`). `null`/absent for `light` properties, which have no `perm` on the wire. Absent on `propertyUpdate`/`propertyCommand`/`propertyDeleted`. |
| `rule` | string \| null | `propertyDefinition` (type `switch` only) | One of `"OneOfMany"`, `"AtMostOne"`, `"AnyOfMany"` (INDI's switch selection rule). Absent for every other type. |
| `state` | string \| null | `propertyDefinition`, `propertyUpdate` | One of `PropertyState`'s four values below, or the raw string if a driver ever reports something else. `null` on `propertyCommand` (a command's outcome isn't known until the corresponding `propertyUpdate` arrives) and on `propertyDeleted`/`message`. |
| `message` | string \| null | all | For `kind: "message"`, the message text itself. For every other `kind`, an optional human-readable annotation the driver attached to that same def/set/new/del (INDI allows any vector to carry a `message` attribute); `null` when absent. |
| `elements` | array \| null | `propertyDefinition`, `propertyUpdate`, `propertyCommand` | See [Element shapes](#element-shapes) below. `null` for `propertyDeleted`/`message`, and for a `propertyDefinition` of type `blob` (BLOB members have no value to report at definition time — see [BLOBs](#blobs)). |
| `timestamp` | string | all | ISO 8601, UTC, from the underlying INDI event's own timestamp (not wall-clock-at-receipt). |

### `PropertyState` values

`Idle`, `Ok`, `Busy`, `Alert` — INDI's four vector-level states. Distinct from any individual
element's own value (e.g. a `light` element's `Ok`/`Alert`, which lives in `elements`, not here).

### Element shapes

`elements` is a list of per-member objects, one entry per INDI element/member of the vector, in
the order the driver defines them. Every element carries `name` and `value`; which other fields
appear depends on `type` and on whether this is a `propertyDefinition` (full metadata, as INDI's
own `def*Vector` carries) or a `propertyUpdate`/`propertyCommand` (value only, as INDI's own
`set*Vector`/`new*Vector` carry — a property's per-element metadata is fixed at definition time
and not repeated on every update).

| `type` | `propertyDefinition` element shape | `propertyUpdate`/`propertyCommand` element shape |
|---|---|---|
| `text` | `{ "name", "label", "value": string }` | `{ "name", "value": string }` |
| `number` | `{ "name", "label", "value": number, "format", "min": number, "max": number, "step": number }` | `{ "name", "value": number }` |
| `switch` | `{ "name", "label", "value": "On" \| "Off" }` | `{ "name", "value": "On" \| "Off" }` |
| `light` | `{ "name", "label", "value": "Idle" \| "Ok" \| "Busy" \| "Alert" }` | `{ "name", "value": "Idle" \| "Ok" \| "Busy" \| "Alert" }` |
| `blob` | not present — see [BLOBs](#blobs) | `{ "name", "size": number, "format": string }` — see [BLOBs](#blobs) |

`format` (numbers only) is INDI's `printf`-style or sexagesimal format string (e.g. `"%6.2f"`,
`"%10.6m"`), passed through as-is rather than pre-rendered — a client that wants the driver's
intended display precision applies `format` itself; a client that just wants a machine-usable
number uses `value`. `min`/`max`/`step` are the driver-declared numeric bounds; absent (not `0`)
when the driver didn't declare them (INDI treats `min == max == 0` as "no bounds", which this
schema represents as an absent field rather than a misleading `0`).

Example — `defNumberVector` for `EQUATORIAL_EOD_COORD`:

```json
{
  "kind": "propertyDefinition",
  "type": "number",
  "device": "Telescope Simulator",
  "name": "EQUATORIAL_EOD_COORD",
  "label": "Eq. Coordinates",
  "group": "Main Control",
  "perm": "rw",
  "state": "Ok",
  "message": null,
  "elements": [
    { "name": "RA", "label": "RA (hh:mm:ss)", "value": 0.0, "format": "%10.6m", "min": 0, "max": 24, "step": 0 },
    { "name": "DEC", "label": "DEC (dd:mm:ss)", "value": 0.0, "format": "%10.6m", "min": -90, "max": 90, "step": 0 }
  ],
  "timestamp": "2026-07-14T18:50:00Z"
}
```

The corresponding `propertyCommand` sent to slew:

```json
{
  "kind": "propertyCommand",
  "type": "number",
  "device": "Telescope Simulator",
  "name": "EQUATORIAL_EOD_COORD",
  "state": null,
  "message": null,
  "elements": [
    { "name": "RA", "value": 5.5877 },
    { "name": "DEC", "value": -5.3897 }
  ],
  "timestamp": "2026-07-14T18:50:03Z"
}
```

### BLOBs

A BLOB's binary payload is never inlined into a messaging-layer event. Two things drive this:

* **Size.** A FITS frame can be tens of megabytes; buffering the last 200 events (`_MAX_BUFFERED_EVENTS`)
  in memory as base64-inflated JSON would be a very different, much larger, resource commitment on
  a Raspberry Pi than buffering everything else this schema describes.
* **Redundancy.** Captured frames already have a durable, purpose-built path — written to frame
  storage and indexed in SQLite (see [Design.md § Frame storage metadata](Design.md#frame-storage-metadata)
  and [§ Retrieving frames](Design.md#retrieving-frames)) — a second, transient copy riding the
  event stream would duplicate that without adding anything a client actually needs from a live
  feed.

So a `blob` property's `propertyDefinition` carries no `elements` at all (INDI's own
`defBLOBVector` has no value to report either — a BLOB member's size/format are only known once a
value actually arrives), and its `propertyUpdate` carries only size/format metadata per element,
not `value`:

```json
{
  "kind": "propertyUpdate",
  "type": "blob",
  "device": "CCD Simulator",
  "name": "CCD1",
  "state": "Ok",
  "message": null,
  "elements": [
    { "name": "CCD1", "size": 16777216, "format": ".fits" }
  ],
  "timestamp": "2026-07-14T18:50:30Z"
}
```

A client that needs the actual bytes fetches them separately — the same "summary event, fetch the
payload if you need it" split already used for scripts (`scriptCompleted.result` is a summary, not
a frame listing; see [Design.md § Calling scripts and script results](Design.md#calling-scripts-and-script-results)).
For a `propertyUpdate` seen live on the messaging stream, that's a `get_latest_blob(device, name)`
call, keyed by the same `(device, name)` pair and returning the most recent raw bytes received;
for a BLOB captured by a script, it's `list_frames`/frame retrieval, per
[Design.md § Retrieving frames](Design.md#retrieving-frames).

### `propertyDeleted`

```json
{
  "kind": "propertyDeleted",
  "type": null,
  "device": "CCD Simulator",
  "name": "CCD_EXPOSURE",
  "state": null,
  "message": null,
  "elements": null,
  "timestamp": "2026-07-14T18:51:00Z"
}
```

`name` is `null` when the whole device disconnects (INDI's `delProperty` with no property name
attribute), meaning every property previously defined for `device` should be considered gone, not
just one.

### `message`

```json
{
  "kind": "message",
  "type": null,
  "device": "CCD Simulator",
  "name": null,
  "state": null,
  "message": "Camera cooling stabilized at -10.0C",
  "elements": null,
  "timestamp": "2026-07-14T18:52:00Z"
}
```

## Scope: other INDI client events

`indipyclient` (the library `indi_messaging.py` is built on) surfaces several other event/message
types beyond the five `def*Vector`/`set*Vector`/`message`/`delProperty` families this schema
covers. Each is a deliberate, reasoned exclusion, not a silent gap — noted here so the next reader
doesn't have to re-derive why:

| INDI event | Direction | Currently | Why it's not a `kind` here |
|---|---|---|---|
| `getProperties` | client ↔ server | Sent automatically by `indipyclient` (on connect, and periodically if no devices are known yet); a driver's own snooping `getProperties` is likewise handled internally. Not converted by `_to_indi_event` — falls through and is dropped. | Carries no property/message content of its own (no state, no elements, no value) — it's a "please (re-)announce yourself" request, not data. Nothing for an MCP client to act on that isn't already reflected in the `propertyDefinition`s that follow it. |
| `enableBLOB` | client → server only | Sent automatically by `indipyclient` (`_MessagingClient.__init__` sets `enableBLOBdefault = "Also"`; the library re-sends it whenever a new `defBLOBVector`/device appears). | Never received as an event at all — `events.py` has no `enableBLOB` class, and this project never calls `send_getProperties`/`resend_enableBLOB` directly. Purely an internal wire-protocol detail of BLOB delivery, already decided once at startup (see the docstring on `_MessagingClient`); there is nothing for this schema to name. |
| `ConnectionMade` / `ConnectionLost` | local (not from the wire) | Not converted by `_to_indi_event` — falls through and is dropped. Connectivity is instead exposed by *polling* `get_status()` → `MessagingStatus.running`. | Connection lifecycle, not property/message data — deliberately kept out of the `def`/`set`/`message`/`del` vocabulary this schema defines, the same way `MessagingStatus` is already a separate, non-`kind`-tagged shape from `IndiEvent`. If a future push notification for "the INDI connection dropped" is needed (INDIMCP-14's `indi://messages` resource is the natural home for it), it should get its own `kind` (e.g. `connectionLost`) at that time rather than being force-fit into a property-shaped event with `null` `name`/`type`. |
| `VectorTimeOut` | local (not from the wire) | Generated by `indipyclient` when a vector's own `timeout` elapses without a state change; not converted by `_to_indi_event` — falls through and is dropped. | Currently unused by this project — `script_engine`'s `wait_for`/timeout handling (INDIMCP-50) polls property `state` itself rather than consuming this library-level event. Worth revisiting if a future messaging-layer consumer needs "this property timed out" as a pushed event rather than something a poller infers; deferred until there's a concrete caller, same reasoning as `error`'s structured field above. |

## Scripting-layer envelope

Already implemented (INDIMCP-13, `script_runs.py`); recorded here as the other half of the shared
`kind`-tagged convention, per
[Design.md § Event streams](Design.md#event-streams) ("They share the same envelope convention...
so client-side parsing code is uniform across both"). Every scripting-layer payload additionally
carries `runId` and `rigId` (`rigId` is not in Design.md's illustrative JSON — it was added so
progress/results stay traceable to the physical rig a run used, even once a caller only has a
bare `runId` to poll with).

| `kind` | Additional fields |
|---|---|
| `scriptStarted` | `script` (string), `startedAt` (ISO 8601), `pausable` (boolean) |
| `scriptProgress` | `step` (integer), `totalSteps` (integer \| null), `message` (string \| null), `role` (string \| null), `device` (string \| null) |
| `scriptMessage` | `message` (string), `role` (string \| null), `device` (string \| null) — **not** returned by `get_script_status` (see below) |
| `scriptCompleted` | `finishedAt` (ISO 8601), `result` (script-defined summary object) |
| `scriptFailed` | `failedAtStep` (integer), `error` (`{ "message": string }`) |
| `scriptCancelled` | `cancelledAtStep` (integer), `finishedAt` (ISO 8601) |
| `scriptPaused` | `pausedAtStep` (integer) |
| `scriptResumed` | `resumedAtStep` (integer) |
| `scriptPauseRejected` | `reason` (string) |

`scriptProgress`'s `role`/`device` (INDIMCP-58) identify the rig component the step this
progress reports on is acting on — `null` for a step with no single role of its own
(`run_script`, a `count`-based `repeat`). `scriptMessage` is a lower-noise, message-only
sibling of `scriptProgress` for a step handler to report per-invocation activity (e.g.
`capture_frame` reporting the frame it just saved) without that being a numbered progress
step — unlike every other `kind` in this table, it's **not** part of `get_script_status`'s
"current status" response, since it's a point-in-time note rather than run state; a client
only sees it live via `indi://scripts` or by replaying the event log.

`error` is currently just `{ "message": string }` — `script_engine`'s own exceptions
(`ScriptValidationError`/`ScriptPreconditionError`/`ScriptExecutionError`) carry a human-readable
message but no structured detail beyond it yet. A future structured field (e.g. the failing
`propertyState`, sketched as illustrative-only in
[Design.md § Calling scripts and script results](Design.md#calling-scripts-and-script-results))
is deferred until a concrete caller needs to branch on it programmatically rather than just
display it.

`scriptProgress`'s optional `triggeredBy` field (a nested messaging-layer event, per
[Design.md § Event streams](Design.md#event-streams)) is not yet implemented — it's part of the
not-yet-built `indi://scripts` resource (INDIMCP-14), not the current polling-only
`get_script_status`, and its shape when added is exactly the messaging-layer `propertyUpdate`
object documented above.

## Implementation note

`indi_mcp.indi_messaging.IndiEvent` (INDIMCP-5) currently represents `elements` as a flat
`dict[str, str]` of member name → raw value string, rather than the full per-element array shape
defined above — a deliberate first-cut simplification that predates this schema. Bringing it in
line with [Element shapes](#element-shapes) (nested objects, typed values, definition-time
metadata) is follow-up implementation work, tracked separately from this design task.
