"""Runnable compatibility includes data shapes, not implementation details."""

from dataclasses import replace

import pytest

from toolang.lang.ast import Program
from toolang.lang.contracts import RunnableContract


SOURCE = """
struct Detail:
  value: Text
  count?: Number
struct Item:
  detail: Detail
struct Unused:
  note: Text
agic worker(_: Item, option?: Text, limit?: Number) -> Item:
  Work.
"""


def contract(source):
    program = Program.from_source(source)
    return RunnableContract.resolve(
        program.agics[0], structs={s.name: s for s in program.structs}
    )


@pytest.mark.parametrize(
    "before,after",
    [
        ("value: Text", "value: Number"),
        ("count?: Number", "count: Number"),
        ("option?: Text", "option: Text"),
        ("option?: Text", "option?: Number"),
        ("_: Item", "_: Item[]"),
        ("-> Item", "-> Text"),
    ],
)
def test_signature_includes_all_inputs_and_transitive_structs(before, after):
    assert contract(SOURCE) != contract(SOURCE.replace(before, after))


def test_signature_ignores_docs_locations_body_and_field_order():
    updated = SOURCE.replace(
        "  value: Text\n  count?: Number", "  count?: Number\n  value: Text"
    )
    updated = updated.replace(
        "option?: Text, limit?: Number", "limit?: Number, option?: Text"
    )
    updated = updated.replace("note: Text", "note: Number").replace(
        "Work.", "Do different work."
    )
    assert contract(SOURCE) == contract("\n## Updated documentation.\n" + updated)


def test_source_defaults_match_explicit_signatures():
    assert contract("agic worker:\n  Work.\n") == contract(
        "agic worker(_: Part[]) -> Text:\n  Work.\n"
    )
    program = Program.from_source(SOURCE)
    worker = program.agics[0]
    assert RunnableContract.resolve(worker, structs={}) != RunnableContract.resolve(
        replace(worker, output="Number"), structs={}
    )
