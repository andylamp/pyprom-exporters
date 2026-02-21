"""Sphinx configuration for pyprom-exporters."""
# pylint: disable=invalid-name

from __future__ import annotations

import sys
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

project = "pyprom-exporters"
author = "Andreas A. Grammenos"

try:
    release = metadata.version("pyprom-exporters")
except metadata.PackageNotFoundError:
    release = "0.0.0"
version = release

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx.ext.intersphinx",
]

source_suffix = {
    ".md": "markdown",
}
master_doc = "index"

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

autodoc_default_options = {
    "members": True,
    "member-order": "bysource",
    "show-inheritance": True,
}
autodoc_typehints = "description"
napoleon_google_docstring = False
napoleon_numpy_docstring = True
napoleon_include_init_with_doc = True

myst_enable_extensions = [
    "attrs_block",
    "colon_fence",
    "deflist",
    "fieldlist",
]

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
}

html_theme = "furo"
html_title = "pyprom-exporters documentation"
html_static_path: list[str] = []
