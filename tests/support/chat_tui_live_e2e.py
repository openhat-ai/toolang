"""Subprocess entry point for real-provider chat TUI system tests."""

from __future__ import annotations

from pathlib import Path
import sys

from .chat_tui_runner import run_chat_tui
from .live_provider import create_live_agent


def main() -> None:
    root = Path(sys.argv[1])
    model = sys.argv[2]
    kind = sys.argv[3]
    runnable = {"agic": "smoke", "flow": "relay"}[kind]
    setup, state = create_live_agent(root, model=model)
    run_chat_tui(
        setup,
        state,
        selects={kind: runnable, "model": model},
        models=(model,),
    )


if __name__ == "__main__":
    main()
