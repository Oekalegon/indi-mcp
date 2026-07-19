"""Validates the built-in scripts shipped in the repo's `scripts/` directory.

Unlike `rigs/`/`observatories/`, which are user/hardware-specific and never
committed, primitive/composed scripts (see `docs/Design.md`'s "Composing
scripts" section) are meant to ship with the project. `slew` (INDIMCP-8),
`park`/`unpark` (INDIMCP-48), and a `connect_*`/`disconnect_*` pair per
device-bearing role (INDIMCP-52) ship so far; the remaining primitives,
tracking control, and a composed sequence are tracked separately
(INDIMCP-41 through INDIMCP-47, INDIMCP-49). This just confirms whatever's
here loads and validates cleanly, the way any script a client might
upload would.
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
    `plate_solve.yaml` declaring `id: plate_solve_until_precision`) would
    fail a stem-vs-id comparison despite loading perfectly correctly.
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


def test_builtin_connect_mount_script_sets_connect_and_waits_on_vector_state() -> None:
    script_store.load_scripts(SCRIPTS_DIR)

    connect_mount = script_store.get_script("connect_mount")

    assert connect_mount.pausable is False
    assert connect_mount.parameters == {}
    assert len(connect_mount.steps) == 2
    set_step, wait_step = connect_mount.steps
    assert isinstance(set_step, script_store.SetPropertyStep)
    assert set_step.role == "mount"
    assert set_step.property == "CONNECTION"
    assert set_step.elements == {"CONNECT": "On"}
    assert isinstance(wait_step, script_store.WaitForStep)
    assert wait_step.condition.role == "mount"
    assert wait_step.condition.property == "CONNECTION"
    assert wait_step.condition.element is None
    assert wait_step.condition.value == "Ok"


def test_builtin_disconnect_mount_script_sets_disconnect_and_waits_on_connect_element() -> None:
    """Unlike connect, CONNECTION's vector state resets to Idle (not Ok) once disconnected —
    confirmed against a real indiserver — so disconnect scripts must wait on the CONNECT
    element going Off rather than on the vector state."""
    script_store.load_scripts(SCRIPTS_DIR)

    disconnect_mount = script_store.get_script("disconnect_mount")

    assert disconnect_mount.pausable is False
    assert disconnect_mount.parameters == {}
    assert len(disconnect_mount.steps) == 2
    set_step, wait_step = disconnect_mount.steps
    assert isinstance(set_step, script_store.SetPropertyStep)
    assert set_step.role == "mount"
    assert set_step.property == "CONNECTION"
    assert set_step.elements == {"DISCONNECT": "On"}
    assert isinstance(wait_step, script_store.WaitForStep)
    assert wait_step.condition.role == "mount"
    assert wait_step.condition.property == "CONNECTION"
    assert wait_step.condition.element == "CONNECT"
    assert wait_step.condition.value == "Off"


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
