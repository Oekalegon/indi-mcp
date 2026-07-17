"""Loading and validating the script library.

A script is a declarative sequence of INDI steps — set a property, wait for
a condition, capture a frame, slew, or call another script — used to run
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
"""

import logging
import os
import re
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, model_validator

logger = logging.getLogger(__name__)

__all__ = [
    "Condition",
    "IfStep",
    "PARAMETER_REFERENCE",
    "Parameter",
    "RaDecTarget",
    "RepeatStep",
    "RunScriptStep",
    "Script",
    "ScriptSummary",
    "SetPropertyStep",
    "SlewStep",
    "SlewTarget",
    "Step",
    "WaitForStep",
    "get_script",
    "list_scripts",
    "load_scripts",
    "referenced_roles",
]

SCRIPTS_DIR_ENV = "INDI_MCP_SCRIPTS_DIR"
_DEFAULT_SCRIPTS_DIR = Path("scripts")

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
    """Collect every string value in a step's own fields (not nested step lists)."""
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
        if isinstance(step, SetPropertyStep | CaptureFrameStep | SlewStep):
            roles.add(step.role)
        elif isinstance(step, WaitForStep | IfStep):
            roles.add(step.condition.role)
        elif isinstance(step, RepeatStep) and step.until is not None:
            roles.add(step.until.role)
    return roles


_scripts: dict[str, Script] = {}


def _scripts_dir() -> Path:
    return Path(os.environ.get(SCRIPTS_DIR_ENV, _DEFAULT_SCRIPTS_DIR))


def load_scripts(directory: Path | None = None) -> list[Script]:
    """Load every `*.yaml` script from `directory`, then validate the library as a whole.

    Defaults to `$INDI_MCP_SCRIPTS_DIR`, falling back to `./scripts`. This is
    a two-pass load: (1) parse and pydantic-validate each file independently,
    exactly like `load_rigs` — a file that fails to parse or match the
    per-script schema is logged and skipped; (2) once every valid script's
    `id` is known, validate `run_script` references, argument schemas, and
    check the whole library for call cycles. A script that fails a
    library-level check is also dropped (logged) — and since dropping one
    script can make another script's `run_script` reference dangle, this
    repeats until no more scripts are dropped.
    """
    global _scripts
    directory = directory if directory is not None else _scripts_dir()
    scripts: dict[str, Script] = {}
    if not directory.is_dir():
        logger.info("Scripts directory does not exist, no scripts loaded: %s", directory)
        _scripts = scripts
        return []
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

    scripts = _drop_scripts_failing_library_checks(scripts)
    _scripts = scripts
    logger.info("Loaded %d script(s) from %s", len(scripts), directory)
    return list(scripts.values())


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


def _check_call_arguments(
    caller: Script, callee: Script, arguments: dict[str, Any]
) -> str | None:
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
