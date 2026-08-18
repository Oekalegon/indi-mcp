"""Subscribable `indi://messages` and `indi://mcp-server` event stream resources.

Per `docs/Design.md#event-streams`: `indi://messages` carries raw INDI
protocol traffic (`indi_messaging.IndiEvent`); `indi://mcp-server` carries
everything about this server's own operation instead — script-run progress
(`indi_mcp.event_streams`'s `scripts_uri`, historically its own top-level
`indi://scripts` stream, moved under here by INDIMCP-57) and connection
lifecycle (`connectionMade`/`connectionLost`, INDIMCP-57) for the MCP
server's own link to `indiserver`, the `indiserver` process, and individual
driver processes. All three share the same `kind`-tagged envelope
convention. This module is the broker connecting new events raised by the
messaging/scripting/process-management layers to MCP's
`resources/subscribe` / `notifications/resources/updated` / `resources/read`
mechanism: a small rolling in-memory buffer per stream (read by
`resources/read`), plus a subscriber registry notified whenever a new event
is published.

**The live `resources/subscribe` channel itself is best-effort, live-only**
— matching Design.md exactly: a client that was disconnected when an event
occurred should not assume it received every missed event via this route,
and the in-memory buffers above are bounded and cleared on process restart.
What actually lets a reconnecting client catch up is the separate, durable
SQLite log this module also writes every event to (`event_log.record_event`,
INDIMCP-15) — see `event_log`'s own module docstring and its `get_events`
catch-up query.

**Durable writes go through one bounded queue, not one task per event**
(INDIMCP-59). `record_event` is a blocking `sqlite3` call, and a "chatty"
device can publish many events a second (per `docs/Design.md#event-streams`)
— spawning a fresh `asyncio.to_thread` per event would let an arbitrary
number of threads pile up all contending for the same SQLite write lock,
exhausting the default thread-pool executor every other blocking call in
this process also shares (`frame_store`, `event_log.run_purge_loop`, ...).
A single persistent worker task drains a bounded `asyncio.Queue` one item at
a time instead, so at most one durable write is ever in flight. If the
queue fills (the writer falling behind a sustained burst), the *oldest*
queued event is dropped to make room for the newest — consistent with the
in-memory buffers above, which already drop their oldest entry once full —
rather than growing the queue (and the process's memory) without bound.
Only every Nth drop is actually logged (`_DROP_LOG_INTERVAL`), not every
one: during the one scenario this path exists for (a sustained overload),
logging every single drop would itself pile more small synchronous work
back onto the event loop, working against the whole point of bounding it.
"""

import asyncio
import logging
from collections import deque
from collections.abc import Mapping
from typing import Literal, NamedTuple, Protocol, TypedDict
from urllib.parse import quote

from pydantic import AnyUrl

from indi_mcp import event_log

logger = logging.getLogger(__name__)

__all__ = [
    "ConnectionEvent",
    "clear_messages",
    "connection_uri",
    "drain",
    "is_subscribable_uri",
    "messages_uri",
    "publish_connection_event",
    "publish_message_event",
    "publish_script_event",
    "read_connection",
    "read_messages",
    "read_scripts",
    "scripts_uri",
    "subscribe",
    "unsubscribe",
]


class ConnectionEvent(TypedDict):
    """One connection-lifecycle event, in the same `kind`-tagged envelope convention as
    `indi_messaging.IndiEvent`/`script_runs.ScriptRunStatus` — see `publish_connection_event`
    (INDIMCP-57)."""

    kind: Literal["connectionMade", "connectionLost"]
    target: Literal["server", "indiserver"] | str
    message: str | None
    timestamp: str

_MAX_BUFFERED_EVENTS = 200

