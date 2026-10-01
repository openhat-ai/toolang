"""Human record rendering preserves queryable values and bounded ref widths."""

import io
import json

import pytest
from rich.cells import cell_len
from rich.console import Console

from toolang.cli.common import output
from toolang.cli.common.records import echo_records


@pytest.mark.parametrize(
    "ref",
    [
        "fs/read",
        "vendor/" + "x" * 130,
        "provider/" + "模型" * 30,
        "p/" + "e\u0301" * 55,
    ],
)
def test_ref_column_wraps_without_truncating_or_widening_other_columns(
    monkeypatch, ref
):
    stream = io.StringIO()
    monkeypatch.setattr(
        output, "_TABLE_CONSOLE", Console(file=stream, width=4096, color_system=None)
    )
    echo_records([{"ref": ref, "tags": ["ready"]}], ("ref", "tags"), json_=False)
    lines = stream.getvalue().splitlines()
    header = next(line for line in lines if "REF" in line)
    tags_column = header.index("TAGS")
    assert tags_column - header.index("REF") - 2 <= 40
    rows = [
        line
        for line in lines
        if line.strip() and not set(line.strip()) <= {"─"} and line != header
    ]
    chunks = []
    for line in rows:
        chunk = line.strip().removesuffix("ready").rstrip()
        assert cell_len(chunk) <= 40
        chunks.append(chunk)
    assert "".join(chunks) == ref
    if len(ref) > 40:
        assert chunks[0].endswith("/")


def test_human_price_padding_and_long_location_are_preserved(monkeypatch, capsys):
    stream = io.StringIO()
    monkeypatch.setattr(
        output, "_TABLE_CONSOLE", Console(file=stream, width=4096, color_system=None)
    )
    record = {
        "ref": "provider/model",
        "max_output": 32768,
        "price": "  1.00 / 12.00",
        "location": "https://example.test/" + "path/" * 35,
    }
    echo_records([record], tuple(record), json_=False)
    rendered = stream.getvalue()
    assert "REF" in rendered and "MAX_OUTPUT" in rendered
    assert record["price"] in rendered
    assert record["location"] in rendered
    echo_records([record], tuple(record), json_=True)
    assert json.loads(capsys.readouterr().out) == [record]


@pytest.mark.parametrize("name", ["x" * 35, "模型" * 8, "x" * 75])
def test_model_wrapped_ref_aligns_continuations_and_details(monkeypatch, capsys, name):
    stream = io.StringIO()
    monkeypatch.setattr(
        output, "_TABLE_CONSOLE", Console(file=stream, width=4096, color_system=None)
    )
    records = [
        {
            "ref": "provider/" + name,
            "context": 2000000,
            "max_output": 32768,
            "price": "  1.00 / 12.00",
            "tags": ["ready", "remote"],
        },
        {
            "ref": "local/" + "tiny" * 8,
            "context": 512,
            "max_output": None,
            "price": "  0.00 /  0.00",
            "tags": ["ready", "local"],
        },
    ]
    columns = tuple(records[0])
    echo_records(records, columns, json_=False, align_ref_continuations=True)
    lines = stream.getvalue().splitlines()
    header = next(line for line in lines if "REF" in line)
    ref_start = header.index("REF")
    width = header.index("CONTEXT") - ref_start - 2
    rows = [
        line[ref_start:]
        for line in lines
        if line.strip() and not set(line.strip()) <= {"─"} and line != header
    ]
    assert rows[0].rstrip() == "provider/"
    assert "2,000,000" in rows[1]
    assert "32,768" in rows[1]
    assert records[0]["price"] in rows[1]
    assert "ready,remote" in rows[1]
    continuation = name[:40] if name.isascii() else name
    assert rows[1].startswith(" " * (width - cell_len(continuation)) + continuation)
    if len(name) > 40:
        remainder = name[40:]
        assert rows[2].rstrip() == " " * (width - cell_len(remainder)) + remainder
    assert records[1]["ref"] in rows[-1] and "512" in rows[-1]
    assert "ready,local" in rows[-1]

    echo_records(records, columns, json_=True, align_ref_continuations=True)
    assert json.loads(capsys.readouterr().out) == records
