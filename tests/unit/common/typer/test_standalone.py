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


def test_extensions_share_declarative_help_without_package_imports(tmp_path):
    for name in ("ui", "options"):
        source = PROJECT_ROOT / "src/toolang/common/typer" / f"{name}.py"
        (tmp_path / f"{name}_extension.py").write_text(source.read_text())
    script = """
import sys
import typer
import ui_extension
from options_extension import OptionalValue, OptionalValueCommand

class Command(OptionalValueCommand):
    optional_values = {"model": OptionalValue(bare_value="auto")}

app = typer.Typer(add_completion=False)
@app.command(cls=Command)
def show(model: str = typer.Option("installed", envvar="DEMO_MODEL")):
    raise AssertionError("help executed the command")

assert not any(name == 'toolang' or name.startswith('toolang.') for name in sys.modules)
raise SystemExit(ui_extension.run(app, args=['--help'], prog_name='demo'))
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, text=True, capture_output=True
    )
    assert result.returncode == 0, result.stderr
    assert "[env: DEMO_MODEL=] [default: installed] [bare: auto]" in " ".join(
        result.stdout.split()
    )
