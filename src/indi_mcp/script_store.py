"""Loading and validating the script library.

A script is a declarative sequence of INDI steps — set a property, wait for
a condition, capture a frame, slew, cool the camera, select a filter, or call
another script — used to run
imaging sequences without an embedded expression language. See
`docs/ScriptSchema.md` for the full field-by-field schema reference and
`docs/Design.md` for the background and rationale. This module implements
that schema as pydantic models plus a script library loader; the execution
engine that runs a loaded `Script` against a rig is `script_engine.py`
(INDIMCP-7).

Scripts are YAML documents, not SQLite rows, loaded with `yaml.safe_load`
and validated against the schema below, mirroring `rig_store.py`'s
discipline: a file that fails to parse or validate is logged and skipped
rather than aborting the whole load, since scripts may be hand-edited or
uploaded by a client (INDIMCP-9). Unlike a rig file, a script's validity
also depends on the rest of the library (a `run_script` step's target must
exist and the whole library must be free of call cycles), so loading is a
two-pass process — see `load_scripts`.

Built-in scripts and user/client-uploaded scripts (`save_script`) live in
two separate directories on disk — see `load_scripts` — so redeploying the
built-in checkout can never clobber an upload, and an upload can never be
mistaken for (or silently override) a built-in. They still share one flat
`id` namespace at runtime: a `run_script` step in either can call into the
other.
"""

import logging
import os
import re
import threading
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, model_validator

logger = logging.getLogger(__name__)

__all__ = [
    "Condition",
    "CoolCameraStep",
    "IfStep",
    "PARAMETER_REFERENCE",
    "Parameter",
    "RaDecTarget",
    "RepeatStep",
    "RunScriptStep",
    "Script",
    "ScriptSummary",
    "SelectFilterStep",
    "SetFocusPositionStep",
    "SetPropertyStep",
    "SlewStep",
    "SlewTarget",
    "Step",
    "WaitForStep",
    "get_script",
    "list_scripts",
    "load_scripts",
    "referenced_roles",
    "save_script",
]

SCRIPTS_DIR_ENV = "INDI_MCP_SCRIPTS_DIR"
_DEFAULT_SCRIPTS_DIR = Path("scripts")

USER_SCRIPTS_DIR_ENV = "INDI_MCP_USER_SCRIPTS_DIR"
_DEFAULT_USER_SCRIPTS_DIR = Path("user_scripts")

PARAMETER_REFERENCE = re.compile(r"^\{\{\s*(\w+)\s*\}\}$")

ParameterType = Literal["string", "integer", "number", "boolean"]
ConditionOperator = Literal[
    "equals",
    "notEquals",
    "greaterThan",
    "lessThan",
    "greaterThanOrEqual",
    "lessThanOrEqual",
]

_PYTHON_TYPES: dict[ParameterType, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
}


def _check_reference_string(value: Any) -> Any:
    """Reject a string value that isn't a `{{ paramName }}` reference.

    Numeric/boolean step fields (`exposureSeconds`, `timeoutSeconds`,
    `count`, ...) also need to accept a parameter reference in place of a
    literal value (see `docs/ScriptSchema.md#parameter-references`), so
    they're typed as `<real type> | str` — this validator makes sure a
    string in that union is actually a reference, not just a typo'd number.
    """
    if isinstance(value, str) and not PARAMETER_REFERENCE.match(value):
        raise ValueError(f"{value!r} is not a valid number or a {{{{ paramName }}}} reference")
    return value


NumberOrReference = Annotated[float | str, BeforeValidator(_check_reference_string)]
IntOrReference = Annotated[int | str, BeforeValidator(_check_reference_string)]


class _StrictModel(BaseModel):
    """Base for script schema models: reject unknown fields from hand-edited/uploaded YAML."""

    model_config = ConfigDict(extra="forbid")


class Parameter(_StrictModel):
    """One named, typed input a script accepts (from a `run_script` call or step)."""

    type: ParameterType
    required: bool = False
    default: Any | None = None
    description: str | None = None


class Condition(_StrictModel):
    """A single comparison of live INDI state against a fixed value.

    Shared by `wait_for`, `repeat`'s `until`, and `if`. Compares one piece of
    known INDI state — never an arbitrary expression, consistent with the
    no-embedded-expression-language rule.
    """

    role: str
    property: str
    element: str | None = None
    operator: ConditionOperator
    value: str | float | bool


