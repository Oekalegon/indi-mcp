"""Subscribable `indi://messages` and `indi://scripts` event stream resources.

Per `docs/Design.md#event-streams`: two separate streams that share the same
`kind`/`type` envelope already used by `indi_messaging.IndiEvent` (the
messaging layer) and `script_runs.ScriptRunStatus` (the scripting layer).
This module is the broker connecting new events raised by those two modules
to MCP's `resources/subscribe` / `notifications/resources/updated` /
`resources/read` mechanism: a small rolling in-memory buffer per stream
(read by `resources/read`), plus a subscriber registry notified whenever a
new event is published.

**Best-effort, live-only** — matching Design.md exactly: a client that was
disconnected when an event occurred should not assume it received every
missed event via this route. The durable SQLite catch-up log
(`get_events`/retention) is a separate, later concern (INDIMCP-15); this
module only ever holds a bounded rolling window in memory, cleared on
process restart.
"""

import asyncio
import logging
from collections import deque
from collections.abc import Mapping
from typing import Protocol
from urllib.parse import quote

from pydantic import AnyUrl

logger = logging.getLogger(__name__)

__all__ = [
    "clear_messages",
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


_messages: deque[Mapping] = deque(maxlen=_MAX_BUFFERED_EVENTS)
_scripts: deque[Mapping] = deque(maxlen=_MAX_BUFFERED_EVENTS)

_subscribers: dict[str, set[_NotifiableSession]] = {}

_background_tasks: set[asyncio.Task] = set()
"""Strong references to in-flight notification tasks.

`asyncio.create_task` results must be held onto somewhere or the task can be
garbage-collected mid-execution — a well-known asyncio footgun. Each task
removes itself once done (see `_schedule_notify`).
"""


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


def publish_message_event(event: Mapping) -> None:
    """Record a messaging-layer event and notify `indi://messages` (and per-device) subscribers."""
    _messages.appendleft(event)
    _schedule_notify(messages_uri(None))
    device = event.get("device")
    if device:
        _schedule_notify(messages_uri(device))


def publish_script_event(event: Mapping) -> None:
    """Record a scripting-layer event and notify `indi://scripts` (and per-run) subscribers."""
    _scripts.appendleft(event)
    _schedule_notify(scripts_uri(None))
    run_id = event.get("runId")
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
