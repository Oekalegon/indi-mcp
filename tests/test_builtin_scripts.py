"""Validates the built-in scripts shipped in the repo's `scripts/` directory.

Unlike `rigs/`/`observatories/`, which are user/hardware-specific and never
committed, the primitive scripts named in `docs/Design.md`'s "Composing
scripts" section (`cool_camera`, `slew`, `plate_solve_until_precision`,
`select_filter`, `focus`, `capture_frame`) are meant to ship with the
project (INDIMCP-8) — this just confirms they load and validate cleanly,
the way any script a client might upload would.
"""

from pathlib import Path

import pytest

from indi_mcp import script_store

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


@pytest.fixture(autouse=True)
def _reset_loaded_scripts() -> None:
    script_store._scripts = {}


def test_builtin_scripts_directory_loads_with_no_errors() -> None:
    scripts = script_store.load_scripts(SCRIPTS_DIR)

    loaded_ids = {s.id for s in scripts}
    on_disk = {path.stem for path in SCRIPTS_DIR.glob("*.yaml")}
    assert loaded_ids == on_disk, "a built-in script file failed to load — check logs"


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