class RaDecTarget(_StrictModel):
    """A fixed equatorial coordinate `slew` target."""

    ra: NumberOrReference
    dec: NumberOrReference


class SlewTarget(_StrictModel):
    """A `slew` step's target: exactly one of a fixed coordinate or a named object."""

    raDec: RaDecTarget | None = None
    objectName: str | None = None

    @model_validator(mode="after")
    def _check_exactly_one_target(self) -> "SlewTarget":
        if (self.raDec is None) == (self.objectName is None):
            raise ValueError("slew target must set exactly one of raDec or objectName")
        return self


class _StepBase(_StrictModel):
    """Fields every step primitive carries, regardless of type."""

    description: str | None = None
    every: int | None = None


class SetPropertyStep(_StepBase):
    step: Literal["set_property"]
    role: str
    property: str
    elements: dict[str, str]


class WaitForStep(_StepBase):
    step: Literal["wait_for"]
    condition: Condition
    timeoutSeconds: NumberOrReference


class CaptureFrameStep(_StepBase):
    step: Literal["capture_frame"]
    role: str
    exposureSeconds: NumberOrReference
    frameType: Literal["Light", "Dark", "Flat", "Bias"] = "Light"
    binningX: IntOrReference = 1
    binningY: IntOrReference = 1


class SlewStep(_StepBase):
    step: Literal["slew"]
    role: str
    target: SlewTarget


class CoolCameraStep(_StepBase):
    step: Literal["cool_camera"]
    role: str
    targetTempC: NumberOrReference
    timeoutSeconds: NumberOrReference = 300


class SelectFilterStep(_StepBase):
    """Select a filter wheel slot, by number or by filter name — exactly one of the two.

    `filterName` is resolved to a numeric `FILTER_SLOT_VALUE` via the rig
    component's own `slots` map (`docs/RigSchema.md`) at execution time — a
    lookup only the execution engine can do (it needs the rig's own
    configuration, not just this step's fields), which is why `select_filter`
    is an engine-implemented primitive rather than a plain `set_property`/
    `wait_for` composition (INDIMCP-61).
    """

    step: Literal["select_filter"]
    role: str
    slot: IntOrReference | None = None
    filterName: str | None = None
    timeoutSeconds: NumberOrReference = 30

    @model_validator(mode="after")
    def _check_exactly_one_target(self) -> "SelectFilterStep":
        if (self.slot is None) == (self.filterName is None):
            raise ValueError("select_filter must set exactly one of slot or filterName")
        return self


class SetFocusPositionStep(_StepBase):
    """Move the focuser to an absolute position, checked against the rig component's own
    travel range.

    Checking `position` against the rig component's `minPosition`/`maxPosition`
    (`docs/RigSchema.md`) needs rig configuration a plain `set_property` step has no access
    to — the same category of reason `select_filter`'s `filterName` resolution is an
    engine-implemented primitive rather than a plain `set_property`/`wait_for` composition
    (INDIMCP-62). Not every focuser driver rejects an out-of-range `ABS_FOCUS_POSITION`
    consistently, so failing fast here is more useful than waiting out `timeoutSeconds` to
    discover the same thing from a vector that never reaches `Ok`.
    """

    step: Literal["set_focus_position"]
    role: str
    position: IntOrReference
    timeoutSeconds: NumberOrReference = 60


class RunScriptStep(_StepBase):
    step: Literal["run_script"]
    script: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class RepeatStep(_StepBase):
    step: Literal["repeat"]
    count: int | None = None
    until: Condition | None = None
    maxIterations: int | None = None
    steps: "list[Step]"

    @model_validator(mode="after")
    def _check_count_xor_until(self) -> "RepeatStep":
        if (self.count is None) == (self.until is None):
            raise ValueError("repeat must set exactly one of count or until")
        if self.until is not None and self.maxIterations is None:
            raise ValueError("repeat.until requires maxIterations")
        return self


class IfStep(_StepBase):
    step: Literal["if"]
    condition: Condition
    then: "list[Step]"
    else_: "list[Step]" = Field(default_factory=list, alias="else")

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


Step = Annotated[
    SetPropertyStep
    | WaitForStep
    | CaptureFrameStep
    | SlewStep
    | CoolCameraStep
    | SelectFilterStep
    | SetFocusPositionStep
    | RunScriptStep
    | RepeatStep
    | IfStep,
    Field(discriminator="step"),
]

RepeatStep.model_rebuild()
IfStep.model_rebuild()


