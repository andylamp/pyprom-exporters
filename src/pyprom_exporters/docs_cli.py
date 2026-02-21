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

    """
    args = argv if argv is not None else sys.argv[1:]

    try:
        # pylint: disable=import-outside-toplevel
        from sphinx.cmd.build import main as sphinx_build_main
    except ModuleNotFoundError:
        sys.stderr.write(
            "Sphinx is not installed. Install docs dependencies with "
            "`uv sync --frozen` or `uv sync --frozen --extra docs`.\n",
        )
        raise SystemExit(1) from None

    build_args = list(args) if args else list(DEFAULT_SPHINX_ARGS)
    raise SystemExit(sphinx_build_main(build_args))
