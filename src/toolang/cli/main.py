"""Compatibility wrapper for the Toolang CLI entrypoint."""

from __future__ import annotations

from .toolang.app import *  # noqa: F403


if __name__ == "__main__":
    from .toolang.app import main

    raise SystemExit(main())