_NOTIFY_TIMEOUT_SECONDS = 5.0
"""Ceiling on how long `_notify` waits for one subscriber's `send_resource_updated`.

Without this, a subscriber whose send never raises but also never returns (a half-open
connection, a transport-level stall) would hang `_notify` forever. That used to only cost
that one publish's notification task; since `_schedule_notify` now allows at most one
`_notify` per URI in flight at a time (`_pending_notify_uris`), an unbounded hang here would
starve *every* subscriber of that URI — including healthy ones — until the hang resolves.
Timing out and dropping the offending subscriber, the same way a raised exception already
is, keeps the coalescing gate from being held open indefinitely by one bad connection."""


class _NotifiableSession(Protocol):
    """The one piece of `mcp.server.session.ServerSession` this module needs."""

    async def send_resource_updated(self, uri: AnyUrl) -> None: ...


class _QueuedEvent(NamedTuple):
    """One durable-write job queued for `_record_worker` — see `_schedule_record`."""

    stream: event_log.Stream
    payload: Mapping
    device: str | None
    run_id: str | None
    target: str | None


_messages: deque[Mapping] = deque(maxlen=_MAX_BUFFERED_EVENTS)
_scripts: deque[Mapping] = deque(maxlen=_MAX_BUFFERED_EVENTS)
_connections: deque[Mapping] = deque(maxlen=_MAX_BUFFERED_EVENTS)

_subscribers: dict[str, set[_NotifiableSession]] = {}

_background_tasks: set[asyncio.Task] = set()
"""Strong references to in-flight, one-off notification tasks (`_schedule_notify`).

`asyncio.create_task` results must be held onto somewhere or the task can be
garbage-collected mid-execution — a well-known asyncio footgun. Each task
removes itself once done. Durable-write tasks used to live here too, but
now go through the persistent `_record_worker`/`_record_queue` pair below
instead (INDIMCP-59) — `drain()` waits on both this set and that queue.
"""

_pending_notify_uris: set[str] = set()
"""URIs with a `_notify` task currently in flight — see `_schedule_notify`'s coalescing.

The MCP SDK's Streamable HTTP transport (`mcp.server.streamable_http`) routes every message
for a session — every tool-call response *and* every notification — through one sequential,
session-wide `message_router` task, delivering to a per-target stream bounded at 16
unconsumed messages before `send()` blocks. A device's post-connect burst can easily publish
dozens of `propertyDefinition` events in a few milliseconds; firing one `_notify` task per
event (the old behavior) queues a notification per event too, and once a slow-to-drain
client's queue exceeds that 16-message buffer, `message_router`'s blocked `send()` head-of-
line-blocks *every other message on the session* — including unrelated tool-call responses
like `get_script_status`, which is what made an already-succeeded `connect` script look stuck
forever from the client's side. `_notify` doesn't carry event data anyway — it's just "this
resource changed, go re-read it" — so a client always fetches the *current* window when it
does re-read; collapsing a burst down to at most one in-flight notification per URI loses
nothing observable while keeping the queue nowhere near that 16-message ceiling.
"""

_RECORD_QUEUE_MAXSIZE = 1000
"""Bounds memory if durable writes fall behind a sustained event burst — see module docstring."""

_record_queue: "asyncio.Queue[_QueuedEvent] | None" = None
"""Created lazily (`_ensure_record_worker`) against whichever event loop is actually running,
the same lazy-start style `_schedule_notify`/`_schedule_record` already use — `asyncio.Queue`
binds to the loop of its first `get`/`put` call, so a module-level instance created at import
time (before any loop necessarily exists, and potentially reused across a test suite's several
independent loops) would be wrong. `None` until the first event is ever published."""

_record_worker_task: asyncio.Task | None = None
"""The single persistent task draining `_record_queue` — see `_record_worker`."""

_DROP_LOG_INTERVAL = 100
"""Log only every Nth dropped/deduplicated event during a sustained run of them, not every one.

Logging isn't free (formatting, handler I/O) — during the scenarios these drop paths actually
exist for (`_schedule_record`'s queue overwhelmed by a sustained burst; `publish_message_event`
deduping a run of consecutive-duplicate events, INDIMCP-88), unconditionally logging every drop
would itself add a steady stream of small synchronous work back onto the event loop, working
against the very goal (bounding how much work a burst can pile onto this process) both paths
exist for.
"""