class Script(_StrictModel):
    """A single script definition, as declared in one `scripts/*.yaml` file."""

    id: str
    name: str
    description: str | None = None
    pausable: bool
    parameters: dict[str, Parameter] = Field(default_factory=dict)
    steps: list[Step] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_parameter_references_are_declared(self) -> "Script":
        for reference in _iter_parameter_references(self.steps):
            if reference not in self.parameters:
                raise ValueError(f"undeclared parameter reference {{{{ {reference} }}}}")
        return self


class ScriptSummary(TypedDict):
    """The id/name/description of a loaded script, without its full definition."""

    id: str
    name: str
    description: str | None


def _iter_steps(steps: list[Step]) -> "list[Step]":
    """Flatten `steps` and everything nested inside `repeat`/`if` bodies."""
    flattened: list[Step] = []
    for step in steps:
        flattened.append(step)
        if isinstance(step, RepeatStep):
            flattened.extend(_iter_steps(step.steps))
        elif isinstance(step, IfStep):
            flattened.extend(_iter_steps(step.then))
            flattened.extend(_iter_steps(step.else_))
    return flattened


def _iter_string_fields(value: Any) -> "list[str]":
    """Collect every string value in a step's own fields (not nested step lists).

    Deliberately broader than the specific fields `script_engine._substitute`
    actually resolves at runtime (`elements` values, `condition.value`,
    `run_script.parameters`, `exposureSeconds`/`timeoutSeconds`/...) — this
    walks *every* string field of every step, including structural ones like
    `role`/`property`/`script` that are never substituted. That's
    intentional: it's cheap and safe to over-validate here (a `role` or
    `property` name coincidentally shaped like `"{{ name }}"` is vanishingly
    unlikely), and it means this check never needs to be kept in sync,
    field-by-field, with whichever fields the engine currently substitutes —
    a future field the engine starts substituting is already covered
    without a script_store change.
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, BaseModel):
        strings: list[str] = []
        for field_name in type(value).model_fields:
            if field_name == "steps" or field_name in ("then", "else_"):
                continue
            strings.extend(_iter_string_fields(getattr(value, field_name)))
        return strings
    if isinstance(value, dict):
        strings = []
        for item in value.values():
            strings.extend(_iter_string_fields(item))
        return strings
    if isinstance(value, list):
        strings = []
        for item in value:
            strings.extend(_iter_string_fields(item))
        return strings
    return []


def _iter_parameter_references(steps: list[Step]) -> "list[str]":
    """Every `{{ name }}` parameter reference used anywhere across `steps` (recursively)."""
    references: list[str] = []
    for step in _iter_steps(steps):
        for value in _iter_string_fields(step):
            match = PARAMETER_REFERENCE.match(value)
            if match:
                references.append(match.group(1))
    return references


def _run_script_steps(steps: list[Step]) -> "list[RunScriptStep]":
    return [step for step in _iter_steps(steps) if isinstance(step, RunScriptStep)]


def referenced_roles(script: Script) -> set[str]:
    """Every rig-component `role` referenced by `script`'s own steps/conditions.

    Not transitive through `run_script` — a calling script's roles and a
    callee's roles are separate sets; the execution engine (`script_engine.py`)
    is responsible for walking the whole call tree when it needs every role
    referenced across a run. See
    `docs/ScriptSchema.md#resolving-roles-to-devices`.
    """
    roles: set[str] = set()
    for step in _iter_steps(script.steps):
        if isinstance(
            step,
            SetPropertyStep
            | CaptureFrameStep
            | SlewStep
            | CoolCameraStep
            | SelectFilterStep
            | SetFocusPositionStep,
        ):
            roles.add(step.role)
        elif isinstance(step, WaitForStep | IfStep):
            roles.add(step.condition.role)
        elif isinstance(step, RepeatStep) and step.until is not None:
            roles.add(step.until.role)
    return roles


_scripts: dict[str, Script] = {}

_save_script_lock = threading.Lock()
"""Serializes `save_script`'s validate-then-write sequence.

