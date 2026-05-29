"""Compatibility wrapper for the standalone caps CLI."""

from __future__ import annotations

from .caps.main import *  # noqa: F403


if __name__ == "__main__":
    from .caps.main import main

    raise SystemExit(main())