_dropped_event_count = 0
"""Total events dropped by `_schedule_record` since the process started (or the last test reset)."""

_last_message_event: Mapping | None = None
"""The most recently published messaging event, kept only to detect an exact repeat — see
`publish_message_event` (INDIMCP-88). `None` right after startup or `clear_messages()`, so the
first event of a fresh connection is never mistaken for a duplicate of one from a previous
session."""

_duplicate_message_event_count = 0
"""Total consecutive-duplicate messaging events dropped since the process started (or the last
test reset) — see `publish_message_event`."""


async def drain() -> None:
    """Wait for every in-flight notification and queued durable write to finish.

    Meant to be called once, on shutdown (see `server.py`'s `_lifespan`),
    after any periodic work (`event_log.run_purge_loop`) has already been
    cancelled — without this, an event published right before the process
    exits could have its durable write abandoned mid-flight, silently
    losing exactly the kind of event a reconnecting client depends on the
    durable log to still have (see this module's own docstring). Only
    waits on work already scheduled/queued at the moment it's called — a
    publish that happens *during* drain isn't covered, since there's no
    way to know about it in advance; that's an inherent limit of
    fire-and-forget scheduling ending at process exit, not something this
    can close without blocking future publishes indefinitely. Each
    notification task already handles its own errors internally (`_notify`
    drops a failed subscriber), and `_record_worker` logs and swallows a
    failed write, so nothing here needs to re-raise on a failure — the final
    `await _record_worker_task` below matches that same guarantee explicitly
    (see its own comment), rather than assuming cancellation is the only way
    that task can ever end.

    `_record_queue.join()` waits for every currently queued event to be
    *durably written*, not just dequeued — `_record_worker` only calls
    `task_done()` after its write attempt finishes (success or logged
    failure) — so this returns only once the worker has actually caught up,
    then stops the now-idle worker task before returning. Both globals are
    reset to `None` afterwards (whether the worker stopped cleanly or with
    an unexpected exception) — leaving a finished `Task` referenced by
    `_record_worker_task` would mean *any* later `await` of it (a repeat
    `drain()` call, a test's own cleanup, ...) re-raises whatever exception
    it ended with, every time, since awaiting an already-completed `Task`
    doesn't consume that exception.
    """
    global _record_queue, _record_worker_task
    pending = [task for task in _background_tasks if not task.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    if _record_queue is not None:
        await _record_queue.join()
    if _record_worker_task is not None:
        _record_worker_task.cancel()
        try:
            await _record_worker_task
        except asyncio.CancelledError:
            pass
        except Exception:
            # `_record_worker`'s own loop only wraps its write attempt in try/except, not
            # `queue.get()` itself — if the task ever ended some other way (a bug there, not
            # cancellation), it would otherwise still be sitting on an unretrieved exception at
            # this point; re-raising it here would break the "never raises" contract this whole
            # function otherwise documents and tests hold it to.
            logger.exception("Durable-write worker task ended with an unexpected error")
    _record_queue = None
    _record_worker_task = None


def messages_uri(device: str | None) -> str:
    """The `indi://messages` resource URI, scoped to `device` if given.

    `device` is percent-encoded (`safe=""`) per RFC 6570 URI-template rules
    for substituted values: an unencoded `/` in a device name would add an
    extra path segment that the single-segment `indi://messages/{device}`
    resource template (see `server.py`) can never match, silently making
    that device's scoped stream unreachable via `resources/read`. A
    compliant client subscribing to this scoped resource is expected to
    encode the value the same way when building the URI it subscribes to,
    so the two sides agree on the same string.
    """
    return f"indi://messages/{quote(device, safe='')}" if device else "indi://messages"


def scripts_uri(run_id: str | None) -> str:
    """The `indi://mcp-server/scripts` resource URI, scoped to `run_id` if given (see
    `messages_uri`). Renamed from the top-level `indi://scripts` by INDIMCP-57 — see this
    module's docstring."""
    return (
        f"indi://mcp-server/scripts/{quote(run_id, safe='')}"
        if run_id
        else "indi://mcp-server/scripts"
    )


def connection_uri(target: str | None) -> str:
    """The `indi://mcp-server/connection` resource URI, scoped to `target` if given (see
    `messages_uri`). `target` is one of `"server"` (this server's own link to `indiserver`),
    `"indiserver"` (the `indiserver` process), or a driver's catalog label (an individual
    driver process) — see `publish_connection_event`."""
    return (
        f"indi://mcp-server/connection/{quote(target, safe='')}"
        if target
        else "indi://mcp-server/connection"
    )


_UNSCOPED_URIS = ("indi://messages", "indi://mcp-server/scripts", "indi://mcp-server/connection")
_SCOPED_PREFIXES = (
    "indi://messages/",
    "indi://mcp-server/scripts/",
    "indi://mcp-server/connection/",
)


def is_subscribable_uri(uri: str) -> bool:
    """Whether `uri` is one of the resources this module actually publishes to.

    Checks the *shape* advertised by the `indi://messages`/`indi://messages/{device}`/
    `indi://mcp-server/scripts`/`indi://mcp-server/scripts/{runId}`/
    `indi://mcp-server/connection`/`indi://mcp-server/connection/{target}` resources (see
    `server.py`) — a single non-empty scope segment with no further `/` — not whether that
    particular device/run/target currently exists. Subscribing ahead of a device connecting or
    a run starting is expected and should still succeed; this only rejects URIs this module can
    never publish an update to at all (a typo like `indi://message`, or an unrelated resource
    like `foo://bar`), which would otherwise register a subscription that silently never fires.
    """
    if uri in _UNSCOPED_URIS:
        return True
    for prefix in _SCOPED_PREFIXES:
        if uri.startswith(prefix):
            scope = uri[len(prefix) :]
            return bool(scope) and "/" not in scope
    return False


async def _notify(uri: str) -> None:
    """Send `notifications/resources/updated` for `uri` to every current subscriber.

    A subscriber that fails to notify (e.g. its connection just dropped) is
    dropped from the registry rather than left to fail again on every future
    event — this is exactly the kind of client the "best-effort" channel is
    allowed to lose events for; the durable catch-up path is `get_events`
    (INDIMCP-15), not this one. `uri` is parsed into an `AnyUrl` once, outside
    the per-subscriber loop: it's the same value for every subscriber, and
    parsing it inside the loop's `try` would misattribute a genuine
    URI-construction failure as every subscriber's connection having failed,
    dropping them all rather than surfacing the real bug. Each send is bounded by
    `_NOTIFY_TIMEOUT_SECONDS` — see that constant's docstring for why a hang here is now
    worse than it used to be, now that `_schedule_notify` coalesces per URI.
    """
    subscribers = _subscribers.get(uri)
    if not subscribers:
        return
    parsed_uri = AnyUrl(uri)
    for session in list(subscribers):
        try:
            await asyncio.wait_for(
                session.send_resource_updated(parsed_uri), timeout=_NOTIFY_TIMEOUT_SECONDS
            )
        except Exception:
            logger.exception("Failed to notify subscriber of %s; dropping it", uri)
            subscribers.discard(session)
    if not subscribers:
        _subscribers.pop(uri, None)


def _schedule_notify(uri: str) -> None:
    """Fire-and-forget `_notify(uri)` from a synchronous call site, coalescing bursts.

    Publishing happens from both async contexts (`indi_messaging.rxevent`)
    and sync ones (`script_runs`'s `on_progress` callback, `pause_script`),
    so this never awaits directly — it schedules a task on whatever loop is
    currently running, which is always the case at every real call site
    (an MCP tool/notification handler, or a task already running under one).
    Skipped entirely when nobody is subscribed to `uri`, the common case.

    Also skipped if a `_notify` for this exact `uri` is already in flight
    (`_pending_notify_uris`) — seeing `uri` again before that one has even
    been delivered means a subscriber will re-read the current window
    (which already reflects every publish so far) as soon as it lands, so a
    second notification would only tell it something it's about to find out
    anyway. See `_pending_notify_uris`'s own docstring for why this matters
    well beyond just saving redundant work.
    """
    if uri not in _subscribers or uri in _pending_notify_uris:
        return
    _pending_notify_uris.add(uri)
    task = asyncio.create_task(_notify_and_clear_pending(uri))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _notify_and_clear_pending(uri: str) -> None:
    """Runs `_notify(uri)`, then clears `uri` from `_pending_notify_uris` so a later publish
    can schedule a fresh notification — see `_schedule_notify`'s coalescing."""
    try:
        await _notify(uri)
    finally:
        _pending_notify_uris.discard(uri)


async def _record_worker(queue: "asyncio.Queue[_QueuedEvent]") -> None:
    """Durably write queued events one at a time, forever, until cancelled.

    A single persistent worker — not one ephemeral task per event, unlike
    the old fire-and-forget model this replaced (INDIMCP-59) — is what
    actually bounds concurrent SQLite writes to one at a time and bounds how
    many `asyncio.to_thread` worker threads this module can occupy: a
    "chatty" device's `propertyUpdate` burst (see
    `docs/Design.md#event-streams`) no longer risks spawning dozens of
    concurrent write attempts that all contend for the same SQLite write
    lock and can exhaust the default thread-pool executor every other
    blocking call in this process also shares (`frame_store`,
    `event_log.run_purge_loop`, ...). A failed write is logged and
    swallowed — matching the old `_record`'s behavior — so one bad write
    never stops the worker from draining the rest of the queue.
    """
    while True:
        item = await queue.get()
        try:
            await asyncio.to_thread(
                event_log.record_event,
                item.stream,
                item.payload,
                device=item.device,
                run_id=item.run_id,
                target=item.target,
            )
        except Exception:
            logger.exception("Failed to durably record a %s event to the event log", item.stream)
        finally:
            queue.task_done()


def _ensure_record_worker() -> "asyncio.Queue[_QueuedEvent]":
    """Return the durable-write queue, lazily creating it and its worker task if needed.

    Lazy, on-demand creation (rather than an explicit `start`/`stop` pair
    wired into `server.py`'s lifespan) matches `_schedule_notify`'s existing
    style: the first real publish call, on whatever loop is actually
    running, is what brings the queue and its worker to life — see
    `_record_queue`'s own docstring for why the queue specifically can't
    just be a module-level literal instead.
    """
    global _record_queue, _record_worker_task
    if _record_queue is None:
        _record_queue = asyncio.Queue(maxsize=_RECORD_QUEUE_MAXSIZE)
    if _record_worker_task is None or _record_worker_task.done():
        _record_worker_task = asyncio.create_task(_record_worker(_record_queue))
    return _record_queue


def _schedule_record(
    stream: event_log.Stream,
    payload: Mapping,
    *,
    device: str | None,
    run_id: str | None,
    target: str | None = None,
) -> None:
    """Enqueue `event_log.record_event(...)` for the durable-write worker, applying backpressure.

    Unlike `_schedule_notify`, this always runs regardless of whether anyone
    is currently subscribed — durable persistence exists to serve a client
    that reconnects *later* (`event_log.get_events`), not to mirror live
    delivery. If the queue is full (the worker falling behind a sustained
    burst — see `_RECORD_QUEUE_MAXSIZE`), the oldest queued event is dropped
    to make room for this newest one — the same "bounded, newest-biased"
    policy `_messages`/`_scripts`'s `maxlen` deques already apply to the live
    in-memory view, rather than letting the queue (and the process's
    memory) grow without bound. Every drop counts against
    `_dropped_event_count`, but only every `_DROP_LOG_INTERVAL`th one is
    actually logged, so a sustained overload doesn't turn logging itself
    into more of the very load this is meant to bound.
    """
    global _dropped_event_count
    queue = _ensure_record_worker()
    item = _QueuedEvent(stream, payload, device, run_id, target)
    try:
        queue.put_nowait(item)
        return
    except asyncio.QueueFull:
        pass
    try:
        dropped = queue.get_nowait()
        queue.task_done()
    except asyncio.QueueEmpty:
        # By the same single-threaded, no-yield-point-in-between reasoning as below, another
        # producer can't actually have refilled/drained this queue out from under us right
        # here today — but there's no harm in not assuming that never changes.
        dropped = None
    if dropped is not None:
        _dropped_event_count += 1
        if _dropped_event_count % _DROP_LOG_INTERVAL == 1:
            logger.warning(
                "Durable event-log queue is full (maxsize=%d); dropped %d event(s) so far "
                "(most recently a %s event) to make room for new ones",
                _RECORD_QUEUE_MAXSIZE,
                _dropped_event_count,
                dropped.stream,
            )
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        # Same single-threaded reasoning as the QueueEmpty branch above — this can't actually
        # happen today either, but drop rather than spin or block a synchronous,
        # fire-and-forget call site if it ever does.
        _dropped_event_count += 1
        if _dropped_event_count % _DROP_LOG_INTERVAL == 1:
            logger.warning(
                "Durable event-log queue is still full; dropped %d event(s) so far "
                "(most recently a %s event)",
                _dropped_event_count,
                stream,
            )


def publish_message_event(event: Mapping) -> None:
    """Record a messaging-layer event and notify `indi://messages` (and per-device) subscribers.

    Drops `event` if it is field-for-field identical to the immediately preceding one
    (INDIMCP-88). `indipyclient`/`indiserver` have been observed occasionally delivering the
    exact same `set*Vector` twice in a row — same device, property, elements, state, and
    timestamp — even though `indi_messaging._MessagingClient.rxevent` calls this function
    exactly once per event it receives, so the duplication isn't introduced on this side. Left
    unfiltered, each duplicate wastes half the rolling buffer's retention window and doubles the
    durable-write load for no new information, since a byte-for-byte repeat carries nothing a
    client couldn't already see in the first copy. Comparing against the single most recently
    published event (not a per-device history) matches how the duplicates were observed: as
    literal neighbors in the stream, not merely two events that happen to share the same value.

    This also applies to the locally synthesized `propertyCommand` events `send_property`
    publishes, not just events actually received from the wire — intentionally: a repeated
    command is just as wasteful to buffer/persist twice as a repeated wire update, and
    `send_property` stamps a fresh microsecond-precision timestamp on every call, so two
    genuinely separate commands are never mistaken for one duplicate in practice.

    Also durably persisted to the event log — see `_schedule_record`.
    """
    global _last_message_event, _duplicate_message_event_count
    if event == _last_message_event:
        _duplicate_message_event_count += 1
        if _duplicate_message_event_count % _DROP_LOG_INTERVAL == 1:
            logger.debug(
                "Dropped a duplicate messaging event (device=%r, name=%r); %d dropped so far",
                event.get("device"),
                event.get("name"),
                _duplicate_message_event_count,
            )
        return
    _last_message_event = dict(event)
    _messages.appendleft(event)
    device = event.get("device")
    _schedule_record("messages", event, device=device, run_id=None)
    _schedule_notify(messages_uri(None))
    if device:
        _schedule_notify(messages_uri(device))


def publish_script_event(event: Mapping) -> None:
    """Record a scripting-layer event and notify `indi://mcp-server/scripts` (and per-run)
    subscribers.

    Also durably persisted to the event log — see `_schedule_record`.
    """
    _scripts.appendleft(event)
    run_id = event.get("runId")
    _schedule_record("scripts", event, device=None, run_id=run_id)
    _schedule_notify(scripts_uri(None))
    if run_id:
        _schedule_notify(scripts_uri(run_id))


def publish_connection_event(event: Mapping) -> None:
    """Record a connection-lifecycle event and notify `indi://mcp-server/connection` (and
    per-target) subscribers (INDIMCP-57).

    `event["target"]` is one of `"server"` (this server's own TCP link to `indiserver`, sourced
    from `indipyclient`'s local `ConnectionMade`/`ConnectionLost` events —
    `indi_messaging._MessagingClient.rxevent`), `"indiserver"` (the `indiserver` process itself
    — `indi_server.start_server`/`stop_server`), or a driver's catalog label (an individual
    driver process — `indi_driver.start_driver`/`stop_driver`). Unlike `publish_message_event`,
    no duplicate-suppression is applied here: a repeated `connectionLost` for the same target
    (e.g. `indiserver` retrying a failed reconnect every few seconds) is itself meaningful
    information, not noise to collapse away, the same way a chatty device's raw wire traffic
    is not what this stream carries.

    Also durably persisted to the event log — see `_schedule_record`.
    """
    _connections.appendleft(event)
    target = event.get("target")
    _schedule_record("connection", event, device=None, run_id=None, target=target)
    _schedule_notify(connection_uri(None))
    if target:
        _schedule_notify(connection_uri(target))


def clear_messages() -> None:
    """Discard every buffered messaging-layer event.

    Called by `indi_messaging.start_messaging` on (re)connect: this is the
    single source of truth for messaging events (there's no longer a
    separate buffer in `indi_messaging` itself), so starting a fresh session
    clears it here, the same way `_latest_blobs` is cleared alongside it.
    Subscriptions/notifications are untouched — only the rolling read
    buffer is reset. Also resets `_last_message_event` (INDIMCP-88), so the
    first event of the new session is never dropped as a false "duplicate"
    of whatever the previous session's connection last happened to send.
    """
    global _last_message_event
    _messages.clear()
    _last_message_event = None


def read_messages(device: str | None = None) -> dict[str, list[Mapping]]:
    """The rolling window of recent messaging-layer events, newest first.

    Matches what `resources/read` on `indi://messages`/`indi://messages/{device}`
    returns, per `docs/Design.md#event-streams` ("a small JSON envelope with
    a rolling window of recent events").
    """
    events = [e for e in _messages if device is None or e.get("device") == device]
    return {"events": events}


def read_scripts(run_id: str | None = None) -> dict[str, list[Mapping]]:
    """The rolling window of recent scripting-layer events, newest first (see `read_messages`)."""
    events = [e for e in _scripts if run_id is None or e.get("runId") == run_id]
    return {"events": events}


def read_connection(target: str | None = None) -> dict[str, list[Mapping]]:
    """The rolling window of recent connection-lifecycle events, newest first (see
    `read_messages`). INDIMCP-57."""
    events = [e for e in _connections if target is None or e.get("target") == target]
    return {"events": events}


def subscribe(uri: str, session: _NotifiableSession) -> None:
    """Register `session` to be notified whenever a new event is published to `uri`.

    There's no explicit cleanup for a session that disconnects without
    sending `resources/unsubscribe` first — FastMCP gives this module no
    session-close hook to react to. A disconnected session simply lingers
    in `_subscribers` until the next publish tries to notify it, at which
    point `send_resource_updated` fails and `_notify` drops it (see its
    docstring). Best-effort, matching the rest of this module: a live-only
    channel, not a resource one has to explicitly tear down to stay correct.
    """
    _subscribers.setdefault(uri, set()).add(session)


def unsubscribe(uri: str, session: _NotifiableSession) -> None:
    """Undo a prior `subscribe(uri, session)`; a no-op if it wasn't subscribed."""
    subscribers = _subscribers.get(uri)
    if subscribers is None:
        return
    subscribers.discard(session)
    if not subscribers:
        del _subscribers[uri]
