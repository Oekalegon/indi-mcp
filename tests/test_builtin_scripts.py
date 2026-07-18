"""Validates the built-in scripts shipped in the repo's `scripts/` directory.

Unlike `rigs/`/`observatories/`, which are user/hardware-specific and never
committed, primitive/composed scripts (see `docs/Design.md`'s "Composing
scripts" section) are meant to ship with the project. Only `slew` ships so
far (INDIMCP-8); the remaining primitives and a composed sequence are
tracked separately (INDIMCP-41 through INDIMCP-47). This just confirms
whatever's here loads and validates cleanly, the way any script a client
might upload would.
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
