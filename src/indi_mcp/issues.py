"""A general, non-fatal "something's off, but here's the result anyway" reporting channel.

Where an exception says "this can't continue," an `Issue` says "the caller should know about
this," at a severity the caller can act on differently: `INFO`/`WARNING`/`ERROR` are collected
alongside a normal result (see `script_engine._report_issue`), while `FATAL` still aborts the
operation the same way an unhandled exception always has (INDIMCP-73).
"""

from enum import StrEnum
from typing import TypedDict

__all__ = ["Issue", "Severity"]


class Severity(StrEnum):
    """How a caller should treat an `Issue`.

    Only `FATAL` changes control flow (aborts) — `INFO`/`WARNING`/`ERROR` are all "collect and
    continue," differing only in how urgently a human should look at them, not in what the
    engine itself does with them.
    """

    INFO = "Info"
    WARNING = "Warning"
    ERROR = "Error"
    FATAL = "Fatal"


class Issue(TypedDict):
    """One reported condition, `kind`-tagged like every other event/status envelope in this
    project (e.g. `script_runs.ScriptRunMessage`).

    `code` is a short, stable, machine-readable slug (e.g. `"filterConfigSynced"`) a caller
    can match on without parsing `message`. `role`/`device` follow the same "who this is about,
    if anyone in particular" convention as `script_engine.ScriptProgress`'s own fields — `None`
    when an issue isn't about a single resolved role/device.
    """

    kind: str
    severity: Severity
    code: str
    message: str
    role: str | None
    device: str | None
