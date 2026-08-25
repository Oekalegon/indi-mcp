"""Sphinx configuration for the indi-mcp documentation."""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

project = "indi-mcp"
copyright = f"{date.today().year}, Don Willems"
author = "Don Willems"

extensions = [
    "autoapi.extension",
    "sphinx.ext.napoleon",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
    "myst_parser",
]

source_suffix = {
    ".rst": "restructuredtext",
    ".md": "markdown",
}

exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

# -- AutoAPI (Python API extraction, DocC-style) ----------------------------
autoapi_type = "python"
autoapi_dirs = ["../../src/indi_mcp"]
autoapi_root = "api"
autoapi_add_toctree_entry = True
autoapi_options = [
    "members",
    "undoc-members",
    "show-inheritance",
    "show-module-summary",
]
autoapi_python_class_content = "both"
autoapi_keep_files = True
"""Keep AutoAPI's generated `api/*.rst` on disk after the build (gitignored, see
`.gitignore`) instead of deleting them — makes a docutils warning/error pointing at a
generated `.rst` file actually inspectable locally, rather than needing a second build with
this flag flipped on just to see what triggered it."""

suppress_warnings = ["autoapi.python_import_resolution"]
"""`INDI_PORT` is defined in `indiweb.indi_server` (outside `autoapi_dirs`) and re-exported
via `from indiweb.indi_server import INDI_PORT` in `indi_mcp.indi_server` — AutoAPI's static
import resolver can't follow a re-export back to a module it isn't documenting, so every
downstream `from indi_mcp.indi_server import INDI_PORT` (`indi_messaging`, `server`, `cli`)
warns "Cannot resolve import" even though the symbol renders correctly. Suppressing the whole
category rather than the three specific instances since this is a structural limitation of
following any third-party re-export, not something fixable per docstring — a real new
unresolved-import warning would most likely be the same underlying (harmless) cause."""

napoleon_google_docstring = True
napoleon_numpy_docstring = False

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
}

# -- HTML output --------------------------------------------------------
html_theme = "furo"
html_static_path = ["_static"]
