"""Standalone topic help preserves native parsing and offline presentation."""

from io import StringIO
from pathlib import Path
import re
import shlex
import subprocess
import sys

import pytest
from rich.cells import cell_len
from rich.console import Console
from rich.text import Text

from toolang.cli.common.policy import (
    _ALLOW_FIELDS,
    _DEFAULT_FIELDS,
    _LIMIT_FIELDS,
    resolve_ceiling_overrides,
    resolve_compact_override,
    resolve_default_overrides,
    resolve_limit_overrides,
)
from toolang.cli.toolang.main import app
from toolang.common.typer.ui import PLAIN, UV, run


TOPICS = ("allow", "limit", "default", "model")


def _invoke(arguments, *, prog_name="too", width=80, theme=PLAIN, color_system=None):
    stdout, stderr = StringIO(), StringIO()
    console = Console(
        file=stdout,
        width=width,
        force_terminal=True,
        color_system=color_system,
        no_color=False,
    )
    status = run(
        app,
        args=arguments,
        prog_name=prog_name,
        theme=theme,
        console=console,
        error_console=Console(file=stderr, width=width, color_system=None),
        terminal_width=width,
    )
    return status, Text.from_ansi(stdout.getvalue()), stderr.getvalue(), console


@pytest.mark.parametrize("prog_name", ["too", "toolang"])
@pytest.mark.parametrize("topic", [None, *TOPICS])
def test_topic_help_aliases_render_the_selected_reference(prog_name, topic):
    arguments = ["help", *([topic] if topic else [])]
    outputs = []
    for tail in ([], ["-h"], ["--help"]):
        status, output, error, _ = _invoke([*arguments, *tail], prog_name=prog_name)
        assert status == 0 and not error
        outputs.append(output.plain)
    assert outputs[0] == outputs[1] == outputs[2]
    output = outputs[0]
    if topic is None:
        assert f"Usage: {prog_name} help [OPTIONS] [TOPIC]" in output
        rows = output.split("Topics:\n", 1)[1].split("\n\n", 1)[0]
        assert tuple(line.split()[0] for line in rows.splitlines()) == TOPICS
        assert "Run 'too help TOPIC' for details." in output
        assert "Use 'too COMMAND --help' for command-specific options." in output
    else:
        assert "Usage:" not in output and "Options:" not in output
        assert "Show this message and exit" not in output and "Rules:" not in output
        assert "Examples:" in output
        assert output.startswith(
            "Model selection\n" if topic == "model" else f"--{topic} <KEY>=<VALUE>\n"
        )


@pytest.mark.parametrize(
    ("arguments", "diagnostic"),
    [
        (["help", "unknown"], "No such command 'unknown'"),
        (["help", "chat"], "No such command 'chat'"),
        (["help", "ALLOW"], "No such command 'ALLOW'"),
        (["help", "--unknown"], "No such option: --unknown"),
        (["help", "allow", "--unknown"], "No such option: --unknown"),
        (["help", "allow", "extra"], "Got unexpected extra argument(s) (extra)"),
    ],
)
def test_invalid_topic_invocations_use_native_stderr_diagnostics(arguments, diagnostic):
    status, output, error, _ = _invoke(arguments)
    assert status == 2 and not output.plain
    assert diagnostic in error
    assert "Traceback" not in error


@pytest.mark.parametrize(
    "prefix", ["gus", "missing.too", "https://example.com/gus.too"]
)
def test_topics_reject_agent_or_file_prefixes_without_preparation(prefix):
    status, output, error, _ = _invoke([prefix, "help"])
    assert status == 2 and not output.plain
    assert "Usage: too help [OPTIONS] [TOPIC]" in error
    assert "Command 'help' does not accept an agent prefix." in error
    assert "Traceback" not in error


