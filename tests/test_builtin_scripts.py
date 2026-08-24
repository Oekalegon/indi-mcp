"""Validates the built-in scripts shipped in the repo's `scripts/` directory.

Unlike `rigs/`/`observatories/`, which are user/hardware-specific and never
committed, primitive/composed scripts (see `docs/Design.md`'s "Composing
scripts" section) are meant to ship with the project. `slew` (INDIMCP-8),
`park`/`unpark` (INDIMCP-48), a generic `connect`/`disconnect` pair,
role-parameterized (INDIMCP-52), `cool_camera` (INDIMCP-41), `select_filter`,
`set_focus_position` (INDIMCP-63), `capture_frame` (INDIMCP-44), a set of
composed capture sequences — `capture_light_sequence`, `capture_flat_sequence`,
`capture_dark_sequence`, `capture_bias_sequence` (INDIMCP-46) —,
`sync_filter_names`/`adopt_filter_names_from_driver` (INDIMCP-64), mount
tracking control — `track_off`, `set_track_mode` (generic across
sidereal/solar/lunar/custom via a parameterized `set_property` element key,
INDIMCP-49), `set_custom_tracking_rate` — and `cooler_on`/`cooler_off`
(INDIMCP-84), `abort_exposure` (INDIMCP-86), and `capture_sensor_calibration_set`
(INDIMCP-81) ship so far; the remaining primitives are tracked separately
(INDIMCP-45, INDIMCP-47). This just
confirms whatever's here loads and validates cleanly, the way any script a
client might upload would.
"""

from pathlib import Path

import pytest

from indi_mcp import script_store

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


@pytest.fixture(autouse=True)
def _reset_loaded_scripts() -> None:
    script_store._scripts = {}


def test_builtin_scripts_directory_loads_with_no_errors() -> None:
    """Every `*.yaml` file in `scripts/` loads successfully — none silently dropped.

    Counts, not identity: a script's `id` is independent of its filename
    (`script_store.py`'s own convention — see `load_scripts`), so a future
    built-in script whose filename doesn't exactly match its `id` (e.g.
    `plate_solve_rig.yaml` declaring `id: solve_rig`) would fail a stem-vs-id
    comparison despite loading perfectly correctly.
    """
    on_disk = list(SCRIPTS_DIR.glob("*.yaml"))

    scripts = script_store.load_scripts(SCRIPTS_DIR)

    assert len(scripts) == len(on_disk), "a built-in script file failed to load — check logs"


def test_builtin_slew_script_is_a_thin_wrapper_around_the_slew_step() -> None:
    script_store.load_scripts(SCRIPTS_DIR)

    slew = script_store.get_script("slew")

    assert slew.pausable is False
    assert set(slew.parameters) == {"ra", "dec"}
    assert slew.parameters["ra"].required is True
    assert slew.parameters["dec"].required is True
    assert len(slew.steps) == 1
    step = slew.steps[0]
    assert isinstance(step, script_store.SlewStep)
    assert step.role == "mount"
    assert step.target.raDec is not None
    assert step.target.raDec.ra == "{{ ra }}"
    assert step.target.raDec.dec == "{{ dec }}"


def test_builtin_cool_camera_script_is_a_thin_wrapper_around_the_cool_camera_step() -> None:
    script_store.load_scripts(SCRIPTS_DIR)

    cool_camera = script_store.get_script("cool_camera")

    assert cool_camera.pausable is False
    assert set(cool_camera.parameters) == {"targetTempC", "timeoutSeconds"}
    assert cool_camera.parameters["targetTempC"].required is False
    assert cool_camera.parameters["targetTempC"].default == -10
    assert cool_camera.parameters["timeoutSeconds"].required is False
    assert cool_camera.parameters["timeoutSeconds"].default == 300
    assert len(cool_camera.steps) == 1
    step = cool_camera.steps[0]
    assert isinstance(step, script_store.CoolCameraStep)
    assert step.role == "camera"
    assert step.targetTempC == "{{ targetTempC }}"
    assert step.timeoutSeconds == "{{ timeoutSeconds }}"


