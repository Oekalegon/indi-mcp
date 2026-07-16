"""The INDI messaging layer: streaming property/message events and sending commands.

An `indipyclient.IPyClient` subclass connects to `indiserver` as a plain INDI
client, translating every `def*Vector`/`set*Vector`/`message`/`delProperty`
event it receives into a small `kind`/`type`-tagged dict (see `IndiEvent`) and
buffering the most recent ones in memory. Sending a property command
(`new*Vector`) is likewise recorded into the same buffer as a `propertyCommand`
event, so a client can observe both directions of traffic with one call.
"""

import asyncio
import contextlib
import logging
from collections import deque
from datetime import UTC, datetime
from typing import Any, TypedDict

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

from indi_mcp.indi_server import INDI_PORT

logger = logging.getLogger(__name__)

__all__ = [
    "IndiEvent",
    "MessagingStatus",
    "get_status",
    "list_devices",
    "list_messages",
    "send_property",
    "start_messaging",
    "stop_messaging",
]

_MAX_BUFFERED_EVENTS = 200

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


class IndiEvent(TypedDict):
    """A single INDI protocol event, in a `kind`/`type`-tagged envelope."""

    kind: str
    type: str | None
    device: str | None
    name: str | None
    state: str | None
    message: str | None
    elements: dict[str, str] | None
    timestamp: str


class MessagingStatus(TypedDict):
    """Current state of the INDI messaging connection."""

    running: bool
    host: str
    port: int


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
        "state": getattr(event, "state", None),
        "message": getattr(event, "message", None) or None,
        "elements": elements,
        "timestamp": event.timestamp.isoformat(),
    }


class _MessagingClient(IPyClient):
    """An `IPyClient` that buffers every received event as an `IndiEvent`."""

    def __init__(self, host: str, port: int, buffer: deque[IndiEvent]) -> None:
        super().__init__(indihost=host, indiport=port)
        self._buffer = buffer

    async def rxevent(self, event: Any) -> None:
        indi_event = _to_indi_event(event)
        if indi_event is not None:
            self._buffer.appendleft(indi_event)


_buffer: deque[IndiEvent] = deque(maxlen=_MAX_BUFFERED_EVENTS)
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
    _buffer.clear()
    _client = _MessagingClient(host, port, _buffer)
    _host, _port = host, port
    _task = asyncio.create_task(_client.asyncrun())
    return await get_status()


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


def list_devices() -> list[str]:
    """List the INDI device names currently known to the messaging client."""
    client = _require_client()
    return list(client.data.keys())


def list_messages(device: str | None = None, limit: int = 50) -> list[IndiEvent]:
    """List the most recently seen INDI events, newest first, optionally filtered to one device."""
    matching = (event for event in _buffer if device is None or event["device"] == device)
    result = []
    for event in matching:
        if len(result) >= limit:
            break
        result.append(event)
    return result


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
    _buffer.appendleft(event)
    return event
