"""Compatibility wrapper for the Toolang CLI entrypoint."""

from __future__ import annotations

from .toolang.main import *  # noqa: F403


if __name__ == "__main__":
    from .toolang.main import main

    raise SystemExit(main())