@pytest.mark.parametrize("topic", TOPICS)
@pytest.mark.parametrize("theme", [PLAIN, UV])
@pytest.mark.parametrize("width", [44, 80, 120])
@pytest.mark.parametrize("color_system", [None, "standard"])
def test_topics_wrap_and_distinguish_definitions_from_explanations(
    topic, theme, width, color_system
):
    status, output, error, console = _invoke(
        ["help", topic], theme=theme, width=width, color_system=color_system
    )
    assert status == 0 and not error
    assert all(cell_len(line) <= width for line in output.plain.splitlines())
    assert "Rules:" not in output.plain
    assert "Keys:" not in output.plain and "Values:" not in output.plain
    if color_system is None:
        return
    key, label, explanation = {
        "allow": ("models", "QUERY", "Match models"),
        "limit": ("agic_model_calls", "INTEGER", "Non-negative"),
        "default": ("runnable", "REF", "Entry ref"),
        "model": ("effort", "LEVEL", "Reasoning level"),
    }[topic]
    assignment = re.search(rf"^ *({key})=", output.plain, re.M)
    assert assignment is not None
    key_start = assignment.start(1)
    key_style = output.get_style_at_offset(console, key_start)
    metavar_style = output.get_style_at_offset(console, key_start + len(key) + 1)
    section_start = output.plain.index("Possible values:", key_start)
    label_style = output.get_style_at_offset(
        console, output.plain.index(label, section_start)
    )
    prose_style = output.get_style_at_offset(
        console, output.plain.index(explanation, section_start)
    )
    heading_style = output.get_style_at_offset(console, section_start)
    assert key_style.bold and not metavar_style.bold
    assert bool(metavar_style.dim) is (theme is PLAIN)
    assert label_style.bold and not prose_style.bold and not prose_style.dim
    assert heading_style.bold
    assert (label_style.color is not None) is (theme is UV)
    assert heading_style == output.get_style_at_offset(
        console, output.plain.index("Examples:")
    )


@pytest.mark.parametrize(
    ("topic", "fields"),
    [
        ("allow", _ALLOW_FIELDS),
        ("limit", _LIMIT_FIELDS),
        ("default", _DEFAULT_FIELDS),
        ("model", ("ref", "effort", "max_output")),
    ],
)
def test_topic_keys_keep_their_value_definitions_in_indented_blocks(topic, fields):
    _, output, _, _ = _invoke(["help", topic])
    definitions = list(re.finditer(r"^( *)([a-z_]+)=(\S+)\n", output.plain, re.M))
    assert tuple(match[2] for match in definitions) == fields
    for index, match in enumerate(definitions):
        end = (
            definitions[index + 1].start()
            if index + 1 < len(definitions)
            else output.plain.index("\nExamples:")
        )
        block = output.plain[match.end() : end]
        indentation = match[1] + "  "
        assert block.startswith(indentation)
        if "|" not in match[3]:
            assert "Possible values:" not in block
            continue
        values = block.split(f"\n{indentation}Possible values:\n", 1)[1]
        rows = values.split("\n\n", 1)[0].splitlines()
        assert all(line.startswith(indentation + "  ") for line in rows)
        assert tuple(line.split()[0] for line in rows) == tuple(match[3].split("|"))


def test_topic_content_preserves_value_rules_and_default_lifetimes():
    outputs = {
        topic: " ".join(_invoke(["help", topic])[1].plain.split()) for topic in TOPICS
    }
    assert "combine TQ queries by union" in outputs["allow"]
    assert "With all or none, that key may appear only once" in outputs["allow"]
    assert "Existing policy restrictions still apply" in outputs["allow"]
    assert "cost=DECIMAL|none" in outputs["limit"]
    assert "Non-negative decimal" in outputs["limit"]
    assert "USD per run tree" in outputs["limit"]
    assert "seconds per run tree" in outputs["limit"]
    assert "Each key may appear once" in outputs["limit"]
    assert "runtime instance" in outputs["default"]
    assert "current session" in outputs["default"]
    assert "do not reconfigure the runtime or other sessions" in outputs["default"]
    assert "Use --model for one run" in outputs["default"]
    assert "legacy [default] tables are ignored" in outputs["default"]
    assert "TOOLANG_DEFAULT_MODEL supplies runtime model defaults" in outputs["default"]
    assert (
        "TOOLANG_DEFAULT_RUNNABLE and runtime runnable defaults are unsupported"
        in outputs["default"]
    )
    assert "Select the entry ref; Chat only" in outputs["default"]
    assert "none Use automatic entry selection" in outputs["default"]
    assert "Omit the ref to change parameters on an effective model" in outputs["model"]
    assert "Non-negative token budget" in outputs["model"]
    assert "auto Clear the reasoning parameter" in outputs["model"]
    assert "Positive output token count" in outputs["model"]
    assert "auto Clear the output limit" in outputs["model"]
    assert "unset selects model-free execution" in outputs["model"]
    assert "--compact-model requires an exact ref" in outputs["model"]
    assert "none is not a model selection value" in outputs["model"]


