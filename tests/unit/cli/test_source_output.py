"""Source rendering preserves bytes and typed tree values."""

import html
import re

import pytest
from typer._click.utils import strip_ansi

from toolang.cli.toolang.source_output import (
    ColorMode,
    ast_sexp,
    color_enabled,
    render_source,
)
from toolang.lang.ast import CapDecl, Parameter, Span


@pytest.mark.parametrize(
    "source",
    [
        "",
        "# 🧭 中文\r\nagic echo(_):\r\n\tuser: [red] <>&  \r\n",
        "agic echo:\n  " + "x" * 1000,
        "# docs\nagic broken(_:):\n",
    ],
)
def test_source_rendering_retains_exact_text(source):
    assert render_source(source, color=False, html=False) == source
    assert strip_ansi(render_source(source, color=True, html=False)) == source
    exported = render_source(source, color=False, html=True)
    body = re.search(r"<code[^>]*>(.*?)</code>", exported, re.S)
    assert body is not None
    assert html.unescape(re.sub(r"<[^>]+>", "", body[1])) == source
    assert "\x1b[" not in exported
    assert not re.search(r'(?:src|href)=["\']https?://', exported)


@pytest.mark.parametrize(
    "mode,environ,terminal,expected",
    [
        (None, {}, False, False),
        (None, {}, True, True),
        (None, {"NO_COLOR": "1", "FORCE_COLOR": "1"}, True, False),
        (None, {"NO_COLOR": "", "FORCE_COLOR": "1"}, False, True),
        (None, {"FORCE_COLOR": "0"}, False, False),
        (None, {"TERM": "dumb"}, True, False),
        (ColorMode.always, {"NO_COLOR": "1", "TERM": "dumb"}, False, True),
        (ColorMode.never, {"FORCE_COLOR": "1"}, True, False),
    ],
)
def test_color_precedence(mode, environ, terminal, expected):
    assert color_enabled(mode, environ=environ, terminal=terminal) is expected


def test_dotted_styles_fall_back_and_unknown_captures_are_plain(monkeypatch):
    from toolang.lang import highlight

    monkeypatch.setattr(
        highlight,
        "captures",
        lambda source: [
            highlight.Capture(0, 3, "keyword.future"),
            highlight.Capture(3, 6, "unrecognized"),
        ],
    )
    rendered = render_source("abcdef", color=True, html=False)
    assert "\x1b[35mabc\x1b[0mdef" == rendered


def test_ast_sexp_keeps_all_fields_and_metadata_without_inventing_nodes():
    node = CapDecl(
        span=Span(2),
        kind="skill",
        name="example",
        body='Quotes "(🧭)"\n',
        params=(Parameter(span=Span(2), name="path", optional=True),),
        meta={"kind": "not-an-ast", "values": [True, None, 1.5], "empty": {}},
    )
    result = ast_sexp(node)
    assert result.startswith("(skill\n  span: (span line: 2)")
    assert 'body: "Quotes \\"(🧭)\\"\\n"' in result
    assert '("kind" "not-an-ast")' in result
    assert "optional: true" in result
    assert '("empty" (map))' in result
    assert "doc: null" in result
    assert result.endswith(")\n")