def test_builtin_capture_frame_script_is_a_thin_wrapper_around_the_capture_frame_step() -> None:
    script_store.load_scripts(SCRIPTS_DIR)

    capture_frame = script_store.get_script("capture_frame")

    assert capture_frame.pausable is False
    assert set(capture_frame.parameters) == {
        "exposureSeconds",
        "frameType",
        "binningX",
        "binningY",
        "gain",
        "offset",
        "frameX",
        "frameY",
        "frameWidth",
        "frameHeight",
    }
    assert capture_frame.parameters["exposureSeconds"].required is True
    assert capture_frame.parameters["frameType"].required is False
    assert capture_frame.parameters["frameType"].default == "Light"
    assert capture_frame.parameters["binningX"].required is False
    assert capture_frame.parameters["binningX"].default == 1
    assert capture_frame.parameters["binningY"].required is False
    assert capture_frame.parameters["binningY"].default == 1
    assert capture_frame.parameters["gain"].required is False
    assert capture_frame.parameters["gain"].default is None
    assert capture_frame.parameters["offset"].required is False
    assert capture_frame.parameters["offset"].default is None
    for name in ("frameX", "frameY", "frameWidth", "frameHeight"):
        assert capture_frame.parameters[name].required is False
        assert capture_frame.parameters[name].default is None
    assert len(capture_frame.steps) == 1
    step = capture_frame.steps[0]
    assert isinstance(step, script_store.CaptureFrameStep)
    assert step.role == "camera"
    assert step.exposureSeconds == "{{ exposureSeconds }}"
    assert step.frameType == "{{ frameType }}"
    assert step.binningX == "{{ binningX }}"
    assert step.binningY == "{{ binningY }}"
    assert step.gain == "{{ gain }}"
    assert step.offset == "{{ offset }}"
    assert step.frameX == "{{ frameX }}"
    assert step.frameY == "{{ frameY }}"
    assert step.frameWidth == "{{ frameWidth }}"
    assert step.frameHeight == "{{ frameHeight }}"


def test_builtin_plate_solve_rig_script_is_a_thin_wrapper_around_the_plate_solve_step() -> None:
    """The canonical script the tool-surface redesign's Plate solving group (INDIMCP-119)
    runs via run_script in place of the dropped standalone plate_solve/
    plate_solve_until_precision tools that INDIMCP-121 first authored this script to
    supersede — a strict superset of both former scripts, now deleted since nothing
    references them any more: exposureSeconds/syncMount/timeoutSeconds behave like the old
    plate_solve.yaml's; toleranceArcsec/maxAttempts, when set, behave like the old
    plate_solve_until_precision.yaml's.
    """
    script_store.load_scripts(SCRIPTS_DIR)

    script = script_store.get_script("plate_solve_rig")

    assert script.pausable is False
    assert set(script.parameters) == {
        "exposureSeconds",
        "syncMount",
        "toleranceArcsec",
        "maxAttempts",
        "timeoutSeconds",
    }
    assert script.parameters["exposureSeconds"].required is False
    assert script.parameters["exposureSeconds"].default is None
    assert script.parameters["syncMount"].required is False
    assert script.parameters["syncMount"].default is True
    assert script.parameters["toleranceArcsec"].required is False
    assert script.parameters["toleranceArcsec"].default is None
    assert script.parameters["maxAttempts"].required is False
    assert script.parameters["maxAttempts"].default == 3
    assert script.parameters["timeoutSeconds"].required is False
    assert script.parameters["timeoutSeconds"].default == 60
    assert len(script.steps) == 1
    step = script.steps[0]
    assert isinstance(step, script_store.PlateSolveStep)
    assert step.role == "camera"
    assert step.mountRole == "mount"
    assert step.exposureSeconds == "{{ exposureSeconds }}"
    assert step.syncMount == "{{ syncMount }}"
    assert step.toleranceArcsec == "{{ toleranceArcsec }}"
    assert step.maxAttempts == "{{ maxAttempts }}"
    assert step.timeoutSeconds == "{{ timeoutSeconds }}"


def test_builtin_park_script_sets_park_and_waits() -> None:
    script_store.load_scripts(SCRIPTS_DIR)

    park = script_store.get_script("park")

    assert park.pausable is False
    assert park.parameters == {}
    assert len(park.steps) == 2
    set_step, wait_step = park.steps
    assert isinstance(set_step, script_store.SetPropertyStep)
    assert set_step.role == "mount"
    assert set_step.property == "TELESCOPE_PARK"
    assert set_step.elements == {"PARK": "On"}
    assert isinstance(wait_step, script_store.WaitForStep)
    assert wait_step.condition.role == "mount"
    assert wait_step.condition.property == "TELESCOPE_PARK"
    assert wait_step.condition.element is None
    assert wait_step.condition.value == "Ok"


