"""The INDI messaging layer: streaming property/message events and sending commands.

An `indipyclient.IPyClient` subclass connects to `indiserver` as a plain INDI
client, translating every `def*Vector`/`set*Vector`/`message`/`delProperty`
event it receives into a small `kind`/`type`-tagged dict (see `IndiEvent`).
Sending a property command (`new*Vector`) is likewise recorded as a
`propertyCommand` event, so a client can observe both directions of traffic
with one call. `event_streams` is the single source of truth for the
resulting rolling window of recent events — both `list_messages` (this
module's own polling tool) and the `indi://messages` subscribable resource
(INDIMCP-14) read from the same buffer there, rather than each maintaining
an independent copy that could silently drift out of sync.
"""

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, TypedDict, cast

from indipyclient import (
    IPyClient,
    Message,
    defBLOBVector,
    defLightVector,
    defNumberVector,
    defSwitchVector,
    defTextVector,
    delProperty,
    setBLOBVector,
    setLightVector,
    setNumberVector,
    setSwitchVector,
    setTextVector,
)

from indi_mcp import event_streams
from indi_mcp.indi_server import INDI_PORT

logger = logging.getLogger(__name__)

__all__ = [
    "BlobSnapshot",
    "IndiEvent",
    "MessagingStatus",
    "PropertyState",
    "get_latest_blob",
    "get_property_range",
    "get_property_state",
    "get_property_values",
    "get_status",
    "list_devices",
    "list_messages",
    "send_property",
    "start_messaging",
    "stop_messaging",
]

_STARTUP_POLL_TIMEOUT = 2.0
_STARTUP_POLL_INTERVAL = 0.1

_DEF_VECTOR_TYPES: dict[type, str] = {
    defSwitchVector: "switch",
    defTextVector: "text",
    defNumberVector: "number",
    defLightVector: "light",
    defBLOBVector: "blob",
}
_SET_VECTOR_TYPES: dict[type, str] = {
    setSwitchVector: "switch",
    setTextVector: "text",
    setNumberVector: "number",
    setLightVector: "light",
    setBLOBVector: "blob",
}
_VECTORTYPE_TO_TYPE = {
    "SwitchVector": "switch",
    "TextVector": "text",
    "NumberVector": "number",
    "LightVector": "light",
    "BLOBVector": "blob",
}


class PropertyState(StrEnum):
    """An INDI property vector's own state, distinct from any of its element values."""

    IDLE = "Idle"
    OK = "Ok"
    BUSY = "Busy"
    ALERT = "Alert"


def _coerce_property_state(raw: str | None) -> "PropertyState | str | None":
    """Coerce a raw INDI state string to `PropertyState` when it's one of the four known values.

    Falls back to the raw string unchanged for anything else, rather than
    raising: the wire value is the actual source of truth, not this enum,
    so an unfamiliar value (a future INDI addition, a quirky driver) is
    passed through rather than treated as an error.
    """
    if raw is None:
        return None
    try:
        return PropertyState(raw)
    except ValueError:
        return raw


class IndiEvent(TypedDict):
    """A single INDI protocol event, in a `kind`/`type`-tagged envelope."""

    kind: str
    type: str | None
    device: str | None
    name: str | None
    state: PropertyState | str | None
    message: str | None
    elements: dict[str, str] | None
    timestamp: str


class MessagingStatus(TypedDict):
    """Current state of the INDI messaging connection."""

    running: bool
    host: str
    port: int


class BlobSnapshot(TypedDict):
    """The most recently received BLOB vector update for one `(device, vectorname)` pair.

    `values` maps member name to its raw decoded bytes; `sizeformat` maps
    the same member names to `(size, format)`, `format` being a file
    extension (e.g. `".fits"`) reported by the driver itself — see
    `get_latest_blob`.
    """

    values: dict[str, bytes]
    sizeformat: dict[str, tuple[int, str]]
    timestamp: datetime


def _elements(event: Any) -> dict[str, str] | None:
    if isinstance(event, setBLOBVector):
        return {name: f"{size} bytes ({fmt})" for name, (size, fmt) in event.sizeformat.items()}
    if isinstance(event, defBLOBVector):
        return None
    return dict(event.data)


