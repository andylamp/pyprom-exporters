# Copyright (c) 2026 pyprom-exporters contributors
# SPDX-License-Identifier: Apache-2.0

"""CLI helpers for documentation workflows."""

from __future__ import annotations

import sys

DEFAULT_SPHINX_ARGS = ["-W", "--keep-going", "-b", "html", "docs", "docs/_build/html"]


def main(argv: list[str] | None = None) -> None:
    """Build project documentation with sensible defaults.

    Parameters
    ----------
    argv : list[str] | None, optional
        Custom arguments forwarded to ``sphinx-build``.
        If omitted, strict HTML build defaults are used.

    Raises
    ------
    SystemExit
        With the Sphinx exit status, or status 1 when Sphinx is unavailable.

    """
    args = argv if argv is not None else sys.argv[1:]

    try:
        # Keep this optional dependency lazy so runtime-only installs can explain how to add it.
        from sphinx.cmd.build import main as sphinx_build_main  # ruff: ignore[import-outside-top-level]
    except ModuleNotFoundError:
        sys.stderr.write(
            "Sphinx is not installed. Install docs dependencies with "
            "`uv sync --locked` or `uv sync --locked --extra docs`.\n",
        )
        raise SystemExit(1) from None

    build_args = list(args) if args else list(DEFAULT_SPHINX_ARGS)
    raise SystemExit(sphinx_build_main(build_args))