def test_builtin_connect_script_is_role_parameterized_and_waits_on_vector_state() -> None:
    """One generic script covers every device-bearing role: `role` is a required parameter
    substituted into each step's `role` field, resolved before any step runs."""
    script_store.load_scripts(SCRIPTS_DIR)

    connect = script_store.get_script("connect")

    assert connect.pausable is False
    assert set(connect.parameters) == {"role"}
    assert connect.parameters["role"].required is True
    assert len(connect.steps) == 2
    set_step, wait_step = connect.steps
    assert isinstance(set_step, script_store.SetPropertyStep)
    assert set_step.role == "{{ role }}"
    assert set_step.property == "CONNECTION"
    assert set_step.elements == {"CONNECT": "On"}
    assert isinstance(wait_step, script_store.WaitForStep)
    assert wait_step.condition.role == "{{ role }}"
    assert wait_step.condition.property == "CONNECTION"
    assert wait_step.condition.element is None
    assert wait_step.condition.value == "Ok"


def test_builtin_disconnect_script_is_role_parameterized_and_waits_on_connect_element() -> None:
    """Unlike connect, CONNECTION's vector state resets to Idle (not Ok) once disconnected —
    confirmed against a real indiserver — so disconnect waits on the CONNECT element going
    Off rather than on the vector state."""
    script_store.load_scripts(SCRIPTS_DIR)

    disconnect = script_store.get_script("disconnect")

    assert disconnect.pausable is False
    assert set(disconnect.parameters) == {"role"}
    assert disconnect.parameters["role"].required is True
    assert len(disconnect.steps) == 2
    set_step, wait_step = disconnect.steps
    assert isinstance(set_step, script_store.SetPropertyStep)
    assert set_step.role == "{{ role }}"
    assert set_step.property == "CONNECTION"
    assert set_step.elements == {"DISCONNECT": "On"}
    assert isinstance(wait_step, script_store.WaitForStep)
    assert wait_step.condition.role == "{{ role }}"
    assert wait_step.condition.property == "CONNECTION"
    assert wait_step.condition.element == "CONNECT"
    assert wait_step.condition.value == "Off"


def test_builtin_cooler_on_script_sets_cooler_on_and_waits() -> None:
    script_store.load_scripts(SCRIPTS_DIR)

    cooler_on = script_store.get_script("cooler_on")

    assert cooler_on.pausable is False
    assert cooler_on.parameters == {}
    assert len(cooler_on.steps) == 2
    set_step, wait_step = cooler_on.steps
    assert isinstance(set_step, script_store.SetPropertyStep)
    assert set_step.role == "camera"
    assert set_step.property == "CCD_COOLER"
    assert set_step.elements == {"COOLER_ON": "On"}
    assert isinstance(wait_step, script_store.WaitForStep)
    assert wait_step.condition.role == "camera"
    assert wait_step.condition.property == "CCD_COOLER"
    assert wait_step.condition.element == "COOLER_ON"
    assert wait_step.condition.value == "On"


def test_builtin_abort_exposure_script_sets_abort_and_waits() -> None:
    script_store.load_scripts(SCRIPTS_DIR)

    abort_exposure = script_store.get_script("abort_exposure")

    assert abort_exposure.pausable is False
    assert abort_exposure.parameters == {}
    assert len(abort_exposure.steps) == 2
    set_step, wait_step = abort_exposure.steps
    assert isinstance(set_step, script_store.SetPropertyStep)
    assert set_step.role == "camera"
    assert set_step.property == "CCD_ABORT_EXPOSURE"
    assert set_step.elements == {"ABORT": "On"}
    assert isinstance(wait_step, script_store.WaitForStep)
    assert wait_step.condition.role == "camera"
    assert wait_step.condition.property == "CCD_ABORT_EXPOSURE"
    assert wait_step.condition.element is None
    assert wait_step.condition.value == "Ok"