def _to_indi_event(event: Any) -> IndiEvent | None:
    if isinstance(event, Message):
        kind, type_name, elements = "message", None, None
    elif isinstance(event, delProperty):
        kind, type_name, elements = "propertyDeleted", None, None
    else:
        kind = type_name = None
        elements = None
        for cls, name in _DEF_VECTOR_TYPES.items():
            if isinstance(event, cls):
                kind, type_name, elements = "propertyDefinition", name, _elements(event)
                break
        else:
            for cls, name in _SET_VECTOR_TYPES.items():
                if isinstance(event, cls):
                    kind, type_name, elements = "propertyUpdate", name, _elements(event)
                    break
        if kind is None:
            return None
    return {
        "kind": kind,
        "type": type_name,
        "device": event.devicename,
        "name": event.vectorname,
        "state": _coerce_property_state(getattr(event, "state", None)),
        "message": getattr(event, "message", None) or None,
        "elements": elements,
        "timestamp": event.timestamp.isoformat(),
    }


class _MessagingClient(IPyClient):
    """An `IPyClient` that publishes every received event as an `IndiEvent` to `event_streams`.

    Also sets `enableBLOBdefault = "Also"` so BLOBs are actually
    transmitted at all — `indipyclient` defaults this to `"Never"`, and
    `IPyClient` auto-sends the corresponding `enableBLOB` instruction the
    moment it learns of a `defBLOBVector`, using whatever
    `enableBLOBdefault` is set to *at that time*; setting it here, in
    `__init__`, guarantees it's in place before `asyncrun()` (started
    immediately after construction by `start_messaging`) ever processes one.
    "Also" (rather than "Only") keeps every other property flowing
    alongside BLOBs — `capture_frame`'s `_wait_for_property_state` call for
    `CCD_EXPOSURE` still needs ordinary property updates too.
    """

    def __init__(self, host: str, port: int) -> None:
        super().__init__(indihost=host, indiport=port)
        self.enableBLOBdefault = "Also"

    async def rxevent(self, event: Any) -> None:
        indi_event = _to_indi_event(event)
        if indi_event is not None:
            event_streams.publish_message_event(indi_event)
        if isinstance(event, setBLOBVector):
            _latest_blobs[(event.devicename, event.vectorname)] = {
                "values": dict(event.data),
                "sizeformat": dict(event.sizeformat),
                "timestamp": datetime.now(tz=UTC),
            }


_latest_blobs: dict[tuple[str, str], BlobSnapshot] = {}
_client: _MessagingClient | None = None
_task: asyncio.Task | None = None
_host = "localhost"
_port = INDI_PORT


def _require_client() -> _MessagingClient:
    if _client is None:
        raise RuntimeError("INDI messaging is not started; call start_messaging first.")
    return _client


async def start_messaging(host: str = "localhost", port: int = INDI_PORT) -> MessagingStatus:
    """Connect to `indiserver` and start streaming its property/message events."""
    global _client, _task, _host, _port
    if _client is not None:
        await stop_messaging()
    logger.info("Starting INDI messaging client (%s:%d)", host, port)
    event_streams.clear_messages()
    _latest_blobs.clear()
    _client = _MessagingClient(host, port)
    _host, _port = host, port
    _task = asyncio.create_task(_client.asyncrun())
    return await _wait_until_connected()


async def stop_messaging() -> MessagingStatus:
    """Disconnect from `indiserver` and stop streaming."""
    global _client, _task
    if _task is not None:
        _task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _task
        _task = None
    _client = None
    return await get_status()


async def get_status() -> MessagingStatus:
    """Report whether the INDI messaging stream is running, and its host/port."""
    return {"running": _client is not None and _client.connected, "host": _host, "port": _port}


