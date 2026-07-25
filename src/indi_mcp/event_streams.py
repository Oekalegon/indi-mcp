"""Subscribable `indi://messages` and `indi://scripts` event stream resources.

Per `docs/Design.md#event-streams`: two separate streams that share the same
`kind`/`type` envelope already used by `indi_messaging.IndiEvent` (the
messaging layer) and `script_runs.ScriptRunStatus` (the scripting layer).
This module is the broker connecting new events raised by those two modules
to MCP's `resources/subscribe` / `notifications/resources/updated` /
`resources/read` mechanism: a small rolling in-memory buffer per stream
(read by `resources/read`), plus a subscriber registry notified whenever a
new event is published.

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
and logged, rather than growing the queue (and the process's memory)
without bound.
"""

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import Mapping
from typing import NamedTuple, Protocol
from urllib.parse import quote

from pydantic import AnyUrl

from indi_mcp import event_log

logger = logging.getLogger(__name__)

__all__ = [
    "clear_messages",
    "drain",
    "is_subscribable_uri",
    "messages_uri",
    "publish_message_event",
    "publish_script_event",
    "read_messages",
    "read_scripts",
    "scripts_uri",
    "subscribe",
    "unsubscribe",
]

_MAX_BUFFERED_EVENTS = 200


class _NotifiableSession(Protocol):
    """The one piece of `mcp.server.session.ServerSession` this module needs."""

    async def send_resource_updated(self, uri: AnyUrl) -> None: ...


class _QueuedEvent(NamedTuple):
    """One durable-write job queued for `_record_worker` — see `_schedule_record`."""

    stream: event_log.Stream
    payload: Mapping
    device: str | None
    run_id: str | None


_messages: deque[Mapping] = deque(maxlen=_MAX_BUFFERED_EVENTS)
_scripts: deque[Mapping] = deque(maxlen=_MAX_BUFFERED_EVENTS)

_subscribers: dict[str, set[_NotifiableSession]] = {}

