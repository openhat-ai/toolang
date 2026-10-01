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