@pytest.mark.parametrize("topic", TOPICS)
def test_topic_examples_keep_shell_argument_boundaries_at_44_columns(topic):
    _, output, _, _ = _invoke(["help", topic], width=44)
    examples = output.plain.split("Examples:\n", 1)[1].split("\nSee also:", 1)[0]
    commands = []
    for paragraph in examples.strip().split("\n\n"):
        lines = [
            line.strip() for line in paragraph.splitlines() if line.startswith("    ")
        ]
        command = "\n".join(lines).replace("\\\n", "")
        tokens = shlex.split(command)
        assert tokens[0] == "too"
        assert tokens[1] == "gus" and tokens[2] in {"chat", "start"}
        commands.append(tokens)
    assert len(commands) == 2
    for tokens in commands:
        for flag, resolver in (
            ("--allow", resolve_ceiling_overrides),
            ("--limit", resolve_limit_overrides),
        ):
            values = [
                tokens[i + 1] for i, token in enumerate(tokens[:-1]) if token == flag
            ]
            if values:
                resolver({}, values)
        if "--default" in tokens:
            resolve_default_overrides(
                {}, [tokens[tokens.index("--default") + 1]], session=True
            )
        if "--compact-model" in tokens:
            resolve_compact_override({}, tokens[tokens.index("--compact-model") + 1])


def test_explicit_agent_and_script_selectors_escape_the_help_command(tmp_path):
    status, output, error, _ = _invoke(["agent:help", "chat", "--help"])
    assert status == 0 and not error
    assert "Chat directly with an agent" in output.plain
    source = tmp_path / "demo.too"
    source.write_text("## Explain the program.\nagic help:\n  Help.\n")
    for arguments in (
        ["run", str(source), "help", "--help"],
        [str(source), "agic:help", "--help"],
        [str(source), "runnable:help", "--help"],
    ):
        status, output, error, _ = _invoke(arguments)
        assert status == 0 and not error
        assert "Explain the program" in output.plain


def test_topic_help_does_not_load_configuration_sources_or_execution(tmp_path):
    probe = """
import contextlib
import importlib.abc
from io import StringIO
from pathlib import Path
import socket
import sys

prefixes = (
    'fastapi', 'prompt_toolkit', 'toolang.up.core', 'toolang.up.source',
    'toolang.state.prepare', 'toolang.execution.executor', 'toolang.setup.config',
    'toolang.cli.toolang.commands.script', 'toolang.cli.toolang.commands.chat',
    'toolang.cli.toolang.commands.model_catalog',
)
class OfflineImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        assert not any(fullname == p or fullname.startswith(p + '.') for p in prefixes), fullname
sys.meta_path.insert(0, OfflineImports())

from toolang.cli.toolang.main import main
from toolang.cli.common import context
from toolang.cli import config
from toolang.catalog.agent import LocalAgents

def forbidden(*args, **kwargs):
    raise AssertionError('topic help must remain offline')
class UnreadableStdin(StringIO):
    read = readline = readlines = forbidden
sys.stdin = UnreadableStdin()
context.load_config = config.load_config = forbidden
LocalAgents.get = forbidden
socket.create_connection = forbidden
with contextlib.redirect_stdout(StringIO()):
    assert main(['--help']) == 0
assert 'toolang.cli.toolang.commands.help' not in sys.modules
for root in (Path.cwd(), Path.cwd() / 'missing'):
    for topic in ([], ['allow'], ['limit'], ['default'], ['model']):
        for tail in ([], ['-h'], ['--help']):
            with contextlib.redirect_stdout(StringIO()):
                assert main(['--root', str(root), 'help', *topic, *tail]) == 0
assert not (Path.cwd() / 'missing').exists()
assert not any(name == p or name.startswith(p + '.') for name in sys.modules for p in prefixes)
"""
    (tmp_path / "config.toml").write_text("invalid [ TOML")
    (tmp_path / "toolang.toml").write_text("invalid [ TOML")
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    assert not completed.stdout and not completed.stderr
    assert {path.name for path in tmp_path.iterdir()} == {"config.toml", "toolang.toml"}


@pytest.mark.parametrize("executable", ["too", "toolang"])
def test_installed_executable_displays_topic_help(executable, tmp_path):
    completed = subprocess.run(
        [Path(sys.executable).with_name(executable), "help", "model"],
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.startswith("Model selection\n")
    assert "Parameters:" in completed.stdout
    assert not completed.stderr and not tuple(tmp_path.iterdir())
