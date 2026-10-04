# Flow Evaluation

This document owns flow evaluation, binding, result shapes and iteration
contracts. The upstream [grammar reference](https://github.com/openhat-ai/tree-sitter-toolang/blob/main/GRAMMAR.md)
owns productions, legal clauses and CST fields. [Program semantics](program.md)
owns runnable signatures; [call input](call-input.md) owns Content evaluation.
The forms below are schematic notation, not complete executable programs.

## Flow And Binding

```text
flow [NAME] [(PARAMS)] [-> T]:
  STMTS

VALUE_STMT                    update `_`
let NAME = VALUE_STMT         update `NAME`
let VALUE_STMT                discard the result

repeat ...                    update locals through its body

let NAME = BODY         evaluate Content and assign one `Part[]` value to `NAME`
```

Flow signatures use the runnable parameter rules in
[program.md](./program.md), including implicit `_ : Part[]`, explicit `()`, and
named parameters.

Initial input and arguments share one flat local namespace: `_` holds input
and each parameter name holds its argument. See
[flat input mappings](./call-input.md#flat-input-mappings).

`_` is the primary local. A value statement reads a locals snapshot, computes
one result, and applies its binding only after the complete statement succeeds.
Except primary `_`, parameter/local names cannot start or end with `_`.
Runtime history names are supplied separately from authored bindings.
`repeat` is different: it produces no result and accepts no `let` binding. Its
body statements update the current flow locals normally as the loop proceeds.

A flow body containing only `pass` lowers to no statements and leaves its
initial locals unchanged. It still applies the declared output contract to the
final primary local; `pass` does not manufacture a missing output.


## Exec

`exec` replaces the current runnable and never returns on success, including
inside nested repeats. It binds the target's declared inputs from current locals
and resolves named targets from one latest published State. Inline agics keep
their containing code. The Run keeps its
identity, resource ceiling, accounting, and original output contract.

Named and inline targets use the same forms and input binding as `run`.
Exec has no result binding, argument list, or modifiers. Named targets must
exist in the accepted caller's definitions and keep compatible contracts.
Current and ancestor targets are rejected on each branch; earlier handoffs may
be called again. Failed validation leaves the binding unchanged and fails the
Flow normally.

## Statement behavior

| Operation | Runtime behavior |
| --- | --- |
| `run` / implicit prose | Invoke a named runnable or inline agic as a child Run. |
| `let NAME = BODY` | Evaluate Content into one `Part[]` local without a child Run. |
| `exec` | Replace the current runnable within the same Run. |
| `scatter` / `gather` | One child expands an item into a list / reduces a list into an item. |
| `storm` / `map` | Independent calls from one input / one call per list item; preserve result order. |
| `settle` | Sequential reduction with accumulator history. |
| `keep` / `drop` | Positional selection or one Boolean child per item. |
| `sort` | Score each item, then order stably in the required direction. |
| `repeat` | Execute a block while updating its working locals. |
| `ask` / `seek` | Parse and record steps, then fail because their human/agent bridges are not connected. |

Evidence for the bridge boundary: [ask](../src/toolang/execution/executor/stmts/ask.py)
and [seek](../src/toolang/execution/executor/stmts/seek.py).

The reshape statements form two execution families:

| Execution | `item -> list` | `list -> list` | `list -> item` |
| --- | --- | --- | --- |
| one child run | `scatter` | - | `gather` |
| multiple child runs | `storm` | `map` | `settle` |

`scatter/gather` delegate reshape to one runnable. `storm/map/settle` let the
executor coordinate multiple child runs. All five remain value statements and
bind their complete result once.


## Rules

### Blocks And Text

- A body's first substantive entry must indent deeper than its header.
  Structural siblings use the same indentation; a dedent closes the matching
  blocks. Empty required bodies are invalid, including comment-only loops.
- Blank lines and structural comments do not establish a body baseline.
  An implicit run may continue across one blank line; two blank lines or a
  structural comment end it.
- The first complete token of every implicit prose line is checked for
  lowercase keywords, including continuations. `sort these items` is invalid
  syntax; `Sort these items.` is prose. `sorter` is not the keyword `sort`.
- Explicit bodies such as `run:`, `map using:`, and `until:` preserve literal
  keywords, Markdown, and relative text indentation until the body dedents.
  Text margins use the same eight-column tab stops as parsing. Lowering removes
  the shared margin and represents relative indentation with spaces; formatting
  width changes only structural indentation. Interior blank lines are retained.
  Completed bodies do not require a final newline.
- `until` is optional when a repeat has a count. It must follow at least one
  executable statement, use the repeat body's sibling indentation, and be its
  final substantive entry. A repeat without a count requires `until`.


### Results

- Named runnable roles use the result contract declared by their agic or flow.
- Declaration output defaults to `Text`. Inline output defaults are `Text[]`
  for scatter, `Boolean` for keep/drop/until, `Number` for sort, and `Text`
  otherwise. Explicit signatures remain authoritative.
- Scatter requires an array output type, including an explicit annotation's
  complete array suffix. Map/storm preserve array-valued child results as nested
  arrays. Gather permits any output type; settle is constrained by its seed
  contract as described below.
- Inline agics capture their own free template references, excluding runtime
  variables. References inside sections also capture existing outer locals;
  section fields take precedence. Item-only fields do not require outer inputs.
  This also applies to `_` referenced only inside sections. Captures retain the
  current local's value type, including Boolean, structs, arrays, and Parts;
  recorded input types are reused when rendering the call. Captured values have
  already passed their producing boundary, so a struct returned by another
  module does not require a matching declaration in the caller. Named calls
  still validate arguments against their declared parameter contracts. Authored
  named parameters default to `Text`; `_` defaults to `Part[]`.
  Map/keep/drop/sort/gather/settle require `_` in the child's signature or inline
  body. Scatter/storm permit its omission.
- `ask` has a `Part[]` result contract but cannot return human input until its
  bridge is implemented.
- A direct `let NAME = BODY` evaluates its `Content` as one `Part[]` value local
  with language type `Part[]`, without starting a child run.
- Inline `keep`, `drop`, and `until` bodies default to `Boolean`; inline
  `sort` defaults to `Number`. An explicit incompatible return type is rejected.
- Named filters must declare `Boolean`; named scorers must declare `Number`.
- Generated inline `keep`, `drop`, `sort`, and `until` evaluators disable tools.
  They inherit recall for explicit history-variable references; child agics never
  prepend historical messages automatically.
- `repeat` is control flow, not a value statement. It has no result or binding.
  Its body statements update the same working locals according to their own
  bindings. Zero iterations leave locals unchanged.


### Runs

- `run RUNNABLE` follows the caller's [module visibility](program.md#program-modules)
  and binds compatible named targets from the latest published State. Inline
  `run` creates an inline agic from the accepted containing code.
- Bare `TEXT` is shorthand for inline `run` and starts the same child run.
- `scatter` and `gather` each start one child run, then reshape its result.
- Scatter has no count; its child must return an array, whose length determines
  the output length.
- `storm` starts `N` independent child runs and preserves result order.
- `map`, filter-based `keep/drop`, and `sort` start one child run per item.
- Settle accepts an optional trailing `from:` initializer, evaluated once in the
  outer frame before entering its own iteration scope. The initializer introduces
  no local. In an adhoc multiline body, only baseline `from:` ends reducer text;
  deeper occurrences remain literal. A named reducer's colon block contains only
  the `from:` clause. Settle retains one prior frame and has no window clause.
- Settle without an initializer uses the first element as the cumulative seed
  and invokes the reducer N-1 times. Each call receives the current element as
  `_` and the previous result as `_1._`; output must match the source element
  type. A singleton is validated and returned without a child call.
- The AST's optional `initial` Content is evaluated once in the outer scope,
  coerced to reducer output type, then used for N calls. It introduces no local.
- Empty map/keep/drop/sort produce typed empty lists without child calls.
  Gather/settle reject empty input before calling a child. Argument and output
  contracts still apply to empty collections.
- Positional `keep/drop` do not start child runs.


### Clauses

- `in P lanes` limits independent child work without changing result order.
  The fallback is the enclosing runnable's inherited lane setting, initially 4.
  A statement override does not change its children's default.
  It is supported by storm, map, predicate keep/drop, and sort. Use `in 1 lane`
  for numeric value 1, including `01`; other positive values require `lanes`.
- `keep/drop first|last N` select directly by current list position.
- `sort ascending` orders scores from lowest to highest; `sort descending`
  orders highest to lowest. Equal scores preserve input order in both directions.
  Empty input produces an empty list without scorer calls.
- Sorting and positional selection are separate statements. Sort commits the
  complete ordered list before a subsequent `keep first N` or `keep last N`
  selects from it. Selection starts no child runs, and retry reuses a committed
  sort result. Cancellation between them can leave the complete sorted list
  committed. Inspection shows a `par` Step followed by a `value` Step.
- Counts are non-negative integer literals. Use `repeat 1 time`, including
  numeric value `01`; other values require `times`.
- Repeat retains 3 prior frames by default. `windowing P` overrides that positive
  capacity independently of the iteration count.
- Every `repeat` has `N`, `until`, or both. When both are present, the first
  stopping condition reached ends the loop. `until` is always final, reads the
  latest locals after the iteration, and does not bind its Boolean result. A
  failed evaluator run or failed Boolean coercion fails the repeat and its
  enclosing flow; failure is never interpreted as `false`.
- `_k.name` reads the kth prior iteration's exit local; `_k._name` reads its
  entry local. `_k._` is the prior output and `_k.__` its input. Snapshots are
  immutable and exclude injected runtime bindings.
- History stays fixed throughout a repeat body and its until. Nested loops shadow
  the nearest scope and restore it on exit; ordinary calls preserve that scope.
  Missing in-window frames may be guarded; missing fields and out-of-window
  references are errors. Frame guards test presence, including empty/false/zero.
- Until waits for the highest history index in its own resolved templates,
  including inherited instruct/context and guards. Insufficient history means
  false with no rendering or child call; the completed round is still saved.
- Parameter/local names cannot start or end with `_`, except primary `_`.
  Data fields remain unrestricted. Thread variables are `_far`, `_near`, `_past`.

The common form is:

```text
verb -> count/direction -> lanes -> using/if/by -> runnable or inline body
```


## Complete example

The inline bodies explicitly consume the current value. This program can be
parsed offline; execution requires a model compatible with its result types.

```too
flow research(_, topic) -> Text:
  scatter using:
    Generate distinct research directions for {{_}} about {{topic}}.
  keep in 4 lanes if:
    Return true if {{_}} is specific and verifiable.
  sort descending in 3 lanes by:
    Score {{_}} by relevance to {{topic}}.
  keep first 3
  gather using:
    Synthesize {{_}} into one report.
  repeat 2 times:
    run: Improve the evidence and structure of {{_}}.
    until: Return true if another revision would not materially improve {{_}}.
```

## Implementation and verification

Lowering and validation: [lang](../src/toolang/lang/).
Evaluation: [flow runner](../src/toolang/execution/executor/runs/flow.py) and
[statement handlers](../src/toolang/execution/executor/stmts/).
Contracts: [flow scenarios](../tests/integration/execution/test_flow_scenarios.py),
[typed values](../tests/unit/execution/test_values.py),
[typed templates](../tests/unit/execution/test_execution_template.py).

Reserved or removed spellings are described in the upstream grammar, not as
implemented operations here. Historical migration guidance remains in Git
history and the [changelog](../CHANGELOG.md).