_background_tasks: set[asyncio.Task] = set()
"""Strong references to in-flight, one-off notification tasks (`_schedule_notify`).

`asyncio.create_task` results must be held onto somewhere or the task can be
garbage-collected mid-execution — a well-known asyncio footgun. Each task
removes itself once done. Durable-write tasks used to live here too, but
now go through the persistent `_record_worker`/`_record_queue` pair below
instead (INDIMCP-59) — `drain()` waits on both this set and that queue.
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
    failed write, so nothing here needs to re-raise on a failure.

    `_record_queue.join()` waits for every currently queued event to be
    *durably written*, not just dequeued — `_record_worker` only calls
    `task_done()` after its write attempt finishes (success or logged
    failure) — so this returns only once the worker has actually caught up,
    then stops the now-idle worker task before returning.
    """
    pending = [task for task in _background_tasks if not task.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    if _record_queue is not None:
        await _record_queue.join()
    if _record_worker_task is not None:
        _record_worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _record_worker_task


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
    """The `indi://scripts` resource URI, scoped to `run_id` if given (see `messages_uri`)."""
    return f"indi://scripts/{quote(run_id, safe='')}" if run_id else "indi://scripts"


_UNSCOPED_URIS = ("indi://messages", "indi://scripts")
_SCOPED_PREFIXES = ("indi://messages/", "indi://scripts/")


def is_subscribable_uri(uri: str) -> bool:
    """Whether `uri` is one of the resources this module actually publishes to.

    Checks the *shape* advertised by the `indi://messages`/`indi://messages/{device}`/
    `indi://scripts`/`indi://scripts/{runId}` resources (see `server.py`) — a single
    non-empty scope segment with no further `/` — not whether that particular device/run
    currently exists. Subscribing ahead of a device connecting or a run starting is expected
    and should still succeed; this only rejects URIs this module can never publish an update
    to at all (a typo like `indi://message`, or an unrelated resource like `frame://foo`),
    which would otherwise register a subscription that silently never fires.
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
    dropping them all rather than surfacing the real bug.
    """
    subscribers = _subscribers.get(uri)
    if not subscribers:
        return
    parsed_uri = AnyUrl(uri)
    for session in list(subscribers):
        try:
            await session.send_resource_updated(parsed_uri)
        except Exception:
            logger.exception("Failed to notify subscriber of %s; dropping it", uri)
            subscribers.discard(session)
    if not subscribers:
        _subscribers.pop(uri, None)


def _schedule_notify(uri: str) -> None:
    """Fire-and-forget `_notify(uri)` from a synchronous call site.

    Publishing happens from both async contexts (`indi_messaging.rxevent`)
    and sync ones (`script_runs`'s `on_progress` callback, `pause_script`),
    so this never awaits directly — it schedules a task on whatever loop is
    currently running, which is always the case at every real call site
    (an MCP tool/notification handler, or a task already running under one).
    Skipped entirely when nobody is subscribed to `uri`, the common case.
    """
    if uri not in _subscribers:
        return
    task = asyncio.create_task(_notify(uri))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


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
    stream: event_log.Stream, payload: Mapping, *, device: str | None, run_id: str | None
) -> None:
    """Enqueue `event_log.record_event(...)` for the durable-write worker, applying backpressure.

    Unlike `_schedule_notify`, this always runs regardless of whether anyone
    is currently subscribed — durable persistence exists to serve a client
    that reconnects *later* (`event_log.get_events`), not to mirror live
    delivery. If the queue is full (the worker falling behind a sustained
    burst — see `_RECORD_QUEUE_MAXSIZE`), the oldest queued event is dropped
    to make room for this newest one, and the drop is logged — the same
    "bounded, newest-biased" policy `_messages`/`_scripts`'s `maxlen` deques
    already apply to the live in-memory view, rather than letting the queue
    (and the process's memory) grow without bound.
    """
    queue = _ensure_record_worker()
    item = _QueuedEvent(stream, payload, device, run_id)
    try:
        queue.put_nowait(item)
        return
    except asyncio.QueueFull:
        pass
    try:
        dropped = queue.get_nowait()
        queue.task_done()
    except asyncio.QueueEmpty:
        # The worker drained the queue between our put_nowait and get_nowait above — no
        # cooperative yield point occurs in between on a single-threaded event loop, so this
        # can't actually happen today, but there's no harm in not assuming it never will.
        dropped = None
    if dropped is not None:
        logger.warning(
            "Durable event-log queue is full (maxsize=%d); dropping the oldest queued "
            "%s event to make room for a new one",
            _RECORD_QUEUE_MAXSIZE,
            dropped.stream,
        )
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        # Lost a race with another producer refilling the slot we just freed above — drop
        # this event rather than spin or block a synchronous, fire-and-forget call site.
        logger.warning("Durable event-log queue is still full; dropping a new %s event", stream)


def publish_message_event(event: Mapping) -> None:
    """Record a messaging-layer event and notify `indi://messages` (and per-device) subscribers.

    Also durably persisted to the event log — see `_schedule_record`.
    """
    _messages.appendleft(event)
    device = event.get("device")
    _schedule_record("messages", event, device=device, run_id=None)
    _schedule_notify(messages_uri(None))
    if device:
        _schedule_notify(messages_uri(device))


def publish_script_event(event: Mapping) -> None:
    """Record a scripting-layer event and notify `indi://scripts` (and per-run) subscribers.

    Also durably persisted to the event log — see `_schedule_record`.
    """
    _scripts.appendleft(event)
    run_id = event.get("runId")
    _schedule_record("scripts", event, device=None, run_id=run_id)
    _schedule_notify(scripts_uri(None))
    if run_id:
        _schedule_notify(scripts_uri(run_id))


def clear_messages() -> None:
    """Discard every buffered messaging-layer event.

    Called by `indi_messaging.start_messaging` on (re)connect: this is the
    single source of truth for messaging events (there's no longer a
    separate buffer in `indi_messaging` itself), so starting a fresh session
    clears it here, the same way `_latest_blobs` is cleared alongside it.
    Subscriptions/notifications are untouched — only the rolling read
    buffer is reset.
    """
    _messages.clear()


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
