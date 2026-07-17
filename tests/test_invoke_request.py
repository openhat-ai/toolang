from __future__ import annotations

from pathlib import Path

import click
import pytest

from toolang.cli.invoke.request import consume_control_options, parse_request
from toolang.lang.ast import AgicDecl, Parameter, Span


SPAN = Span(line=1)


def _agic(*, accepts_input: bool = True, params: tuple[Parameter, ...] = ()) -> AgicDecl:
    return AgicDecl(
        span=SPAN,
        name="demo",
        input=Parameter(span=SPAN, name="_", type_name="Text")
        if accepts_input
        else None,
        params=params,
    )


def test_consume_control_options_splits_models_and_honors_separator() -> None:
    quiet, verbosity, models, tools, caps, remaining = consume_control_options(
        ["--models", "openai,google", "-vv", "--", "-v-v", "message"]
    )

    assert quiet is False
    assert verbosity == 2
    assert models == ("openai", "google")
    assert tools == ()
    assert caps == ()
    assert remaining == ["--", "-v-v", "message"]


def test_parse_request_splits_and_deduplicates_model_selectors() -> None:
    request = parse_request(
        _agic(),
        ["--models=openai,google", "--models", "openai", "hello"],
        executable_kind="agic",
    )

    assert request.models == ("openai", "google")
    assert request.input_text == "hello"


def test_parse_request_coerces_typed_parameters(tmp_path: Path) -> None:
    target = tmp_path / "input.txt"
    params = (
        Parameter(span=SPAN, name="count", type_name="Number"),
        Parameter(span=SPAN, name="enabled", type_name="Boolean"),
        Parameter(span=SPAN, name="path", type_name="Path"),
    )

    request = parse_request(
        _agic(accepts_input=False, params=params),
        ["count=2.5", "enabled=yes", f"path={target}"],
        executable_kind="agic",
    )

    assert request.invoke_params == {
        "count": 2.5,
        "enabled": True,
        "path": str(target.resolve()),
    }


def test_parse_request_rejects_missing_required_parameter() -> None:
    parameter = Parameter(span=SPAN, name="count", type_name="Number")

    with pytest.raises(click.ClickException, match="count=..."):
        parse_request(
            _agic(accepts_input=False, params=(parameter,)),
            [],
            executable_kind="agic",
        )