def test_builtin_cooler_off_script_sets_cooler_off_and_waits() -> None:
    script_store.load_scripts(SCRIPTS_DIR)

    cooler_off = script_store.get_script("cooler_off")

    assert cooler_off.pausable is False
    assert cooler_off.parameters == {}
    assert len(cooler_off.steps) == 2
    set_step, wait_step = cooler_off.steps
    assert isinstance(set_step, script_store.SetPropertyStep)
    assert set_step.role == "camera"
    assert set_step.property == "CCD_COOLER"
    assert set_step.elements == {"COOLER_OFF": "On"}
    assert isinstance(wait_step, script_store.WaitForStep)
    assert wait_step.condition.role == "camera"
    assert wait_step.condition.property == "CCD_COOLER"
    assert wait_step.condition.element == "COOLER_OFF"
    assert wait_step.condition.value == "On"


def test_builtin_select_filter_script_is_a_thin_wrapper_around_the_select_filter_step() -> None:
    script_store.load_scripts(SCRIPTS_DIR)

    select_filter = script_store.get_script("select_filter")

    assert select_filter.pausable is False
    assert set(select_filter.parameters) == {"filterName"}
    assert select_filter.parameters["filterName"].required is True
    assert len(select_filter.steps) == 1
    step = select_filter.steps[0]
    assert isinstance(step, script_store.SelectFilterStep)
    assert step.role == "filterWheel"
    assert step.filterName == "{{ filterName }}"
    assert step.slot is None


def test_builtin_set_focus_position_script_is_a_thin_wrapper_around_its_step() -> None:
    script_store.load_scripts(SCRIPTS_DIR)

    set_focus_position = script_store.get_script("set_focus_position")

    assert set_focus_position.pausable is False
    assert set(set_focus_position.parameters) == {"position"}
    assert set_focus_position.parameters["position"].required is True
    assert len(set_focus_position.steps) == 1
    step = set_focus_position.steps[0]
    assert isinstance(step, script_store.SetFocusPositionStep)
    assert step.role == "focuser"
    assert step.position == "{{ position }}"


def test_builtin_unpark_script_sets_unpark_and_waits() -> None:
    script_store.load_scripts(SCRIPTS_DIR)

    unpark = script_store.get_script("unpark")

    assert unpark.pausable is False
    assert unpark.parameters == {}
    assert len(unpark.steps) == 2
    set_step, wait_step = unpark.steps
    assert isinstance(set_step, script_store.SetPropertyStep)
    assert set_step.role == "mount"
    assert set_step.property == "TELESCOPE_PARK"
    assert set_step.elements == {"UNPARK": "On"}
    assert isinstance(wait_step, script_store.WaitForStep)
    assert wait_step.condition.role == "mount"
    assert wait_step.condition.property == "TELESCOPE_PARK"
    assert wait_step.condition.element is None
    assert wait_step.condition.value == "Ok"


def test_builtin_track_off_script_sets_track_state_off_and_waits() -> None:
    script_store.load_scripts(SCRIPTS_DIR)

    track_off = script_store.get_script("track_off")

    assert track_off.pausable is False
    assert track_off.parameters == {}
    assert len(track_off.steps) == 2
    set_step, wait_step = track_off.steps
    assert isinstance(set_step, script_store.SetPropertyStep)
    assert set_step.role == "mount"
    assert set_step.property == "TELESCOPE_TRACK_STATE"
    assert set_step.elements == {"TRACK_OFF": "On"}
    assert isinstance(wait_step, script_store.WaitForStep)
    assert wait_step.condition.role == "mount"
    assert wait_step.condition.property == "TELESCOPE_TRACK_STATE"
    assert wait_step.condition.element is None
    assert wait_step.condition.value == "Ok"


def test_builtin_set_track_mode_script_substitutes_the_switch_element_and_waits() -> None:
    """One generic script covers every tracking mode: `modeSwitchElement` is substituted
    into the `set_property` step's own `elements` *key* (INDIMCP-49), not just a value — the
    engine capability that replaced three near-duplicate per-mode scripts."""
    script_store.load_scripts(SCRIPTS_DIR)

    set_track_mode = script_store.get_script("set_track_mode")

    assert set_track_mode.pausable is False
    assert set(set_track_mode.parameters) == {"modeSwitchElement"}
    assert set_track_mode.parameters["modeSwitchElement"].required is True
    assert len(set_track_mode.steps) == 2
    set_step, wait_step = set_track_mode.steps
    assert isinstance(set_step, script_store.SetPropertyStep)
    assert set_step.role == "mount"
    assert set_step.property == "TELESCOPE_TRACK_MODE"
    assert set_step.elements == {"{{ modeSwitchElement }}": "On"}
    assert isinstance(wait_step, script_store.WaitForStep)
    assert wait_step.condition.role == "mount"
    assert wait_step.condition.property == "TELESCOPE_TRACK_MODE"
    assert wait_step.condition.element is None
    assert wait_step.condition.value == "Ok"