async def _wait_until_connected() -> MessagingStatus:
    """Poll `get_status` until the client reports connected or the timeout elapses.

    Right after `start_messaging` launches the client's background task, its
    TCP connection to `indiserver` is still completing asynchronously, so
    `_client.connected` can briefly report `False` even though the
    connection is about to succeed.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _STARTUP_POLL_TIMEOUT
    status = await get_status()
    while not status["running"] and loop.time() < deadline:
        await asyncio.sleep(_STARTUP_POLL_INTERVAL)
        status = await get_status()
    return status


def list_devices() -> list[str]:
    """List the INDI device names currently known to the messaging client."""
    client = _require_client()
    return list(client.data.keys())


def get_property_values(device: str, name: str) -> dict[str, str] | None:
    """Return the current member values of `device`'s property `name`.

    `None` if the device isn't connected or hasn't (yet) defined that
    property, rather than raising — a missing property is routine (e.g. a
    camera that hasn't reported `CCD_INFO` yet), not an error condition, and
    callers like `draft_rig` want to treat it as "nothing to prefill from".
    """
    client = _require_client()
    device_obj = client.data.get(device)
    if device_obj is None:
        return None
    vector = device_obj.data.get(name)
    if vector is None:
        return None
    return {member_name: vector[member_name] for member_name in vector.data}


def get_property_state(device: str, name: str) -> PropertyState | str | None:
    """Return the current vector `state` (`Idle`/`Ok`/`Busy`/`Alert`) of `device`'s property `name`.

    `None` if the device isn't connected or hasn't (yet) defined that
    property, matching `get_property_values`'s behavior — routine, not an
    error condition. Returns a `PropertyState` for one of the four known
    values; falls back to the raw string for anything else (see
    `_coerce_property_state`).
    """
    client = _require_client()
    device_obj = client.data.get(device)
    if device_obj is None:
        return None
    vector = device_obj.data.get(name)
    if vector is None:
        return None
    return _coerce_property_state(vector.state)


def get_property_range(device: str, name: str, member: str) -> tuple[float, float] | None:
    """Return the (min, max) range of numeric property `device`.`name`'s `member`.

    `None` if the device, property, or member isn't currently defined.
    """
    client = _require_client()
    device_obj = client.data.get(device)
    if device_obj is None:
        return None
    vector = device_obj.data.get(name)
    if vector is None or member not in vector.data:
        return None
    member_obj = vector.data[member]
    try:
        return float(member_obj.min), float(member_obj.max)
    except (AttributeError, TypeError, ValueError):
        return None


def get_latest_blob(device: str, name: str) -> BlobSnapshot | None:
    """Return the most recently received BLOB vector update for `device`.`name`, if any.

    `None` if no BLOB has ever arrived on this `(device, name)` pair since
    `start_messaging` was last called (`_latest_blobs` is cleared then,
    matching `event_streams.clear_messages()`) — routine (e.g. before the
    first capture), not an error, matching `get_property_values`'s own
    "missing is normal" style.
    Only ever holds the single most recent update per pair — a caller that
    needs to distinguish "this capture's BLOB" from a stale one left over
    from an earlier capture of the same device/vector must compare the
    returned `timestamp` against its own "since I sent the command" marker
    itself (see `script_engine._wait_for_blob`).
    """
    return _latest_blobs.get((device, name))


def list_messages(device: str | None = None, limit: int = 50) -> list[IndiEvent]:
    """List the most recently seen INDI events, newest first, optionally filtered to one device.

    Reads from `event_streams`, the single source of truth for buffered
    messaging events (also backing the `indi://messages` resource) — this
    is purely a filtered/limited view over the same underlying window, not
    a separate copy of it.
    """
    events = cast("list[IndiEvent]", event_streams.read_messages(device)["events"])
    return events[:limit]


async def send_property(device: str, name: str, elements: dict[str, str]) -> IndiEvent:
    """Send a `new*Vector` command to an INDI device, setting `elements` on property `name`."""
    client = _require_client()
    device_obj = client.data.get(device)
    if device_obj is None:
        raise ValueError(f"Unknown INDI device: {device!r}")
    vector = device_obj.data.get(name)
    if vector is None:
        raise ValueError(f"Unknown property {name!r} on device {device!r}")
    await client.send_newVector(device, name, members=elements)
    event: IndiEvent = {
        "kind": "propertyCommand",
        "type": _VECTORTYPE_TO_TYPE.get(vector.vectortype),
        "device": device,
        "name": name,
        "state": None,
        "message": None,
        "elements": dict(elements),
        "timestamp": datetime.now(tz=UTC).isoformat(),
    }
    event_streams.publish_message_event(event)
    return event
