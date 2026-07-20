from pathlib import Path

import pytest
import yaml

from indi_mcp import script_store

MINIMAL_SCRIPT_YAML = """
id: minimal
name: Minimal script
pausable: false
steps: []
"""

COOL_CAMERA_YAML = """
id: cool_camera
name: Cool camera
pausable: true
parameters:
  targetTempC:
    type: number
    required: false
    default: -10
steps:
  - step: set_property
    role: camera
    property: CCD_TEMPERATURE
    elements: { CCD_TEMPERATURE_VALUE: "{{ targetTempC }}" }
  - step: wait_for
    condition:
      role: camera
      property: CCD_TEMPERATURE
      operator: equals
      value: Ok
    timeoutSeconds: 60
"""

CAPTURE_SEQUENCE_YAML = """
id: capture_sequence_m101
name: Capture 20x5min frames of M101 with periodic refocus
pausable: true
parameters:
  targetTempC:
    type: number
    required: false
    default: -10
  exposureSeconds:
    type: number
    required: true
steps:
  - step: run_script
    script: cool_camera
    parameters: { targetTempC: "{{ targetTempC }}" }
  - step: slew
    role: mount
    target:
      objectName: M101
  - step: set_property
    role: filterWheel
    property: FILTER_SLOT
    elements: { FILTER_SLOT_VALUE: "1" }
  - step: repeat
    count: 20
    steps:
      - step: run_script
        script: focus
        every: 2
      - step: capture_frame
        role: camera
        exposureSeconds: "{{ exposureSeconds }}"
        frameType: Light
"""

FOCUS_YAML = """
id: focus
name: Focus
pausable: false
steps: []
"""


@pytest.fixture(autouse=True)
def _reset_loaded_scripts() -> None:
    script_store._scripts = {}


def test_load_scripts_returns_empty_list_when_directory_missing(tmp_path: Path) -> None:
    scripts = script_store.load_scripts(tmp_path / "does-not-exist")

    assert scripts == []
    assert script_store.list_scripts() == []


def test_load_scripts_parses_a_minimal_script(tmp_path: Path) -> None:
    (tmp_path / "minimal.yaml").write_text(MINIMAL_SCRIPT_YAML)

    scripts = script_store.load_scripts(tmp_path)

    assert len(scripts) == 1
    assert scripts[0].id == "minimal"
    assert scripts[0].pausable is False
    assert scripts[0].steps == []


def test_load_scripts_parses_every_step_primitive(tmp_path: Path) -> None:
    (tmp_path / "cool_camera.yaml").write_text(COOL_CAMERA_YAML)
    (tmp_path / "focus.yaml").write_text(FOCUS_YAML)
    (tmp_path / "capture_sequence.yaml").write_text(CAPTURE_SEQUENCE_YAML)

    scripts = script_store.load_scripts(tmp_path)

    by_id = {s.id: s for s in scripts}
    top = by_id["capture_sequence_m101"]
    step_types = [type(step).__name__ for step in top.steps]
    assert step_types == [
        "RunScriptStep",
        "SlewStep",
        "SetPropertyStep",
        "RepeatStep",
    ]
    repeat_step = top.steps[-1]
    assert isinstance(repeat_step, script_store.RepeatStep)
    assert [type(step).__name__ for step in repeat_step.steps] == [
        "RunScriptStep",
        "CaptureFrameStep",
    ]
    assert repeat_step.steps[0].every == 2
    assert repeat_step.count == 20


def test_load_scripts_parses_wait_for_and_cool_camera(tmp_path: Path) -> None:
    (tmp_path / "cool_camera.yaml").write_text(COOL_CAMERA_YAML)

    scripts = script_store.load_scripts(tmp_path)

    wait_step = scripts[0].steps[1]
    assert isinstance(wait_step, script_store.WaitForStep)
    assert wait_step.condition.operator == "equals"
    assert wait_step.timeoutSeconds == 60


def test_slew_target_rejects_both_ra_dec_and_object_name() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        script_store.SlewTarget(
            raDec=script_store.RaDecTarget(ra=10.0, dec=20.0), objectName="M101"
        )


def test_slew_target_rejects_neither_ra_dec_nor_object_name() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        script_store.SlewTarget()


def test_repeat_step_rejects_both_count_and_until() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        script_store.RepeatStep(
            step="repeat",
            count=5,
            until=script_store.Condition(
                role="camera", property="CCD_TEMPERATURE", operator="equals", value="Ok"
            ),
            maxIterations=10,
            steps=[],
        )


def test_repeat_step_rejects_neither_count_nor_until() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        script_store.RepeatStep(step="repeat", steps=[])


