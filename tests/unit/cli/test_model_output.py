"""Human model lists drop optional columns before losing required facts."""

from copy import deepcopy
from io import StringIO

import pytest
from rich.console import Console
from typer._click.utils import strip_ansi
from wcwidth import wcswidth

from toolang.cli.common import output
from toolang.cli.toolang.commands import model_catalog


def _record(**changes):
    return {
        "ref": "vendor/" + "m" * 33,
        "context": 1_000_000,
        "price": "  1.00 /  2.00",
        "tags": ["ready", "remote"],
        "input": ["text", "image"],
        "features": ["reasoning", "tool_call"],
        "max_output": 32768,
        "output": ["text"],
        **changes,
    }


@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize(
    ("width", "columns"),
    [
        (126, "MODEL CONTEXT MAX_OUTPUT PRICE ($/1M) INPUT OUTPUT FEATURES STATUS"),
        (125, "MODEL CONTEXT MAX_OUTPUT PRICE ($/1M) INPUT FEATURES STATUS"),
        (117, "MODEL CONTEXT PRICE ($/1M) INPUT FEATURES STATUS"),
        (105, "MODEL CONTEXT PRICE ($/1M) INPUT STATUS"),
        (84, "MODEL CONTEXT PRICE ($/1M) STATUS"),
    ],
)
def test_display_order_is_independent_of_removal_priority(
    monkeypatch, terminal, width, columns
):
    stream = StringIO()
    monkeypatch.setattr(
        output,
        "_TABLE_CONSOLE",
        Console(file=stream, width=4096, force_terminal=terminal, _environ={}),
    )
    records = [_record()]
    before = deepcopy(records)

    model_catalog._echo_models(records, width=width)

    lines = strip_ansi(stream.getvalue()).splitlines()
    assert len(lines) == 3
    assert lines[0].split() == columns.split()
    assert lines[0].startswith("MODEL") and lines[2].startswith(records[0]["ref"])
    assert set(lines[1]) == {"─"}
    assert all(wcswidth(line) <= width for line in lines)
    assert "1,000,000" in lines[2] and "ready" in lines[2]
    assert "remote" not in lines[2] and "…" not in lines[2]
    assert records == before


@pytest.mark.parametrize(
    "identity",
    ["vendor/" + "x" * 5000, "供应商/" + "模型e\u0301" * 40],
    ids=["ascii", "unicode"],
)
def test_required_columns_can_exceed_budget_without_losing_identity(capsys, identity):
    model_catalog._echo_models([_record(ref=identity)], width=80)

    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 3
    assert lines[0].split() == ["MODEL", "CONTEXT", "PRICE", "($/1M)", "STATUS"]
    assert lines[2].startswith(identity)
    assert "1,000,000" in lines[2] and "1.00/2.00" in lines[2] and "ready" in lines[2]
    assert wcswidth(lines[1]) > 80
    assert "…" not in lines[2]


def test_status_only_contains_readable_availability_reasons(capsys):
    model_catalog._echo_models(
        [
            _record(
                tags=[
                    "not_allowed",
                    "no_env",
                    "no_api",
                    "no_adapter",
                    "local",
                    "authored",
                ]
            )
        ],
        width=240,
    )

    row = capsys.readouterr().out.splitlines()[2]
    assert "not allowed,no env,no api,no adapter" in row
    assert "local" not in row and "authored" not in row


def test_prices_and_numeric_limits_remain_aligned_and_preserve_zero(capsys):
    records = [
        _record(ref="v/one", context=0, max_output=0, price="  1.00 / 20.00"),
        _record(ref="v/two", price="99999.00 /  0.00"),
        _record(
            ref="v/three", context=None, max_output=None, price="-", input=[], output=[]
        ),
    ]

    model_catalog._echo_models(records, width=240)

    lines = capsys.readouterr().out.splitlines()
    assert lines[2].rfind("/") == lines[3].rfind("/")
    assert "99999.00/ 0.00" in lines[3]
    assert "0" in lines[2] and "None" not in "\n".join(lines)
    for header, expected in (
        ("CONTEXT", ["0", "1,000,000"]),
        ("MAX_OUTPUT", ["0", "32,768"]),
    ):
        end = lines[0].index(header) + len(header)
        for line, value in zip(lines[2:], expected):
            assert line[end - len(value) : end] == value


def test_model_cells_preserve_literal_text_and_strip_terminal_instructions(capsys):
    record = _record(
        ref="vendor/[bold]模型👨‍👩‍👧‍👦\x1b]0;bad-title\x07",
        features=["reasoning\n\ttool_call"],
    )

    model_catalog._echo_models([record], width=240)

    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 3
    assert "vendor/[bold]模型👨‍👩‍👧‍👦" in lines[2]
    assert "bad-title" not in lines[2] and "\x1b" not in lines[2]
    assert "reasoning  tool_call" in lines[2]
