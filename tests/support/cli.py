"""Exercise script signatures through the public CLI entry point."""

from contextlib import nullcontext
import sys
from typing import TextIO
from unittest.mock import patch

from toolang.cli.toolang.main import app
from toolang.common.typer.ui import run


def invoke_script(
    args: list[str], *, prog_name: str = "too", stdin: TextIO | None = None
) -> int:
    with patch.object(sys, "stdin", stdin) if stdin is not None else nullcontext():
        return run(app, args=["run", "--", *args], prog_name=prog_name)