def test_repeat_step_until_requires_max_iterations() -> None:
    with pytest.raises(ValueError, match="maxIterations"):
        script_store.RepeatStep(
            step="repeat",
            until=script_store.Condition(
                role="camera", property="CCD_TEMPERATURE", operator="equals", value="Ok"
            ),
            steps=[],
        )


def test_if_step_parses_else_alias(tmp_path: Path) -> None:
    (tmp_path / "with-if.yaml").write_text(
        """
id: with-if
name: With if
pausable: false
steps:
  - step: if
    condition:
      role: camera
      property: CONNECTION
      operator: equals
      value: "On"
    then:
      - step: set_property
        role: camera
        property: CCD_EXPOSURE
        elements: { CCD_EXPOSURE_VALUE: "1" }
    else:
      - step: set_property
        role: camera
        property: CCD_EXPOSURE
        elements: { CCD_EXPOSURE_VALUE: "2" }
"""
    )

    scripts = script_store.load_scripts(tmp_path)

    if_step = scripts[0].steps[0]
    assert isinstance(if_step, script_store.IfStep)
    assert len(if_step.then) == 1
    assert len(if_step.else_) == 1


def test_load_scripts_skips_files_with_invalid_yaml(tmp_path: Path) -> None:
    (tmp_path / "broken.yaml").write_text("id: [unterminated")
    (tmp_path / "minimal.yaml").write_text(MINIMAL_SCRIPT_YAML)

    scripts = script_store.load_scripts(tmp_path)

    assert [s.id for s in scripts] == ["minimal"]


def test_load_scripts_rejects_unknown_top_level_fields(tmp_path: Path) -> None:
    (tmp_path / "extra.yaml").write_text(MINIMAL_SCRIPT_YAML + "\nunknownField: true\n")

    scripts = script_store.load_scripts(tmp_path)

    assert scripts == []


def test_load_scripts_rejects_unknown_step_type(tmp_path: Path) -> None:
    (tmp_path / "bad-step.yaml").write_text(
        'id: bad-step\nname: "Bad step"\npausable: false\nsteps:\n'
        "  - step: teleport\n    role: mount\n"
    )

    scripts = script_store.load_scripts(tmp_path)

    assert scripts == []


def test_load_scripts_keeps_first_definition_on_duplicate_id(tmp_path: Path) -> None:
    (tmp_path / "a-first.yaml").write_text(MINIMAL_SCRIPT_YAML)
    (tmp_path / "b-second.yaml").write_text(
        MINIMAL_SCRIPT_YAML.replace("Minimal script", "Duplicate")
    )

    scripts = script_store.load_scripts(tmp_path)

    assert len(scripts) == 1
    assert scripts[0].name == "Minimal script"


def test_load_scripts_rejects_an_undeclared_parameter_reference(tmp_path: Path) -> None:
    (tmp_path / "bad-ref.yaml").write_text(
        'id: bad-ref\nname: "Bad ref"\npausable: false\nsteps:\n'
        "  - step: set_property\n    role: camera\n    property: CCD_EXPOSURE\n"
        '    elements: { CCD_EXPOSURE_VALUE: "{{ undeclared }}" }\n'
    )

    scripts = script_store.load_scripts(tmp_path)

    assert scripts == []


def test_load_scripts_accepts_a_declared_parameter_reference(tmp_path: Path) -> None:
    (tmp_path / "good-ref.yaml").write_text(
        'id: good-ref\nname: "Good ref"\npausable: false\n'
        "parameters:\n  exposureSeconds:\n    type: number\n    required: true\n"
        "steps:\n  - step: set_property\n    role: camera\n    property: CCD_EXPOSURE\n"
        '    elements: { CCD_EXPOSURE_VALUE: "{{ exposureSeconds }}" }\n'
    )

    scripts = script_store.load_scripts(tmp_path)

    assert [s.id for s in scripts] == ["good-ref"]


def test_load_scripts_rejects_run_script_to_unknown_script(tmp_path: Path) -> None:
    (tmp_path / "caller.yaml").write_text(
        'id: caller\nname: "Caller"\npausable: false\nsteps:\n'
        "  - step: run_script\n    script: does-not-exist\n"
    )

    scripts = script_store.load_scripts(tmp_path)

    assert scripts == []


