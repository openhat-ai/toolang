# Semantic AST

Toolang's `lang` package converts the installed grammar's concrete syntax tree
(CST) into an immutable semantic `Program`. This guide is for language and State
maintainers. [Program](program.md) owns declaration meaning; [Flow](flow.md)
owns evaluation; [Language commands](language-commands.md) owns CLI inspection.

## Source to Program

```text
UTF-8 source -> tree-sitter CST -> semantic lowering -> static validation -> Program
```

`Program.from_source()` performs this path. The CST retains grammar nodes,
comments, recovery regions, and byte positions. The semantic AST retains useful
source lines and attached documentation, declarations, signatures, directives,
messages, and flow operations. It is not a lossless formatting tree.
`format_source()` works from syntax-valid source rather than serializing this AST.

[ast.py](../src/toolang/lang/ast.py) owns the semantic vocabulary and codecs,
[lower.py](../src/toolang/lang/lower.py) consumes CST fields, and
[validate.py](../src/toolang/lang/validate.py) checks declarations and contracts.
Syntax diagnostics and semantic validation remain distinct; `parse --cst`
exposes native recovery, while ordinary `parse` constructs a validated Program.

## Nodes and collections

Semantic `Node` values are frozen dataclasses with a `Span(line)` and optional
`doc`. A Program groups immutable tuples of external `with` declarations,
inline caps, chores, structs, contexts, instructs, agics, and flows. Parameters
and fields carry their own type, optionality, and documentation. Program does
not contain installed plugins, mutable locals, stores, watchers, or runtime events.

An `AgicDecl` holds its signature, directives, selected templates, and authored
messages. A `FlowDecl` holds its signature, directives, and ordered statements.
Statements represent calls, async calls/await, spawn, exec, collection operations,
repeat, and value bindings. `RepeatStmt.until_index` preserves the condition's
position among ordinary body statements. Runtime iteration and scheduling state
are not embedded in these nodes.

Lowering converts implicit prose and inline runnable bodies into unnamed agic
nodes and replaces the statement target with `agic:<adhoc:LINE>`. These helpers
remain private to their containing plan. Missing authored names stay `None`;
State derives lined entry lookups and filename-based exports without rewriting
AST names. [State](state.md#programs-and-module-terms) owns those indexes.

Documentation attachment follows physical adjacency and scope, including
parameter tags. Literal text keeps comment-looking content as text. Signature
omission, context-dependent inline output contracts, and template captures are
normalized or validated by language-owned helpers; no runtime resource lookup
occurs during lowering.

## Validation boundaries

Static validation checks duplicate names, type declarations, signatures,
directives, documentation tags, supported statement forms, and statically known
call contracts. [flow_validation.py](../src/toolang/lang/flow_validation.py)
tracks available local/type facts through flow statements. A main-module check
can receive explicit `external_runnables`; extensions cannot acquire another
module's private namespace. `parse_cap_declarations()` and
`parse_chore_declarations()` serve their narrower authored-source consumers.

Runtime admission still validates actual inputs, selected model parameters,
resource authority, current target existence, and compatibility with a newly
published named target. Parsing cannot prove provider readiness, workspace
availability, or dynamically selected values. [contracts.py](../src/toolang/lang/contracts.py)
provides reusable runnable/output contract rules without executing a Run.

## Serialization

`to_data()` emits semantic JSON with kind discriminators and source/documentation
facts. `program_from_data()` restores typed nodes and immutable collections from
that representation. Restoration is distinct from parsing source again. State
stores parsed Program values in versioned layers; execution records separately
store statement specs with their own store version. Neither representation is
a provider request or a public event payload.

This complete Python example parses and round-trips a model-free Program:

```python
from toolang.lang import Program, program_from_data, to_data

program = Program.from_source("flow identity:\n  pass\n")
data = to_data(program)
restored = program_from_data(data)
assert restored == program
assert restored.flows[0].name == "identity"
```

## Implementation evidence

- [AST and codec tests](../tests/unit/lang/test_program.py), [static validation](../tests/unit/lang/test_static_validation.py)
- [Async syntax](../tests/unit/lang/test_async_await.py), [spawn](../tests/unit/lang/test_spawn.py), [repeat positions](../tests/unit/lang/test_repeat_conditions.py)
- [State layer format](../src/toolang/state/format.py), [State layer tests](../tests/unit/state/test_layer_format.py)