def test_builtin_set_custom_tracking_rate_script_composes_set_track_mode_then_sets_rate() -> None:
    script_store.load_scripts(SCRIPTS_DIR)

    script = script_store.get_script("set_custom_tracking_rate")

    assert script.pausable is False
    assert set(script.parameters) == {"raRateArcsecPerSec", "decRateArcsecPerSec"}
    assert script.parameters["raRateArcsecPerSec"].required is True
    assert script.parameters["decRateArcsecPerSec"].required is True
    assert len(script.steps) == 3
    mode_step, rate_step, rate_wait = script.steps
    assert isinstance(mode_step, script_store.RunScriptStep)
    assert mode_step.script == "set_track_mode"
    assert mode_step.parameters == {"modeSwitchElement": "TRACK_CUSTOM"}
    assert isinstance(rate_step, script_store.SetPropertyStep)
    assert rate_step.role == "mount"
    assert rate_step.property == "TELESCOPE_TRACK_RATE"
    assert rate_step.elements == {
        "TRACK_RATE_RA": "{{ raRateArcsecPerSec }}",
        "TRACK_RATE_DE": "{{ decRateArcsecPerSec }}",
    }
    assert isinstance(rate_wait, script_store.WaitForStep)
    assert rate_wait.condition.property == "TELESCOPE_TRACK_RATE"
    assert rate_wait.condition.value == "Ok"


def test_builtin_capture_light_sequence_composes_cool_slew_filter_focus_and_repeat() -> None:
    """A general-purpose composed sequence (INDIMCP-46), following docs/ScriptSchema.md's
    own top-of-doc example, but with a fixed raDec/focusPosition rather than objectName
    resolution or an autofocus loop, since neither primitive exists yet."""
    script_store.load_scripts(SCRIPTS_DIR)

    lights = script_store.get_script("capture_light_sequence")

    assert lights.pausable is True
    assert set(lights.parameters) == {
        "ra",
        "dec",
        "objectName",
        "filterName",
        "focusPosition",
        "targetTempC",
        "exposureSeconds",
        "count",
        "gain",
        "offset",
        "binningX",
        "binningY",
        "frameX",
        "frameY",
        "frameWidth",
        "frameHeight",
    }
    assert lights.parameters["ra"].required is True
    assert lights.parameters["dec"].required is True
    assert lights.parameters["objectName"].required is False
    assert lights.parameters["filterName"].required is True
    assert lights.parameters["focusPosition"].required is True
    assert lights.parameters["targetTempC"].required is False
    assert lights.parameters["targetTempC"].default == -10
    assert lights.parameters["exposureSeconds"].required is True
    assert lights.parameters["count"].required is True
    assert lights.parameters["gain"].required is False
    assert lights.parameters["offset"].required is False
    assert lights.parameters["binningX"].required is False
    assert lights.parameters["binningX"].default == 1
    assert lights.parameters["binningY"].required is False
    assert lights.parameters["binningY"].default == 1
    assert lights.parameters["frameX"].required is False
    assert lights.parameters["frameY"].required is False
    assert lights.parameters["frameWidth"].required is False
    assert lights.parameters["frameHeight"].required is False
    assert len(lights.steps) == 5
    cool_step, slew_step, filter_step, focus_step, repeat_step = lights.steps
    assert isinstance(cool_step, script_store.RunScriptStep)
    assert cool_step.script == "cool_camera"
    assert cool_step.parameters == {"targetTempC": "{{ targetTempC }}"}
    assert isinstance(slew_step, script_store.SlewStep)
    assert slew_step.role == "mount"
    assert slew_step.target.raDec is not None
    assert slew_step.target.raDec.ra == "{{ ra }}"
    assert slew_step.target.raDec.dec == "{{ dec }}"
    assert slew_step.target.objectName is None
    assert isinstance(filter_step, script_store.SelectFilterStep)
    assert filter_step.role == "filterWheel"
    assert filter_step.filterName == "{{ filterName }}"
    assert isinstance(focus_step, script_store.SetFocusPositionStep)
    assert focus_step.role == "focuser"
    assert focus_step.position == "{{ focusPosition }}"
    assert isinstance(repeat_step, script_store.RepeatStep)
    assert repeat_step.count == "{{ count }}"
    assert len(repeat_step.steps) == 1
    capture_step = repeat_step.steps[0]
    assert isinstance(capture_step, script_store.CaptureFrameStep)
    assert capture_step.role == "camera"
    assert capture_step.exposureSeconds == "{{ exposureSeconds }}"
    assert capture_step.frameType == "Light"
    assert capture_step.objectName == "{{ objectName }}"
    assert capture_step.gain == "{{ gain }}"
    assert capture_step.offset == "{{ offset }}"
    assert capture_step.binningX == "{{ binningX }}"
    assert capture_step.binningY == "{{ binningY }}"
    assert capture_step.frameX == "{{ frameX }}"
    assert capture_step.frameY == "{{ frameY }}"
    assert capture_step.frameWidth == "{{ frameWidth }}"
    assert capture_step.frameHeight == "{{ frameHeight }}"