def test_load_scripts_rejects_run_script_with_undeclared_parameter(tmp_path: Path) -> None:
    (tmp_path / "callee.yaml").write_text(MINIMAL_SCRIPT_YAML)
    (tmp_path / "caller.yaml").write_text(
        'id: caller\nname: "Caller"\npausable: false\nsteps:\n'
        "  - step: run_script\n    script: minimal\n    parameters: { bogus: 1 }\n"
    )

    scripts = script_store.load_scripts(tmp_path)

    assert [s.id for s in scripts] == ["minimal"]


def test_load_scripts_rejects_run_script_missing_a_required_parameter(tmp_path: Path) -> None:
    (tmp_path / "callee.yaml").write_text(
        'id: callee\nname: "Callee"\npausable: false\n'
        "parameters:\n  exposureSeconds:\n    type: number\n    required: true\n"
        "steps: []\n"
    )
    (tmp_path / "caller.yaml").write_text(
        'id: caller\nname: "Caller"\npausable: false\nsteps:\n'
        "  - step: run_script\n    script: callee\n"
    )

    scripts = script_store.load_scripts(tmp_path)

    assert [s.id for s in scripts] == ["callee"]


def test_load_scripts_accepts_run_script_with_literal_number_for_integer_yaml_value(
    tmp_path: Path,
) -> None:
    (tmp_path / "callee.yaml").write_text(
        'id: callee\nname: "Callee"\npausable: false\n'
        "parameters:\n  toleranceArcsec:\n    type: number\n    required: true\n"
        "steps: []\n"
    )
    (tmp_path / "caller.yaml").write_text(
        'id: caller\nname: "Caller"\npausable: false\nsteps:\n'
        "  - step: run_script\n    script: callee\n    parameters: { toleranceArcsec: 5 }\n"
    )

    scripts = script_store.load_scripts(tmp_path)

    assert {s.id for s in scripts} == {"callee", "caller"}


def test_load_scripts_rejects_run_script_with_wrong_literal_type(tmp_path: Path) -> None:
    (tmp_path / "callee.yaml").write_text(
        'id: callee\nname: "Callee"\npausable: false\n'
        "parameters:\n  exposureSeconds:\n    type: number\n    required: true\n"
        "steps: []\n"
    )
    (tmp_path / "caller.yaml").write_text(
        'id: caller\nname: "Caller"\npausable: false\nsteps:\n'
        "  - step: run_script\n    script: callee\n"
        '    parameters: { exposureSeconds: "not a number" }\n'
    )

    scripts = script_store.load_scripts(tmp_path)

    assert [s.id for s in scripts] == ["callee"]


def test_load_scripts_rejects_run_script_reference_to_undeclared_caller_parameter(
    tmp_path: Path,
) -> None:
    (tmp_path / "callee.yaml").write_text(
        'id: callee\nname: "Callee"\npausable: false\n'
        "parameters:\n  exposureSeconds:\n    type: number\n    required: true\n"
        "steps: []\n"
    )
    (tmp_path / "caller.yaml").write_text(
        'id: caller\nname: "Caller"\npausable: false\nsteps:\n'
        "  - step: run_script\n    script: callee\n"
        '    parameters: { exposureSeconds: "{{ undeclared }}" }\n'
    )

    scripts = script_store.load_scripts(tmp_path)

    assert [s.id for s in scripts] == ["callee"]


def test_load_scripts_rejects_run_script_reference_with_mismatched_caller_type(
    tmp_path: Path,
) -> None:
    (tmp_path / "callee.yaml").write_text(
        'id: callee\nname: "Callee"\npausable: false\n'
        "parameters:\n  exposureSeconds:\n    type: number\n    required: true\n"
        "steps: []\n"
    )
    (tmp_path / "caller.yaml").write_text(
        'id: caller\nname: "Caller"\npausable: false\n'
        "parameters:\n  exposureSeconds:\n    type: string\n    required: true\n"
        "steps:\n  - step: run_script\n    script: callee\n"
        '    parameters: { exposureSeconds: "{{ exposureSeconds }}" }\n'
    )

    scripts = script_store.load_scripts(tmp_path)

    assert [s.id for s in scripts] == ["callee"]


def test_load_scripts_accepts_run_script_reference_with_matching_caller_type(
    tmp_path: Path,
) -> None:
    (tmp_path / "callee.yaml").write_text(
        'id: callee\nname: "Callee"\npausable: false\n'
        "parameters:\n  exposureSeconds:\n    type: number\n    required: true\n"
        "steps: []\n"
    )
    (tmp_path / "caller.yaml").write_text(
        'id: caller\nname: "Caller"\npausable: false\n'
        "parameters:\n  exposureSeconds:\n    type: number\n    required: true\n"
        "steps:\n  - step: run_script\n    script: callee\n"
        '    parameters: { exposureSeconds: "{{ exposureSeconds }}" }\n'
    )

    scripts = script_store.load_scripts(tmp_path)

    assert {s.id for s in scripts} == {"callee", "caller"}


