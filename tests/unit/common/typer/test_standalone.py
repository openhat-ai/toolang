"""The two Typer extensions work without importing Toolang."""

import subprocess
import sys

import pytest

from tests import PROJECT_ROOT


@pytest.mark.parametrize("name", ["ui", "options"])
def test_extension_can_be_copied_and_used_alone(name, tmp_path):
    source = PROJECT_ROOT / "src/toolang/common/typer" / f"{name}.py"
    target = tmp_path / "extension.py"
    target.write_text(source.read_text())
    invocation = (
        "raise SystemExit(extension.run(app, args=['--help'], prog_name='demo'))"
        if name == "ui"
        else "app(args=['--model'], prog_name='demo')"
    )
    script = f"""
import sys
import typer
import extension

class OptionalCommand(getattr(extension, 'OptionalValueCommand', typer.core.TyperCommand)):
    optional_values = {{'model': 'auto'}}

app = typer.Typer(add_completion=False)

@app.command(cls=OptionalCommand)
def show(model: str = ''):
    typer.echo(model)

assert not any(name == 'toolang' or name.startswith('toolang.') for name in sys.modules)
{invocation}
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, text=True, capture_output=True
    )
    assert result.returncode == 0, result.stderr
    assert ("Usage: demo" if name == "ui" else "auto") in result.stdout
