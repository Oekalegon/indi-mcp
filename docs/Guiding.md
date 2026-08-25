# Guiding design: connecting PHD2's Event Monitoring API (INDIMCP-26)

How PHD2 gets wired into indi-mcp: what PHD2's Event Monitoring/Server API actually looks like,
how this server connects to it as a client, how PHD2 events/state become MCP resources and
tools, how guiding control (start/stop/dither/settle) is exposed, error handling, and how this
coexists with INDI device management. This document is the design; implementation is expected
to follow as one or more separate todos (see "Open questions and follow-up work" below).

## What PHD2 actually exposes

PHD2 is a separate, independent application (not an INDI driver, not spawned by this server)
that already knows how to guide: it connects to a guide camera and a mount on its own — via
its own INDI client connection, or ASCOM, or an on-camera guide port — using whatever
equipment profile the operator configured *inside PHD2 itself*. What this server integrates
with is PHD2's **Event Monitoring/Server API**, a TCP control channel PHD2 exposes for exactly
this purpose (source: [OpenPHDGuiding/phd2 wiki, "EventMonitoring"](https://github.com/OpenPHDGuiding/phd2/wiki/EventMonitoring)):

* **Transport:** a plain TCP socket, port **4400** by default. A second/third simultaneous PHD2
  instance (multiple rigs on one machine) listens on 4401/4402/... — PHD2 numbers instances
  from 1, and the Nth instance listens on `4400 + (N - 1)`.
* **Framing:** one JSON object per line, each line terminated `CR LF`. No length prefix, no
  other framing — a line-buffered reader is enough.
* **Two message families share the same socket, distinguished by shape:**
  * **Events** — asynchronous, PHD2-initiated. Every event object has an `Event` field (e.g.
    `"GuideStep"`, `"SettleDone"`) plus `Timestamp`, `Host`, `Inst`, and event-specific fields.
    Not correlated to any request — the client never asked for these, they arrive whenever
    PHD2's own state changes.
  * **Requests/responses** — JSON-RPC 2.0, client-initiated. The client sends
    `{"method": "...", "params": ..., "id": N}`; PHD2 replies
    `{"jsonrpc": "2.0", "result": ..., "id": N}` or
    `{"jsonrpc": "2.0", "error": {"code": ..., "message": ...}, "id": N}` on the same socket,
    correlated by `id`. Nothing stops an event and a response from interleaving on the wire —
    a client has to distinguish them by shape (`"Event"` key present → event; `"id"` present
    with no `"Event"` → response), not by any framing-level separation.
* **On connect,** PHD2 immediately sends a `Version` event, then whatever state events apply
  (`LockPositionSet`, `StarSelected`, `CalibrationComplete`, ...), a
  `StartGuiding`/`StartCalibration`/`Paused` event if applicable, and finally an `AppState`
  event — i.e. a new client is brought up to date on current state as a burst of events, not by
  a single "give me everything" call. `get_app_state` (and the other `get_*` methods) are still
  needed to query a fixed point in time on demand — polling — since the events above only
  arrive at connect time or on a state *change* thereafter.

**Event types** (name → key fields, from the same source):

| Event | Key fields |
|---|---|
| `Version` | `PHDVersion`, `PHDSubver`, `MsgVersion`, `OverlapSupport` |
| `AppState` | `State` (`Stopped`, `Selected`, `Calibrating`, `Guiding`, `LostLock`, `Paused`, `Looping`) |
| `LockPositionSet` / `LockPositionLost` / `LockPositionShiftLimitReached` | `X`, `Y` (first only) |
| `StarSelected` | `X`, `Y` |
| `StartCalibration` / `Calibrating` / `CalibrationComplete` / `CalibrationDataFlipped` | `Mount`, plus `dir`/`dist`/`dx`/`dy`/`pos`/`step`/`State` for `Calibrating` |
| `CalibrationFailed` | `Reason` |
| `StartGuiding` / `GuidingStopped` / `Paused` / `Resumed` / `SettleBegin` / `LoopingExposuresStopped` / `ConfigurationChange` | none |
| `GuideStep` | `Frame`, `Time`, `Mount`, `dx`, `dy`, `RADistanceRaw`, `DECDistanceRaw`, `RADuration`, `RADirection`, `DECDuration`, `DECDirection`, `StarMass`, `SNR`, `HFD`, `AvgDist`, `RALimited`, `DecLimited`, `ErrorCode` |
| `GuidingDithered` | `dx`, `dy` |
| `LoopingExposures` | `Frame` |
| `Settling` | `Distance`, `Time`, `SettleTime`, `StarLocked` |
| `SettleDone` | `Status`, `Error`, `TotalFrames`, `DroppedFrames` |
| `StarLost` | `Frame`, `Time`, `StarMass`, `SNR`, `AvgDist`, `ErrorCode`, `Status` |
| `Alert` | `Msg`, `Type` (`info`/`question`/`warning`/`error`) |
| `GuideParamChange` | `Name`, `Value` |

**Relevant control (RPC) methods:** `get_connected`/`set_connected`, `get_app_state`,
`get_paused`/`set_paused`, `get_calibrated`, `guide` (start guiding, takes a `settle` object and
optional `recalibrate`), `dither` (takes `amount`, `raOnly`, `settle`), `stop_capture`, `loop`,
`clear_calibration`, `flip_calibration`, plus a long tail of profile/equipment/algorithm
getters/setters not relevant to this design's scope (see "Not in scope" below). The `settle`
object shared by `guide`/`dither` is `{"pixels": <float>, "time": <seconds>, "timeout":
<seconds>}` — "stop settling once within `pixels` for `time` consecutive seconds, or fail after
`timeout`" — and is exactly the shape `SettleBegin`/`Settling`/`SettleDone` report back on.

## Architecture: a new client module, mirroring `indi_messaging.py`

**Decision: a new `phd2_client.py` module, structurally parallel to `indi_messaging.py`, not a
new capability bolted onto it.**

`indi_messaging.py` already solves almost exactly this problem shape — own a persistent TCP
client connection to an external process, translate its wire protocol into this server's
`kind`-tagged event envelope, publish to a subscribable stream, expose a small set of
query/command functions — just for `indiserver`'s XML protocol instead of PHD2's JSON one. The
same shape applies here: connect, read a stream of async messages, translate, publish; send
commands, correlate replies. Reusing that shape (a dedicated module with `start_*`/`stop_*`/
`get_status` plus a background read loop) rather than trying to route PHD2 traffic through
`indi_messaging.py` itself keeps the two genuinely separate wire protocols and genuinely
separate external processes (`indiserver` vs PHD2) from being coupled in one module — connecting
INDI messaging and disconnecting PHD2 (or vice versa) should be entirely independent operations,
matching how they're independent processes with independent lifecycles already.

**Not a new INDI device role, not routed through `indi_messaging`/`rig_store`'s device
model.** PHD2 is not an INDI device — it doesn't show up in `indi_messaging.list_devices()`,
and `rig_store`'s `guideCamera`/`guideTelescope` roles (already in the schema, see
[RigSchema.md](RigSchema.md)) describe the *physical guiding optics*, which is informational
metadata about the rig — not a live INDI property source and not what this design connects to.
See "Coexistence with INDI device management" below for why that's a deliberate boundary, not
an oversight.

### Connection lifecycle

Same shape as `indi_messaging.start_messaging`/`stop_messaging`/`get_status`
(`manage_indi_infra`'s `component="messaging"` today), plus the same connect-time
`clear_messages()`-style reset:

* `phd2_client.start(host: str = "localhost", port: int = 4400) -> GuidingConnectionStatus` —
  opens the TCP socket (`asyncio.open_connection`), starts the background read-loop task, and
  waits (bounded, same `_STARTUP_POLL_TIMEOUT` convention as `indi_messaging`/`indi_server`)
  for the connect-time `Version` event to confirm this is actually talking to PHD2 and not a
  dead port. Publishes a `connectionMade`/`connectionLost` pair to the *existing*
  `indi://mcp-server/connection` stream with `target="phd2"` (INDIMCP-57's convention — see
  Design.md's "Event streams" section) exactly like `indi_server`/`indi_messaging` already do
  for their own targets, rather than inventing a separate connection-status channel for PHD2.
* `phd2_client.stop() -> GuidingConnectionStatus` — closes the socket, cancels the read loop,
  fails any requests still awaiting a response (see below), publishes `connectionLost`.
* `phd2_client.get_status() -> GuidingConnectionStatus` — `{"running": bool, "host": str, "port":
  int}`, matching `MessagingStatus`'s own shape.

Restart-on-already-running semantics match `indi_messaging.start_messaging`: calling `start`
while already connected reconnects rather than erroring, same rationale (a caller shouldn't
have to know or check current state first).

**This server never starts or stops the PHD2 *process* itself** (unlike `indi_server.py`, which
does launch/kill `indiserver`) — PHD2 is normally a long-running application the operator starts
themselves (headless via `phd2 -i N`, or with its GUI), already configured with its own
equipment profile before this server ever connects to it. Managing the PHD2 process's own
lifecycle is a plausible future addition (mirroring `indi_server.py`) but is explicitly out of
scope here — this design only ever assumes PHD2's control socket is either reachable or not, and
reports which, exactly as `get_indi_status` already treats `indiserver`.

### Request/response correlation

The RPC side needs a way to match an async response to the request that triggered it, since the
read loop and the tool call issuing the request run concurrently. A monotonically increasing
request id plus a `dict[int, asyncio.Future]` — the read loop resolves the matching future when
a response with that `id` arrives; `_send_request(method, params) -> Any` creates the future,
writes the request line, and awaits it with a timeout (`INDI_MCP_PHD2_RPC_TIMEOUT_SECONDS`,
default `10`, same per-module env-var convention as `plate_solver.py`'s constants — see
[PlateSolve.md](PlateSolve.md#config)). A response carrying `"error"` raises rather than
returning — same "the wire value is the actual source of truth" spirit as
`indi_messaging._coerce_property_state`, just surfaced as an exception instead of a passthrough
since an RPC error genuinely means the requested action didn't happen. Every future still
pending when the connection drops (`stop()`, or the socket closing unexpectedly) is
cancelled/failed rather than left hanging forever.

### Event translation

Every PHD2 `Event` becomes one `kind`-tagged dict, following the same convention
`indi_messaging.IndiEvent`/`event_streams.ConnectionEvent`/scripting events already share (see
Design.md's "MCP message format"). Unlike the INDI messaging layer, there's no second `type`
axis here — INDI's `kind`/`type` split exists because one wire concept (`def*Vector`) always
pairs an action with a data type; PHD2's events don't have an equivalent second dimension, so
this follows the scripting/connection streams' simpler `kind`-only shape instead, not the
messaging layer's `kind`+`type` one.

`kind` is `"guiding" + EventName` (e.g. PHD2's `GuideStep` → `guidingStep`, `SettleDone` →
`guidingSettleDone`, `StarLost` → `guidingStarLost`) — a mechanical prefix rather than a
hand-picked name per event, so a new PHD2 event type this design didn't anticipate still
translates predictably instead of being silently dropped. Every event also carries `timestamp`
(from PHD2's own `Timestamp`, not a local clock — preserves PHD2's own event ordering across a
network hop) and passes through its event-specific fields verbatim, camelCased to match this
project's existing JSON convention (`RADistanceRaw` → `raDistanceRaw`, `SNR` → `snr`, etc.) —
same transformation `indi_messaging` already applies to INDI's own PascalCase field names.

**Sketch:**

```json
{
  "kind": "guidingStep",
  "frame": 142,
  "time": 87.3,
  "mount": "Mount",
  "dx": 0.12,
  "dy": -0.08,
  "raDistanceRaw": 0.14,
  "decDistanceRaw": -0.09,
  "raDuration": 320,
  "raDirection": "East",
  "decDuration": 0,
  "decDirection": null,
  "starMass": 1842.0,
  "snr": 24.1,
  "hfd": 2.3,
  "avgDist": 0.41,
  "raLimited": false,
  "decLimited": false,
  "errorCode": 0,
  "timestamp": "2026-08-25T21:04:11.302Z"
}
```

```json
{
  "kind": "guidingSettleDone",
  "status": 0,
  "error": null,
  "totalFrames": 12,
  "droppedFrames": 0,
  "timestamp": "2026-08-25T21:05:02.118Z"
}
```

```json
{
  "kind": "guidingAlert",
  "message": "Star lost: SNR too low",
  "alertType": "error",
  "timestamp": "2026-08-25T21:06:44.910Z"
}
```

(`Alert`'s `Msg`/`Type` are renamed `message`/`alertType` rather than a literal camelCase of
`Msg`/`Type` — `message` matches every other event stream's own field name for free text
(`IndiEvent.message`, `ConnectionEvent.message`), and a bare `type` would collide with the
messaging layer's unrelated `type` field if a client ever handled both streams with shared
field-name assumptions.)

### Event stream: `indi://mcp-server/guiding`

A fourth member of the existing `indi://mcp-server/*` family (alongside `scripts` and
`connection` — see Design.md's "Event streams"), not a new top-level stream and not folded into
`indi://mcp-server/scripts` — guiding activity isn't a script run (no `runId`, no
pause/resume/cancel machinery, continuous rather than one-shot) and mixing it into the scripts
stream would force every scripts subscriber to filter out unrelated guiding chatter, the same
reasoning Design.md already gives for keeping `scripts` and `connection` separate from each
other. Same subscription mechanism (`resources/subscribe`/`notifications/resources/updated`/
`resources/read`), same rolling in-memory buffer + durable event-log write via
`event_streams.publish_*`-style plumbing (a new `publish_guiding_event`, `read_guiding`,
`guiding_uri` alongside the existing three), same "best-effort, live-only; the event log is the
resilience mechanism" caveat. No per-scope variant (`indi://mcp-server/guiding/{something}`) —
unlike `scripts`/`{runId}` or `connection`/`{target}`, there's exactly one guiding session at a
time (one PHD2 connection), so there's nothing to scope by.

`event_log.py`'s `Stream` literal gains `"guiding"` alongside the existing
`"messages"`/`"scripts"`/`"connection"` — the same table, same retention (1 day), same
`get_events` catch-up query, just a new value in an existing column.

### Tool surface

Two tools, matching this project's existing `action`-discriminated, per-concern grouping
(`manage_indi_infra`, `mount_action`, `camera_action`, ... — see
[ToolSurfaceRedesign.md](ToolSurfaceRedesign.md)) rather than one tool per PHD2 method:

**`manage_guiding(action: Literal["connect", "disconnect"], host: str | None = None, port: int
| None = None) -> GuidingConnectionStatus`** — owns the TCP connection lifecycle only
(`phd2_client.start`/`stop`). `host`/`port` only valid with `action="connect"`, defaulting to
`"localhost"`/`4400` — mirrors `manage_indi_infra`'s `component="messaging"` branch exactly,
including the same validation-raises-`ValueError`-on-irrelevant-argument discipline.

**`get_guiding_status() -> GuidingStatus`** — a single combined snapshot, not split
per-concern the way `get_indi_status` is per-`component`, because there's only one thing to ask
PHD2 about (unlike INDI, which has a server/driver/messaging split with genuinely independent
running-or-not states). Calls `get_connected`/`get_app_state`/`get_paused` and reports:

```json
{
  "connectionRunning": true,
  "host": "localhost",
  "port": 4400,
  "phd2Connected": true,
  "appState": "Guiding",
  "paused": false
}
```

`connectionRunning` is this server's own TCP link to PHD2 (matches `MessagingStatus.running`'s
naming); `phd2Connected` is PHD2's *own* connection to its configured guide camera/mount
(PHD2's `get_connected`) — genuinely different questions, both worth surfacing separately rather
than collapsing into one boolean, the same way `IndiServerStatus`/`MessagingStatus` already stay
separate rather than one flag conflating "this server reached `indiserver`" with "`indiserver`
reached the driver."  If `connectionRunning` is `false`, the rest of the fields are `null` — no
RPC calls are made against a socket that isn't open.

**`guiding_action(action: Literal["start", "stop", "pause", "resume", "dither",
"clear_calibration", "flip_calibration"], settlePixels: float | None = None,
settleTimeSeconds: float | None = None, settleTimeoutSeconds: float | None = None,
recalibrate: bool = False, ditherAmount: float | None = None, ditherRaOnly: bool = False) ->
GuidingActionAck`** — the actual guiding control surface, covering the todo's explicit
"start/stop/dither/settle" ask:

* `action="start"` → PHD2's `guide` method. `settlePixels`/`settleTimeSeconds`/
  `settleTimeoutSeconds` are required together (PHD2's `settle` object has no meaningful
  partial form); `recalibrate` optional, default `False`.
* `action="stop"` → PHD2's `stop_capture`. No parameters.
* `action="pause"`/`"resume"` → PHD2's `set_paused` with `true`/`false`. No parameters — PHD2's
  own `"full"` pause-type argument (pause capture entirely vs. just guide corrections) is left
  at PHD2's default rather than exposed, since this server has no scripted use for the
  distinction yet; can be added as an optional parameter later without breaking this shape.
* `action="dither"` → PHD2's `dither`. `ditherAmount` required; `ditherRaOnly` optional
  (default `False`); same `settlePixels`/`settleTimeSeconds`/`settleTimeoutSeconds` triple
  required together, since `dither`'s own `settle` object is the same shape as `guide`'s.
* `action="clear_calibration"`/`"flip_calibration"` → PHD2's methods of the same name. No
  parameters.

Every parameter not valid for the given `action` raises `ValueError` if given — same
`_CAMERA_ACTION_ALLOWED_PARAMS`/`_MOUNT_ACTION_PARAMS`-style per-action allow-list pattern
already used by every other `*_action` tool in `server.py`, including the
`requiredParamsByAction` schema-metadata convention for a schema-reading client.

**Deliberately synchronous acknowledgement, not a `runId`.** Unlike `run_script`, there's no
run to track — `guiding_action` calls the matching PHD2 RPC method and returns once PHD2's own
response comes back (bounded by `INDI_MCP_PHD2_RPC_TIMEOUT_SECONDS`), returning
`{"kind": "guidingStarted", ...}`/`{"kind": "guidingDitherStarted", ...}`/etc. That
acknowledgement means only "PHD2 accepted the request," not "guiding is now stable" —
`action="start"`/`"dither"` both trigger a settle cycle that plays out asynchronously over the
`indi://mcp-server/guiding` stream (`guidingSettleBegin` → `guidingSettling`* →
`guidingSettleDone`) exactly the way PHD2 itself reports it; a caller that needs to know
*when* guiding actually stabilized watches that stream (or polls `get_guiding_status`'s
`appState`), the same "starts immediately, watch the stream or poll status for the real
outcome" pattern `run_script`/`manage_script_run` already established for long-running INDI
operations — reusing that shape here rather than inventing a second one, even though guiding
has no `runId`/pause/cancel of its own.

### Coexistence with INDI device management

This is the crux of "how it coexists with INDI device management," and the answer is:
**almost entirely independently, by design.**

PHD2 and this server's own `indi_messaging.py` client are two *separate* INDI clients, each
capable of connecting to the same `indiserver` instance simultaneously — INDI's protocol
supports multiple concurrent clients against the same driver by design (each client gets its
own TCP connection to `indiserver`, which fans out `def*Vector`/`set*Vector` broadcasts to all
of them). PHD2 typically runs on the same Raspberry Pi as this server (see
[Deployment.md](Deployment.md)) and connects to `indiserver` on `localhost:7624` using its own,
separately-configured equipment profile — this server does not start, configure, or proxy that
connection in any way. Concretely:

* **This server never tells PHD2 which camera/mount to use.** That's entirely PHD2's own
  configuration, set up once by the operator inside PHD2 (or scripted against PHD2's own
  profile-management RPC methods, out of scope here — see "Not in scope" below).
* **`rig_store`'s `guideCamera`/`guideTelescope` components stay purely descriptive.** They
  already exist (see [RigSchema.md](RigSchema.md)) to record *what hardware* a rig's guiding
  train is, for documentation/`rig_diagnostics` purposes — they are not, and don't become,
  wiring instructions for PHD2. A rig's `guideCamera.device` and whatever INDI device PHD2 is
  actually using are two independently-maintained facts that happen to usually describe the
  same physical camera; this design doesn't add any code that assumes or enforces they agree.
  Cross-checking `rig_store`'s guide-camera metadata against PHD2's live equipment (PHD2 exposes
  `get_current_equipment`) is a plausible future `rig_diagnostics`-style addition, not something
  this design needs to resolve now.
* **No shared connection, no shared event stream.** `indi_messaging`'s connection to
  `indiserver` and `phd2_client`'s connection to PHD2 start, stop, and fail completely
  independently — losing the PHD2 link doesn't affect `indi://messages`, and restarting
  `indiserver` (`manage_indi_infra`, `component="server"`) doesn't touch PHD2's own separate
  INDI client connection at all (indiserver just sees PHD2 as one more connected client,
  reconnecting on its own terms).
* **A capture script can still coordinate with guiding**, even without any code-level coupling:
  a `scripts/*.yaml` step sequence (e.g. "dither between subs") calls `guiding_action` the same
  way it calls `camera_action`/`mount_action` today, and can `wait_for` a `guidingSettleDone`
  event on the guiding stream the same way `wait_for` already resolves against
  `propertyUpdate`s on the messaging stream (see [ScriptSchema.md](ScriptSchema.md)) — this is
  future scripting-layer work building *on* this design, not something this design needs to
  implement.

### Not in scope

To keep this design focused on the todo's actual ask (connect PHD2's guiding API; expose
guiding state as resources and guiding control as tools), the following are explicitly deferred,
not silently dropped — each a plausible follow-up todo once the core connection/tool surface
above exists and has real hardware behind it:

* **Managing the PHD2 process itself** (start/stop/restart, mirroring `indi_server.py`) — see
  "Connection lifecycle" above.
* **Calibration-data / equipment-profile management** (`get_calibration_data`,
  `get_current_equipment`, `get_profile`/`set_profile`, ...) — read-only status reporting via
  `get_guiding_status` covers what a script or operator needs to *observe*; changing PHD2's own
  configuration remotely is a separate, later concern.
* **Star-image retrieval** (`get_star_image`) — a plausible future addition alongside frame
  retrieval (see Design.md's "Retrieving frames"), not needed for start/stop/dither/settle.
* **Automatic dithering wired into `capture_light_sequence`-style built-in scripts** — the
  scripting-layer integration sketched above under "Coexistence," left for a script-authoring
  follow-up once `guiding_action`/the guiding stream actually exist to build on.
* **Multiple simultaneous PHD2 instances** (multi-rig setups, PHD2's 4400/4401/4402 numbering)
  — this design's `manage_guiding` takes one `host`/`port` pair for one active connection,
  matching this server's existing one-`indiserver`-connection-at-a-time model
  (`indi_messaging`/`indi_server` are themselves both single-instance).

## Open questions and follow-up work

Per this todo's own description ("expected to spawn multiple sub-todos covering research,
architecture, tool/resource design, and implementation phases") — this document is the
architecture/tool-and-resource design; the following are the implementation-phase follow-ups it
implies, to be filed as separate todos rather than attempted here:

1. **`phd2_client.py` implementation** — the TCP client, read loop, request/response
   correlation, and event translation described above.
2. **`event_streams.py`/`event_log.py` additions** — `publish_guiding_event`/`read_guiding`/
   `guiding_uri`, the new `"guiding"` stream literal, and the `indi://mcp-server/guiding`
   resource registration in `server.py` (mirroring `indi://mcp-server/scripts`'s registration).
3. **`manage_guiding`/`get_guiding_status`/`guiding_action` tool implementation** in `server.py`.
4. **Deployment note** for [Deployment.md](Deployment.md): PHD2 itself needs to be installed and
   have its Event Monitoring server enabled (a checkbox in PHD2's own Guiding settings, "Enable
   Server," not something this server can turn on remotely) — a one-time setup prerequisite the
   same way `solve-field` and its index files are for plate solving.
5. **User documentation** for the guiding tools/stream, once shipped (see Features-1.0.md's
   "User-documentation gaps" list, which this doc's own line item should join).