def test_load_scripts_detects_a_direct_self_call_cycle(tmp_path: Path) -> None:
    (tmp_path / "self-caller.yaml").write_text(
        'id: self-caller\nname: "Self caller"\npausable: false\nsteps:\n'
        "  - step: run_script\n    script: self-caller\n"
    )

    scripts = script_store.load_scripts(tmp_path)

    assert scripts == []


def test_load_scripts_detects_a_three_script_cycle(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text(
        'id: a\nname: "A"\npausable: false\nsteps:\n  - step: run_script\n    script: b\n'
    )
    (tmp_path / "b.yaml").write_text(
        'id: b\nname: "B"\npausable: false\nsteps:\n  - step: run_script\n    script: c\n'
    )
    (tmp_path / "c.yaml").write_text(
        'id: c\nname: "C"\npausable: false\nsteps:\n  - step: run_script\n    script: a\n'
    )
    (tmp_path / "d.yaml").write_text('id: d\nname: "D"\npausable: false\nsteps: []\n')

    scripts = script_store.load_scripts(tmp_path)

    assert [s.id for s in scripts] == ["d"]


def test_load_scripts_finds_cycles_nested_inside_repeat_and_if(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text(
        'id: a\nname: "A"\npausable: false\nsteps:\n'
        "  - step: repeat\n    count: 1\n    steps:\n"
        "      - step: run_script\n        script: b\n"
    )
    (tmp_path / "b.yaml").write_text(
        'id: b\nname: "B"\npausable: false\nsteps:\n'
        "  - step: if\n    condition: { role: camera, property: CONNECTION,"
        ' operator: equals, value: "On" }\n'
        "    then:\n      - step: run_script\n        script: a\n"
    )

    scripts = script_store.load_scripts(tmp_path)

    assert scripts == []


def test_load_scripts_allows_a_valid_run_script_chain(tmp_path: Path) -> None:
    (tmp_path / "cool_camera.yaml").write_text(COOL_CAMERA_YAML)
    (tmp_path / "focus.yaml").write_text(FOCUS_YAML)
    (tmp_path / "capture_sequence.yaml").write_text(CAPTURE_SEQUENCE_YAML)

    scripts = script_store.load_scripts(tmp_path)

    assert {s.id for s in scripts} == {"cool_camera", "focus", "capture_sequence_m101"}


def test_list_scripts_reports_id_name_and_description(tmp_path: Path) -> None:
    (tmp_path / "minimal.yaml").write_text(MINIMAL_SCRIPT_YAML)
    script_store.load_scripts(tmp_path)

    assert script_store.list_scripts() == [
        {"id": "minimal", "name": "Minimal script", "description": None}
    ]


def test_get_script_returns_loaded_script(tmp_path: Path) -> None:
    (tmp_path / "minimal.yaml").write_text(MINIMAL_SCRIPT_YAML)
    script_store.load_scripts(tmp_path)

    script = script_store.get_script("minimal")

    assert script.id == "minimal"


def test_get_script_rejects_unknown_id(tmp_path: Path) -> None:
    script_store.load_scripts(tmp_path)

    with pytest.raises(ValueError, match="Unknown script"):
        script_store.get_script("does-not-exist")


def _minimal_script(script_id: str = "minimal") -> script_store.Script:
    return script_store.Script(id=script_id, name="Minimal script", pausable=False, steps=[])


def test_save_script_writes_a_yaml_file_and_reloads_it(tmp_path: Path) -> None:
    script = _minimal_script()

    saved = script_store.save_script(script, directory=tmp_path)

    assert saved == script
    assert (tmp_path / "minimal.yaml").is_file()
    assert script_store.get_script("minimal") == script


def test_save_script_roundtrips_through_yaml_including_if_else_alias(tmp_path: Path) -> None:
    script = script_store.Script(
        id="with-if",
        name="Has an if/else",
        pausable=False,
        steps=[
            script_store.IfStep(
                step="if",
                condition=script_store.Condition(
                    role="camera", property="CONNECTION", operator="equals", value="Ok"
                ),
                then=[],
                else_=[],
            )
        ],
    )

    script_store.save_script(script, directory=tmp_path)

    raw = yaml.safe_load((tmp_path / "with-if.yaml").read_text())
    assert "else" in raw["steps"][0]
    reloaded = script_store.Script.model_validate(raw)
    assert reloaded == script


def test_save_script_rejects_overwriting_an_existing_file_by_default(tmp_path: Path) -> None:
    script_store.save_script(_minimal_script(), directory=tmp_path)

    with pytest.raises(ValueError, match="already exists"):
        script_store.save_script(_minimal_script(), directory=tmp_path)


def test_save_script_allows_overwrite_when_explicitly_requested(tmp_path: Path) -> None:
    script_store.save_script(_minimal_script(), directory=tmp_path)
    updated = script_store.Script(
        id="minimal", name="Renamed script", pausable=False, steps=[]
    )

    saved = script_store.save_script(updated, overwrite=True, directory=tmp_path)

    assert saved.name == "Renamed script"
    assert script_store.get_script("minimal").name == "Renamed script"


def test_save_script_creates_the_scripts_directory_if_missing(tmp_path: Path) -> None:
    missing_dir = tmp_path / "does-not-exist-yet"

    script_store.save_script(_minimal_script(), directory=missing_dir)

    assert (missing_dir / "minimal.yaml").is_file()


@pytest.mark.parametrize("bad_id", ["", ".", "..", "a/b", "a\\b", "../escape"])
def test_save_script_rejects_ids_that_are_not_safe_filenames(
    tmp_path: Path, bad_id: str
) -> None:
    with pytest.raises(ValueError, match="Invalid script id"):
        script_store.save_script(_minimal_script(bad_id), directory=tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_save_script_rejects_an_id_whose_file_path_is_already_a_directory(
    tmp_path: Path,
) -> None:
    (tmp_path / "minimal.yaml").mkdir()

    with pytest.raises(ValueError, match="is a directory"):
        script_store.save_script(_minimal_script(), directory=tmp_path)


def test_save_script_uses_the_default_directory_when_none_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(script_store.SCRIPTS_DIR_ENV, str(tmp_path))

    script_store.save_script(_minimal_script())

    assert (tmp_path / "minimal.yaml").is_file()


def test_save_script_succeeds_despite_other_invalid_script_files_in_the_directory(
    tmp_path: Path,
) -> None:
    (tmp_path / "broken.yaml").write_text("id: [unterminated")

    saved = script_store.save_script(_minimal_script(), directory=tmp_path)

    assert saved == script_store.get_script("minimal")


def test_save_script_rejects_a_run_script_call_to_an_unknown_script(tmp_path: Path) -> None:
    script = script_store.Script(
        id="caller",
        name="Caller",
        pausable=False,
        steps=[script_store.RunScriptStep(step="run_script", script="does-not-exist")],
    )

    with pytest.raises(ValueError, match="unknown script"):
        script_store.save_script(script, directory=tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_save_script_accepts_a_run_script_call_to_an_existing_script(tmp_path: Path) -> None:
    (tmp_path / "focus.yaml").write_text(FOCUS_YAML)

    script = script_store.Script(
        id="caller",
        name="Caller",
        pausable=False,
        steps=[script_store.RunScriptStep(step="run_script", script="focus")],
    )
    saved = script_store.save_script(script, directory=tmp_path)

    assert saved == script_store.get_script("caller")


def test_save_script_rejects_introducing_a_direct_self_call_cycle(tmp_path: Path) -> None:
    script = script_store.Script(
        id="self-caller",
        name="Self caller",
        pausable=False,
        steps=[script_store.RunScriptStep(step="run_script", script="self-caller")],
    )

    with pytest.raises(ValueError, match="call cycle"):
        script_store.save_script(script, directory=tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_save_script_rejects_breaking_an_existing_callers_argument_type(tmp_path: Path) -> None:
    (tmp_path / "caller.yaml").write_text(
        """
id: caller
name: Caller
pausable: false
steps:
  - step: run_script
    script: callee
    parameters: { value: 1 }
"""
    )
    (tmp_path / "callee.yaml").write_text(
        """
id: callee
name: Callee
pausable: false
parameters:
  value:
    type: number
    required: true
steps: []
"""
    )
    script_store.load_scripts(tmp_path)
    assert {s.id for s in script_store.load_scripts(tmp_path)} == {"caller", "callee"}

    incompatible_callee = script_store.Script(
        id="callee",
        name="Callee",
        pausable=False,
        parameters={"value": script_store.Parameter(type="string", required=True)},
        steps=[],
    )

    with pytest.raises(ValueError, match="breaks caller 'caller'"):
        script_store.save_script(incompatible_callee, overwrite=True, directory=tmp_path)

    assert script_store.Script.model_validate(
        yaml.safe_load((tmp_path / "callee.yaml").read_text())
    ).parameters["value"].type == "number"