def test_builtin_capture_flat_sequence_skips_mount_and_cooling() -> None:
    script_store.load_scripts(SCRIPTS_DIR)

    flats = script_store.get_script("capture_flat_sequence")

    assert flats.pausable is True
    assert set(flats.parameters) == {
        "filterName",
        "focusPosition",
        "exposureSeconds",
        "count",
        "gain",
        "offset",
    }
    assert flats.parameters["exposureSeconds"].required is True
    assert flats.parameters["count"].required is True
    assert flats.parameters["gain"].required is False
    assert flats.parameters["offset"].required is False
    assert len(flats.steps) == 3
    filter_step, focus_step, repeat_step = flats.steps
    assert isinstance(filter_step, script_store.SelectFilterStep)
    assert isinstance(focus_step, script_store.SetFocusPositionStep)
    assert isinstance(repeat_step, script_store.RepeatStep)
    assert repeat_step.count == "{{ count }}"
    capture_step = repeat_step.steps[0]
    assert isinstance(capture_step, script_store.CaptureFrameStep)
    assert capture_step.frameType == "Flat"
    assert capture_step.gain == "{{ gain }}"
    assert capture_step.offset == "{{ offset }}"


def test_builtin_capture_dark_sequence_skips_mount_filter_and_focus() -> None:
    script_store.load_scripts(SCRIPTS_DIR)

    darks = script_store.get_script("capture_dark_sequence")

    assert darks.pausable is True
    assert set(darks.parameters) == {
        "targetTempC",
        "exposureSeconds",
        "count",
        "gain",
        "offset",
        "binningX",
        "binningY",
        "frameX",
        "frameY",
        "frameWidth",
        "frameHeight",
    }
    assert darks.parameters["targetTempC"].default == -10
    assert darks.parameters["exposureSeconds"].required is True
    assert darks.parameters["count"].required is True
    assert darks.parameters["gain"].required is False
    assert darks.parameters["offset"].required is False
    assert darks.parameters["binningX"].required is False
    assert darks.parameters["binningX"].default == 1
    assert darks.parameters["binningY"].required is False
    assert darks.parameters["binningY"].default == 1
    assert darks.parameters["frameX"].required is False
    assert darks.parameters["frameY"].required is False
    assert darks.parameters["frameWidth"].required is False
    assert darks.parameters["frameHeight"].required is False
    assert len(darks.steps) == 2
    cool_step, repeat_step = darks.steps
    assert isinstance(cool_step, script_store.RunScriptStep)
    assert cool_step.script == "cool_camera"
    assert isinstance(repeat_step, script_store.RepeatStep)
    assert repeat_step.count == "{{ count }}"
    capture_step = repeat_step.steps[0]
    assert isinstance(capture_step, script_store.CaptureFrameStep)
    assert capture_step.frameType == "Dark"
    assert capture_step.gain == "{{ gain }}"
    assert capture_step.offset == "{{ offset }}"
    assert capture_step.binningX == "{{ binningX }}"
    assert capture_step.binningY == "{{ binningY }}"
    assert capture_step.frameX == "{{ frameX }}"
    assert capture_step.frameY == "{{ frameY }}"
    assert capture_step.frameWidth == "{{ frameWidth }}"
    assert capture_step.frameHeight == "{{ frameHeight }}"


