"""Provider tables preserve selection facts and yield API space before ENV."""

from copy import deepcopy
from io import StringIO

import pytest
from rich.console import Console
from typer._click.utils import strip_ansi
from wcwidth import wcswidth

from toolang.base.types.model import ModelRoute, Provider, ProviderToolang
from toolang.cli.common import output
from toolang.cli.toolang.commands.model_catalog import _echo_providers


@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize(
    ("width", "api_visible", "env_complete"),
    [(80, False, True), (120, True, True), (240, True, True), (80, False, False)],
)
def test_api_shortens_then_disappears_before_environment_is_shortened(
    monkeypatch, terminal, width, api_visible, env_complete
):
    stream = StringIO()
    monkeypatch.setattr(
        output,
        "_TABLE_CONSOLE",
        Console(file=stream, width=4096, force_terminal=terminal, _environ={}),
    )
    env_name = "PROVIDER_API_KEY_FOR_THIS_DEVELOPMENT_ENVIRONMENT"
    if not env_complete:
        env_name += "_" + "LONG_" * 20
    provider = Provider(
        id="example",
        name="Example",
        env=(env_name,),
        _toolang=ProviderToolang(
            ready_count=12,
            model_count=100,
            route=ModelRoute(api="https://api.example.test/" + "path/" * 40),
        ),
    )

    _echo_providers([provider], width=width)

    lines = strip_ansi(stream.getvalue()).splitlines()
    assert len(lines) == 3
    assert lines[0].split() == [
        "PROVIDER",
        "MODELS",
        *(["API"] if api_visible else []),
        "ENV",
    ]
    assert lines[2].startswith("example") and "12/100" in lines[2]
    assert (env_name in lines[2]) is env_complete
    assert ("…" in lines[2]) is (api_visible or not env_complete)
    assert all(wcswidth(line) <= width for line in lines)
    assert set(lines[1]) == {"─"}


def test_provider_environment_uses_setup_conventions_even_without_credentials(capsys):
    provider = Provider(id="amazon-bedrock", name="Bedrock", env=("IGNORED_API_KEY",))

    _echo_providers([provider], width=240)

    rendered = capsys.readouterr().out
    assert "(AWS_BEARER_TOKEN_BEDROCK + AWS_REGION) OR " in rendered
    assert "(AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY + AWS_REGION)" in rendered
    assert "IGNORED_API_KEY" not in rendered


def test_provider_table_sanitizes_controls_and_preserves_unicode_and_literal_markup(
    capsys,
):
    identity = "供应商e\u0301"
    provider = Provider(
        id=identity,
        name="Example",
        _toolang=ProviderToolang(
            route=ModelRoute(api="https://example.test/[bold]\x1b]0;title\x07\n\t/path")
        ),
    )
    before = deepcopy(provider.to_data())

    _echo_providers([provider], width=80)

    rendered = capsys.readouterr().out
    lines = rendered.splitlines()
    assert len(lines) == 3 and all(wcswidth(line) <= 80 for line in lines)
    assert lines[2].startswith(identity)
    assert "[bold]" in rendered and "title" not in rendered and "\x1b" not in rendered
    assert provider.to_data() == before


def test_provider_identity_and_counts_do_not_shrink(capsys):
    identity = "long-provider-identity-" * 6
    provider = Provider(
        id=identity,
        name="Example",
        _toolang=ProviderToolang(ready_count=100000, model_count=100000),
    )

    _echo_providers([provider], width=80)

    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 3
    assert identity in lines[2] and "100000/100000" in lines[2]