`save_script` runs off the event loop (`asyncio.to_thread` in `server.py`),
so two concurrent uploads could otherwise run their directory-snapshot +
library-check + write in two different worker threads with no ordering
between them — a classic check-then-act race. Two scripts uploaded within
that window that `run_script`-call each other would each validate against a
snapshot that doesn't yet include the other's file, both writes would
succeed, and only the next `load_scripts()` reload would notice the
resulting cycle and silently drop both — exactly the "silently dropped"
failure mode `save_script` exists to avoid for a single bad upload.
"""


def _scripts_dir() -> Path:
    return Path(os.environ.get(SCRIPTS_DIR_ENV, _DEFAULT_SCRIPTS_DIR))


def _user_scripts_dir() -> Path:
    return Path(os.environ.get(USER_SCRIPTS_DIR_ENV, _DEFAULT_USER_SCRIPTS_DIR))


def load_scripts(directory: Path | None = None, user_directory: Path | None = None) -> list[Script]:
    """Load built-in scripts from `directory` and uploaded scripts from `user_directory`.

    Defaults to `$INDI_MCP_SCRIPTS_DIR` (falling back to `./scripts`) for the
    built-in side and `$INDI_MCP_USER_SCRIPTS_DIR` (falling back to
    `./user_scripts`) for the user/client-uploaded side — see the module
    docstring for why these are kept separate on disk. The two are merged
    into one `id`-keyed library before validation; if a user script's `id`
    collides with a built-in one, the built-in wins (the user script is
    logged and dropped) so a client can't silently override built-in
    behavior by reusing its id.

    Loading is otherwise a two-pass process: (1) parse and pydantic-validate
    each file independently (`_parse_script_files`), exactly like
    `load_rigs` — a file that fails to parse or match the per-script schema
    is logged and skipped; (2) once every valid script's `id` is known,
    validate `run_script` references, argument schemas, and check the whole
    library for call cycles. A script that fails a library-level check is
    also dropped (logged) — and since dropping one script can make another
    script's `run_script` reference dangle, this repeats until no more
    scripts are dropped.
    """
    global _scripts
    directory = directory if directory is not None else _scripts_dir()
    user_directory = user_directory if user_directory is not None else _user_scripts_dir()
    scripts = _parse_script_files(directory) if directory.is_dir() else {}
    if not directory.is_dir():
        logger.info("Scripts directory does not exist, no built-in scripts loaded: %s", directory)
    if user_directory.is_dir():
        for script_id, script in _parse_script_files(user_directory).items():
            if script_id in scripts:
                logger.warning(
                    "Skipping user script %r in %s: id collides with a built-in script",
                    script_id,
                    user_directory,
                )
                continue
            scripts[script_id] = script
    else:
        logger.info(
            "User scripts directory does not exist, no user scripts loaded: %s", user_directory
        )
    scripts = _drop_scripts_failing_library_checks(scripts)
    _scripts = scripts
    logger.info("Loaded %d script(s) from %s and %s", len(scripts), directory, user_directory)
    return list(scripts.values())


def _parse_script_files(directory: Path) -> dict[str, Script]:
    """Parse and per-script validate every `*.yaml` file in `directory`.

    A file that fails to parse or match the per-script schema is logged and
    skipped, same as `load_rigs`. Doesn't check anything library-wide (
    `run_script` references, cycles) — see `_drop_scripts_failing_library_checks`.
    """
    scripts: dict[str, Script] = {}
    for path in sorted(directory.glob("*.yaml")):
        if not path.is_file():
            logger.warning("Skipping non-file script path %s", path)
            continue
        try:
            raw = yaml.safe_load(path.read_text())
            script = Script.model_validate(raw)
        except (yaml.YAMLError, ValidationError) as exc:
            logger.warning("Skipping invalid script file %s: %s", path, exc)
            continue
        if script.id in scripts:
            logger.warning(
                "Duplicate script id %r in %s, keeping first definition", script.id, path
            )
            continue
        scripts[script.id] = script
    return scripts


def _drop_scripts_failing_library_checks(scripts: dict[str, Script]) -> dict[str, Script]:
    """Repeatedly drop scripts failing `run_script` reference/argument checks or in a cycle.

    Dropping one script can make another script's `run_script` reference
    dangle (now "unknown script"), so this repeats until a pass drops
    nothing.
    """
    changed = True
    while changed:
        changed = False
        for script_id, script in list(scripts.items()):
            reason = _first_library_check_failure(script, scripts)
            if reason is not None:
                logger.warning("Skipping script %r: %s", script_id, reason)
                del scripts[script_id]
                changed = True
        cyclic = _find_cyclic_scripts(scripts)
        for script_id in cyclic:
            logger.warning("Skipping script %r: part of a run_script call cycle", script_id)
            del scripts[script_id]
        changed = changed or bool(cyclic)
    return scripts


def _first_library_check_failure(script: Script, scripts: dict[str, Script]) -> str | None:
    """The first `run_script` reference/argument problem found in `script`, if any."""
    for call in _run_script_steps(script.steps):
        callee = scripts.get(call.script)
        if callee is None:
            return f"run_script references unknown script {call.script!r}"
        error = _check_call_arguments(script, callee, call.parameters)
        if error is not None:
            return error
    return None


def _check_call_arguments(caller: Script, callee: Script, arguments: dict[str, Any]) -> str | None:
    """Validate a `run_script` call's `arguments` against the callee's declared `parameters`."""
    unknown = set(arguments) - set(callee.parameters)
    if unknown:
        return f"run_script to {callee.id!r} passes undeclared parameter(s) {sorted(unknown)}"
    for name, parameter in callee.parameters.items():
        if name not in arguments:
            if parameter.required:
                return f"run_script to {callee.id!r} is missing required parameter {name!r}"
            continue
        value = arguments[name]
        reference_match = PARAMETER_REFERENCE.match(value) if isinstance(value, str) else None
        if reference_match:
            caller_param_name = reference_match.group(1)
            caller_param = caller.parameters.get(caller_param_name)
            if caller_param is None:
                return (
                    f"run_script to {callee.id!r} references undeclared "
                    f"parameter {{{{ {caller_param_name} }}}}"
                )
            if caller_param.type != parameter.type:
                return (
                    f"run_script to {callee.id!r} passes {name!r} "
                    f"({caller_param.type}) but it expects {parameter.type}"
                )
        elif not _matches_parameter_type(value, parameter.type):
            return f"run_script to {callee.id!r} passes {name!r} with the wrong type"
    return None


def _matches_parameter_type(value: Any, type_: ParameterType) -> bool:
    """Whether a literal YAML `value` matches a declared parameter `type`.

    Not a plain `isinstance(value, _PYTHON_TYPES[type_])`: YAML parses a
    literal like `5` as `int`, which should still satisfy a `"number"`
    parameter (a `number` accepts either `int` or `float`), and Python's
    `bool` is a subclass of `int`, so a `bool` value must be checked first
    or it would wrongly satisfy `"integer"`/`"number"` too.
    """
    if isinstance(value, bool):
        return type_ == "boolean"
    if type_ == "number":
        return isinstance(value, int | float)
    return isinstance(value, _PYTHON_TYPES[type_])


def _find_cyclic_scripts(scripts: dict[str, Script]) -> set[str]:
    """Every script id that's part of a `run_script` call cycle (including direct self-calls).

    Standard recursion-stack DFS: `stack` holds the current path. Reaching a
    script already on `stack` closes a cycle — every script from that point
    onward in `stack` is part of it. `visited` marks scripts whose whole
    reachable subgraph has already been explored (from any starting point),
    since a cycle through an already-fully-explored script would already
    have been found during that exploration, regardless of which script
    the outer loop below started from.
    """
    graph = {
        script_id: [
            call.script for call in _run_script_steps(script.steps) if call.script in scripts
        ]
        for script_id, script in scripts.items()
    }
    cyclic: set[str] = set()
    visited: set[str] = set()

    def visit(script_id: str, stack: list[str]) -> None:
        if script_id in stack:
            cyclic.update(stack[stack.index(script_id) :])
            return
        if script_id in visited:
            return
        visited.add(script_id)
        stack.append(script_id)
        for neighbor in graph[script_id]:
            visit(neighbor, stack)
        stack.pop()

    for script_id in graph:
        visit(script_id, [])
    return cyclic


def list_scripts() -> list[ScriptSummary]:
    """List the id/name/description of every currently loaded script."""
    return [
        {"id": script.id, "name": script.name, "description": script.description}
        for script in _scripts.values()
    ]


def get_script(script_id: str) -> Script:
    """Return the full definition of the script identified by `script_id`."""
    script = _scripts.get(script_id)
    if script is None:
        raise ValueError(f"Unknown script: {script_id!r}")
    return script


def save_script(
    script: Script,
    *,
    overwrite: bool = False,
    directory: Path | None = None,
    builtin_directory: Path | None = None,
) -> Script:
    """Write `script` to `<directory>/<script.id>.yaml` and reload the merged library.

    `directory` is the *user/uploaded* scripts directory (defaults to
    `$INDI_MCP_USER_SCRIPTS_DIR`, falling back to `./user_scripts`) — never
    the built-in one, so an upload can't land among the built-in scripts
    shipped in the repo checkout (see the module docstring). `builtin_directory`
    (defaults to `$INDI_MCP_SCRIPTS_DIR`, falling back to `./scripts`) is only
    read, to check `script` against the built-in scripts too.

    `script` is already schema-validated (pydantic validation happens when
    it's constructed), but unlike a rig or observatory a script's validity
    also depends on the rest of the library — a `run_script` call into or
    out of it must resolve, argument types must line up, and no call cycle
    may result. Those library-level checks (`_check_library_accepts`) run
    against the merged built-in + user library *before* anything is
    written, so a bad upload is rejected with a specific reason rather than
    being written and then silently dropped (logged only) the way
    `load_scripts` handles an invalid file at startup. Reusing a built-in
    `id` is rejected outright, since a client shouldn't be able to shadow
    built-in behavior. Refuses to replace an existing `<script.id>.yaml`
    unless `overwrite` is set, since reusing an `id` could otherwise
    silently destroy a previously saved script. The existence check and the
    write happen as one atomic file-open (exclusive-create unless
    `overwrite`), so two concurrent saves of the same new `id` can't both
    slip past the check. The whole validate-then-write sequence is
    serialized with `_save_script_lock` (see its docstring) since this
    function runs off the event loop, in a worker thread, where two
    concurrent uploads could otherwise race past each other's checks.
    """
    if not script.id or script.id in (".", "..") or "/" in script.id or "\\" in script.id:
        raise ValueError(f"Invalid script id for a filename: {script.id!r}")
    directory = directory if directory is not None else _user_scripts_dir()
    builtin_directory = builtin_directory if builtin_directory is not None else _scripts_dir()
    with _save_script_lock:
        return _save_script_locked(script, overwrite, directory, builtin_directory)


def _save_script_locked(
    script: Script, overwrite: bool, directory: Path, builtin_directory: Path
) -> Script:
    """The validate-then-write body of `save_script`, run under `_save_script_lock`."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except NotADirectoryError as exc:
        raise ValueError(f"Cannot create user scripts directory {directory}: {exc}") from exc
    path = directory / f"{script.id}.yaml"
    if path.is_dir():
        raise ValueError(f"Cannot save script {script.id!r}: {path} is a directory, not a file")

    builtin = _parse_script_files(builtin_directory) if builtin_directory.is_dir() else {}
    if script.id in builtin:
        raise ValueError(f"Cannot save script {script.id!r}: id collides with a built-in script")
    library = dict(builtin)
    for user_id, user_script in _parse_script_files(directory).items():
        library.setdefault(user_id, user_script)
    library = _drop_scripts_failing_library_checks(library)
    library.pop(script.id, None)
    library[script.id] = script
    _check_library_accepts(script, library)

    content = yaml.safe_dump(script.model_dump(exclude_none=True, by_alias=True), sort_keys=False)
    try:
        with path.open("w" if overwrite else "x", encoding="utf-8") as f:
            f.write(content)
    except FileExistsError as exc:
        raise ValueError(
            f"A script file already exists for id {script.id!r} ({path}); "
            "pass overwrite=True to replace it."
        ) from exc
    logger.info("Saved script %r to %s", script.id, path)
    load_scripts(builtin_directory, directory)
    return get_script(script.id)


def _check_library_accepts(script: Script, library: dict[str, Script]) -> None:
    """Raise `ValueError` if saving `script` (already present in `library` by id) is unsafe.

    Checks `script`'s own `run_script` calls, every other script's calls
    *into* `script` (its parameter schema may have just changed under
    them), and whether `script` closes a `run_script` call cycle. `library`
    is expected to already be library-valid apart from `script` itself
    (see `save_script`), so any failure found here is attributable to
    `script`.
    """
    reason = _first_library_check_failure(script, library)
    if reason is not None:
        raise ValueError(f"Cannot save script {script.id!r}: {reason}")
    for other_id, other in library.items():
        if other_id == script.id:
            continue
        if not any(call.script == script.id for call in _run_script_steps(other.steps)):
            continue
        reason = _first_library_check_failure(other, library)
        if reason is not None:
            raise ValueError(
                f"Cannot save script {script.id!r}: breaks caller {other_id!r} ({reason})"
            )
    if script.id in _find_cyclic_scripts(library):
        raise ValueError(f"Cannot save script {script.id!r}: part of a run_script call cycle")
