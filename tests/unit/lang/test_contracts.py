"""Output contracts retain semantic field types independently of model schemas."""

from dataclasses import replace

import pytest

from toolang.lang.ast import Field, Span, StructDecl
from toolang.lang.contracts import OutputContract
from toolang.lang.errors import ToolangValidationError


@pytest.mark.parametrize("field_type", ["Part", "Part[]"])
def test_output_contract_checks_nested_types_with_identical_json_schemas(field_type):
    leaf = StructDecl(
        name="Leaf",
        fields=(Field(name="value", type_name=field_type, span=Span(1)),),
        span=Span(1),
    )
    node = StructDecl(
        name="Node",
        fields=(
            Field(name="leaf", type_name="Leaf", span=Span(2)),
            Field(name="children", type_name="Node[]", span=Span(3)),
        ),
        span=Span(2),
    )
    structs = {"Leaf": leaf, "Node": node}
    contract = OutputContract.resolve("Node[]", structs=structs)
    structs["Leaf"] = replace(
        leaf,
        fields=(replace(leaf.fields[0], type_name=field_type.replace("Part", "Json")),),
    )
    with pytest.raises(ToolangValidationError, match="unchanged"):
        contract.validate("Node[]", structs=structs, name="worker")


def test_output_contract_ignores_field_order_and_source_locations():
    record = StructDecl(
        name="Record",
        fields=(
            Field(name="title", type_name="Text", span=Span(1)),
            Field(name="score", type_name="Number", optional=True, span=Span(2)),
        ),
        span=Span(1),
    )
    contract = OutputContract.resolve("Record", structs={"Record": record})
    moved = replace(
        record,
        span=Span(20),
        fields=tuple(
            replace(field, span=Span(21)) for field in reversed(record.fields)
        ),
    )
    contract.validate("Record", structs={"Record": moved}, name="worker")
    changed = replace(
        moved, fields=(replace(moved.fields[0], optional=False), *moved.fields[1:])
    )
    with pytest.raises(ToolangValidationError, match="unchanged"):
        contract.validate("Record", structs={"Record": changed}, name="worker")
