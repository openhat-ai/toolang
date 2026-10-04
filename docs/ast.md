# Semantic AST

Toolang's abstract syntax tree (AST) is the immutable `Program` consumed by
semantic validation, State preparation and execution. This guide owns its Python
representation, lowering and serialization. Source productions and the concrete
syntax tree (CST) belong to the
[grammar repository](https://github.com/openhat-ai/tree-sitter-toolang/blob/main/GRAMMAR.md).
[Program semantics](program.md), [Agic execution](agic.md) and
[Flow evaluation](flow.md) own the meaning of the represented constructs.

## Construction boundary

```text
source -> grammar CST -> lowering -> semantic validation -> Program
                                                          |
                                                  State module binding
```

`Program.from_source(source, external_flows=...)` is the source entry point.
It rejects syntax errors, lowers a complete tree, and validates the resulting
Program before returning it. Parsing is offline: it does not load Setup, resolve
remote caps, choose a model or start execution. State supplies `external_flows`
when validating calls to public home flows; this does not import their structs,
templates or private declarations into the source module.

The semantic path adds a missing final newline and masks query-data hashes for
tree-sitter while preserving byte offsets; lowering reads the original text.
The raw `too parse --cst` path parses original bytes without those adaptations.
[Source commands](source-commands.md#parse) owns CLI modes, output and diagnostics.

## Nodes and collections

Nodes are frozen, slotted dataclasses with a `kind`, a one-based `span.line` and
optional attached `doc`. A span is a semantic line anchor, not a full token
range or a source filename. Sequences are tuples; cap/job metadata is frozen.

`Program` groups declarations into `withs`, `caps`, `jobs`, `structs`, `contexts`,
`instructs`, `agics` and `flows`. Each collection preserves source-line order;
there is no single interleaved top-level declaration list. Nested messages and
flow statements retain their evaluation order.

| Node family | Representation |
| --- | --- |
| `WithDecl` | Cap kind and source reference, without resolved cap content. |
| `CapDecl`, `JobDecl` | Kind, name, body and metadata; prompt caps also carry inferred parameters. |
| `StructDecl`, `Field` | Named structural types and ordered field declarations. |
| `ContextDecl`, `InstructDecl` | Named template bodies. |
| `Parameter` | Name, optionality, type name and attached parameter documentation. |
| `AgicDecl` | Optional name, input, named parameters, output, directives, context/instruct selections and authored messages. |
| `FlowDecl` | The shared runnable fields plus statements and `name_explicit`. |
| `Message` | Authored role/content and whether the role was explicit in source. This is distinct from a model-call `Message` containing Parts. |
| `Directive` | Name, operator and values; resource query expressions remain intact. |
| `FlowStmt` | A union discriminated by `kind`, with operation-specific fields and binding where applicable. |

The statement union contains run/exec/ask/seek, scatter/storm/gather/settle/map,
keep/drop/sort, repeat and direct-content let nodes. A `RepeatStmt` owns nested
`stmts` and an optional evaluator reference. A `SettleStmt` stores optional
initializer Content in `initial`. There is no `pass` node: lowering omits it.
Exact fields are in [ast.py](../src/toolang/lang/ast.py); behavior belongs in
[Flow evaluation](flow.md#statement-behavior).

## Lowering decisions

Lowering retains semantic facts rather than every source spelling:

- Runnable signature and output defaults become explicit AST fields. Omitted
  parentheses produce the default primary parameter; `()` produces no input
  and no named parameters. See [signature rules](program.md#runnable-signatures).
- Omitted declaration names remain `None`; State assigns entry/export lookup
  identities without renaming AST nodes. `FlowDecl.name_explicit` also preserves
  the authored distinction.
- Inline flow bodies become unnamed `AgicDecl` entries in `Program.agics`.
  Statements refer to them as `agic:<adhoc:LINE>`. `Program.adhoc_lines` identifies
  those inline declarations; `find_agic()` resolves their internal references.
  Do not treat every entry in `agics` as a public named runnable.
- Free template references determine inline capture parameters. Runtime binds
  them to captured local types; inferred AST parameter types alone are not the
  final types of captured values. Generated filter/scorer/until evaluators also
  receive the tool-disabling directive.
- `context` and `instruct` selections become dedicated runnable fields; other
  directives remain ordered `Directive` entries. Inherited resource selection
  is resolved later, not materialized into the AST.
- Named `let` around an operation changes that operation's `binding`; it does
  not wrap it in another statement. `let NAME = BODY` instead becomes `LetStmt`.
  `binding="_"` replaces primary input, a name saves the result, and `None`
  discards it. Exec and repeat have no result binding.
- Documentation attaches to semantic nodes; ordinary comments, punctuation and
  exact whitespace do not survive as a source-preserving tree. The formatter
  operates on syntax, not by printing the semantic AST back to source.

## Validation and runtime boundaries

Source validation checks declarations, names, type references, cap fields,
signatures, directive forms, template inputs and known operation contracts.
Flow validation follows local bindings, collection shape and known iteration
history requirements. Unknown dynamic values and actual value coercion still
require runtime checks. A valid AST does not prove that models, tools, caps or
human/agent bridges are available.

`ToolangSyntaxError` and `ToolangValidationError` share `ToolangSourceError` and
carry structured source diagnostics. A failed source parse returns no partial
Program; raw CST inspection remains available for incomplete source.
Constructing AST dataclasses directly does not run the complete semantic
validation pipeline.

## Serialization and restoration

`to_data()` emits JSON-compatible objects with `kind` and dataclass fields.
Tuples become arrays, spans become `{"line": N}`, and optional fields remain
explicit. `FlowStmt.kind` selects the exact statement class during decoding,
including nodes with otherwise similar field shapes.

`program_from_data()` restores a **previously validated** Program through typed
decoding. It does not parse source or rerun semantic validation. State uses it to
load recorded Programs; use `Program.from_source()` for new authored source.
The AST payload has no independent schema-version envelope. State and execution
storage owners govern compatibility for their containing snapshots and records;
see [State](state.md) and [records](records.md#persistence).

This complete example parses and round-trips offline without executing the flow:

```python
from toolang.lang import Program, program_from_data, to_data

source = "flow echo(_) -> Part[]:\n  pass\n"
program = Program.from_source(source)
flow = program.flows[0]
assert flow.input is not None and flow.input.type_name == "Part[]"
assert flow.stmts == ()
assert program_from_data(to_data(program)) == program
```

## Consumers and verification

State binds Programs to module names, source identities and public/private
runnable indexes. Execution consumes accepted declarations and statements rather
than parsing source again. CLI help and flow descriptions also read the AST;
source tooling exposes it through `too parse --ast --json`.

When changing a node or its lowering, check source validation, serialized
round trips, State compatibility and affected execution/help consumers. A CST
change starts in the grammar repository; an AST-only change need not change the
CST or its release.

[AST types/codecs](../src/toolang/lang/ast.py),
[lowering](../src/toolang/lang/lower.py),
[validation](../src/toolang/lang/validate.py) and
[flow validation](../src/toolang/lang/flow_validation.py) own the implementation.
[Program tests](../tests/unit/lang/test_program.py),
[documentation attachment](../tests/unit/lang/test_doc_comments.py) and
[static validation](../tests/unit/lang/test_static_validation.py) cover lowering,
codec round trips and rejection boundaries. [State cache tests](../tests/unit/state/)
cover restoration and module publication.