def test_builtin_capture_bias_sequence_has_no_setup_steps() -> None:
    script_store.load_scripts(SCRIPTS_DIR)

    bias = script_store.get_script("capture_bias_sequence")

    assert bias.pausable is True
    assert set(bias.parameters) == {
        "exposureSeconds",
        "count",
        "gain",
        "offset",
        "binningX",
        "binningY",
        "frameX",
        "frameY",
        "frameWidth",
        "frameHeight",
    }
    assert bias.parameters["exposureSeconds"].required is False
    assert bias.parameters["exposureSeconds"].default == 0.0
    assert bias.parameters["count"].required is True
    assert bias.parameters["gain"].required is False
    assert bias.parameters["offset"].required is False
    assert bias.parameters["binningX"].required is False
    assert bias.parameters["binningX"].default == 1
    assert bias.parameters["binningY"].required is False
    assert bias.parameters["binningY"].default == 1
    assert bias.parameters["frameX"].required is False
    assert bias.parameters["frameY"].required is False
    assert bias.parameters["frameWidth"].required is False
    assert bias.parameters["frameHeight"].required is False
    assert len(bias.steps) == 1
    repeat_step = bias.steps[0]
    assert isinstance(repeat_step, script_store.RepeatStep)
    assert repeat_step.count == "{{ count }}"
    capture_step = repeat_step.steps[0]
    assert isinstance(capture_step, script_store.CaptureFrameStep)
    assert capture_step.role == "camera"
    assert capture_step.frameType == "Bias"
    assert capture_step.gain == "{{ gain }}"
    assert capture_step.offset == "{{ offset }}"
    assert capture_step.binningX == "{{ binningX }}"
    assert capture_step.binningY == "{{ binningY }}"
    assert capture_step.frameX == "{{ frameX }}"
    assert capture_step.frameY == "{{ frameY }}"
    assert capture_step.frameWidth == "{{ frameWidth }}"
    assert capture_step.frameHeight == "{{ frameHeight }}"


def test_builtin_capture_sensor_calibration_set_captures_bias_and_flat_dark_only() -> None:
    """Trimmed to bias + flat-dark only (INDIMCP-102) — flats need a manually staged
    light source the script engine can't provide, so they're captured separately by
    capture_flat_sequence instead (see docs/SensorCalibration.md)."""
    script_store.load_scripts(SCRIPTS_DIR)

    calibration_set = script_store.get_script("capture_sensor_calibration_set")

    assert calibration_set.pausable is True
    assert set(calibration_set.parameters) == {
        "gain",
        "offset",
        "biasExposureSeconds",
        "flatExposureSeconds",
        "biasCount",
        "darkCount",
    }
    assert calibration_set.parameters["gain"].required is False
    assert calibration_set.parameters["offset"].required is False
    assert calibration_set.parameters["biasExposureSeconds"].required is False
    assert calibration_set.parameters["biasExposureSeconds"].default == 0.0
    assert calibration_set.parameters["flatExposureSeconds"].required is True
    assert calibration_set.parameters["biasCount"].required is True
    assert calibration_set.parameters["darkCount"].required is True
    assert len(calibration_set.steps) == 2

    bias_repeat, flat_dark_repeat = calibration_set.steps

    assert isinstance(bias_repeat, script_store.RepeatStep)
    assert bias_repeat.count == "{{ biasCount }}"
    bias_capture = bias_repeat.steps[0]
    assert isinstance(bias_capture, script_store.CaptureFrameStep)
    assert bias_capture.role == "camera"
    assert bias_capture.gain == "{{ gain }}"
    assert bias_capture.offset == "{{ offset }}"
    assert bias_capture.frameType == "Bias"
    assert bias_capture.exposureSeconds == "{{ biasExposureSeconds }}"

    assert isinstance(flat_dark_repeat, script_store.RepeatStep)
    assert flat_dark_repeat.count == "{{ darkCount }}"
    flat_dark_capture = flat_dark_repeat.steps[0]
    assert isinstance(flat_dark_capture, script_store.CaptureFrameStep)
    assert flat_dark_capture.role == "camera"
    assert flat_dark_capture.gain == "{{ gain }}"
    assert flat_dark_capture.offset == "{{ offset }}"
    assert flat_dark_capture.frameType == "Dark"
    assert flat_dark_capture.exposureSeconds == "{{ flatExposureSeconds }}"
