# Flow Statement Syntax

This document defines the flow surface syntax in one place. It covers authored
statements and their observable semantics; executor, trace, and lowering
details remain in their owning documents.


## Terminology

A **Flow statement** is an authored instruction such as `run`, `generate`,
`map`, `reduce`, `keep`, `drop`, `sort`, `let`, `repeat`, or `exec`.
Use **array operation** when discussing the semantics of
`generate/map/reduce/keep/drop/sort`; this category does not imply an array input.
Generate produces an array from repeated calls. The other five consume an array.

Use **operator** for symbolic syntax such as `=`, `+=`, and `-=`, rather than
as another name for a statement. `stmt` and `FlowStmt` are implementation names
used in code and AST documentation. A **Step** is a recorded execution unit;
a **runnable** is an agic or flow invoked by a statement. These terms are not
synonyms for a Flow statement.


## Notation

```text
NAME       local name
T          Toolang type
N          non-negative count or selection size
P          positive concurrency limit
VALUE_STMT a result-producing statement (excludes repeat and exec)
RUNNABLE   named agic or flow
AGENT      agent selector
MAPPER     per-input runnable returning one complete result
REDUCER    per-item runnable updating an accumulator
FILTER     per-item runnable returning Boolean
SCORER     per-item runnable returning Number
LINE       text on the same line as `:`
TEXT       indented text block
BODY       LINE or an indented TEXT block
STMTS      indented flow statements
```

Uppercase words are placeholders, not keywords. `[X]` marks optional syntax,
and `A | B` marks alternatives. Counts and sort direction immediately follow
the verb. Lane placement follows each statement's form below; an inline
runnable always comes last. Generate/map require lanes before their target.

`BODY` never includes its introducing colon:

```too
KEYWORD ...: LINE

KEYWORD ...:
  TEXT
```

When a statement consumes authored content, its `BODY` is `Content` and
follows [input-syntax.md](./input-syntax.md). A statement block uses `STMTS`
instead.


## Flow And Binding

```text
flow [NAME] [(PARAMS)] [-> T]:
  STMTS

VALUE_STMT                    update `_`
let NAME = VALUE_STMT         update `NAME`
let VALUE_STMT                discard the result

spawn RUNNABLE                start an independent root; preserve `_`
let NAME = spawn RUNNABLE     bind its runtime handle to `NAME`

repeat ...                    update locals through its body

let NAME = BODY         evaluate Content and assign one `Percept` to `NAME`
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


## Spawn

`spawn RUNNABLE` and `spawn [-> T]: BODY` use the same inputs and inline captures
as `run`, then continue as soon as a new root is admitted. Each root uses a new
empty thread under the same agent and executor. `let job = spawn research` binds
a handle; bare spawn and `let spawn research` preserve all locals, including `_`.
The formatter writes nameless-let spawn as bare spawn.

Read metadata through templates: `{{job.id}}` is the run ID, `{{job.thread}}` is
the thread ID, and `{{job.status}}` reads persisted lifecycle status without
waiting. A whole-handle template renders those three fields. One statement sees
one status snapshot per run; later statements may see a newer status. Unknown
fields fail. Capture these fields as ordinary data before passing them to named
runnables. Handles cannot be runnable results or general data arguments.

`Run<T>` is runtime design notation, not a language type or constructor. An
authored struct named `Run` remains ordinary data. Serialized outputs use the
runtime tag `_Run<T>` for a known result type, otherwise `_Run`, inside the same
`type/value/binding` envelope as ordinary outputs. User struct names cannot begin
with `_`. Neither `async` nor `await` is implemented in this release.

The root survives its source finishing, failing, being canceled, or executing a
handoff. Executor shutdown cancels it: script invocations stop their executor on
exit, while local Chat and AgentCore keep theirs for the session/host lifetime.
There is no automatic restart or completion message. Use its ID with existing
inspection and host control commands. See [execution records](run-step-records.md)
for durable handles and retry behavior.

## Exec

`exec` replaces the current runnable and never returns on success, including
inside nested repeats. It binds the target's declared inputs from current locals
and resolves named targets from one latest published State. Inline agics keep
their containing code. The Run keeps its
identity, resource ceiling, accounting, and original output contract.

Named and inline targets use the same forms and input binding as `run`.
Exec has no result binding, argument list, or modifiers. Named targets must
exist in the accepted caller's definitions and keep compatible contracts.
A root Run with no active descendants may exec its own entry runnable when the
latest published implementation has the same normalized contract. Child
self-exec and other active ancestor targets remain rejected; earlier inactive
handoffs may be called again. Failed validation leaves the binding unchanged
and fails the Flow normally.

## Statements

```text
# Call once with the complete input
run RUNNABLE
run [-> T]: BODY
TEXT                                      shorthand for inline `run`
seek AGENT RUNNABLE
seek AGENT [-> T]: BODY
ask: BODY

# Start an independent root without waiting
spawn RUNNABLE
spawn [-> T]: BODY

# Replace the current runnable within the same Run
exec RUNNABLE
exec [-> T]: BODY

# Generate an outer array of complete results
generate N [in P lanes] using RUNNABLE
generate N [in P lanes] [-> T]: BODY

# Reduce the outer array sequentially
reduce using REDUCER
reduce [-> T]: BODY

reduce using REDUCER:
  from: BODY

reduce [-> T]:
  TEXT
  [from: BODY]

# Transform every outer array item
map [in P lanes] using MAPPER
map [in P lanes] [-> T]: BODY

# Select or sort outer array items
keep first N
keep last N
keep [in P lanes] if FILTER
keep [in P lanes] if [-> Boolean]: BODY
drop first N
drop last N
drop [in P lanes] if FILTER
drop [in P lanes] if [-> Boolean]: BODY
sort ascending|descending [in P lanes] by SCORER
sort ascending|descending [in P lanes] by [-> Number]: BODY

# Repeat statements
repeat N times [windowing P]:
  STMTS
  [until: BODY]

repeat [windowing P]:
  STMTS
  until: BODY
```


## Natural Reading

```text
run      run a named agic or flow, or an inline agic
exec     replace the current runnable with a named or inline runnable; never return
seek     seek another agent's help with a named runnable or inline request
ask      ask the human owner for input, judgment, or confirmation
generate generate N independent results from the current value
reduce   reduce outer items to one value through sequential runs
map      map each current item to a new item while preserving order
keep     keep positional items or items accepted by a filter
drop     drop positional items or items accepted by a filter
sort     sort all items by score in the required ascending or descending order
repeat   repeat a statement block, bounded by N or until
```

All value statements bind their complete result once. `run` calls once with the
whole input. `generate N` returns N complete results in an outer array; `map`
returns one complete result per outer input item. Neither flattens array results.
`reduce` combines the outer items sequentially.


## Rules

### Blocks And Text

- A body's first substantive entry must indent deeper than its header.
  Structural siblings use the same indentation; a dedent closes the matching
  blocks. Empty required bodies are invalid, including comment-only loops.
- Use two spaces when authoring. Other widths and tabs are supported, but a
  structural indentation prefix cannot mix tabs and spaces or change spelling
  at an existing level. Tabs use eight-column stops for parsing.
- Blank lines and structural comments do not establish a body baseline.
  An implicit run may continue across one blank line; two blank lines or a
  structural comment end it.
- The first complete token of every implicit prose line is checked for
  lowercase keywords, including continuations. `sort these items` is invalid
  syntax; `Sort these items.` is prose. `sorter` is not the keyword `sort`.
- Explicit bodies such as `run:`, `map:`, and `until:` preserve literal
  keywords, Markdown, and relative text indentation until the body dedents.
  Text margins use the same eight-column tab stops as parsing. Lowering removes
  the shared margin and represents relative indentation with spaces; formatting
  width changes only structural indentation. Interior blank lines are retained.
  Completed bodies do not require a final newline.
- `until` is optional when a repeat has a count. It must follow at least one
  executable statement, use the repeat body's sibling indentation, and be its
  final substantive entry. A repeat without a count requires `until`.


### Results

- Map/reduce/keep/drop/sort consume the actual outermost array. `Text[][]`
  supplies `Text[]` items; open `Json` arrays supply `Json` items; `Part[]`
  supplies `Part` items.
  Parameters, run results, helper Flows, exec, and restored values follow the
  same rule. Scalars, objects, null, and absent values are rejected; operations
  do not parse JSON strings or traverse object fields.
- There is no separate shape/dim. Generate/map with result type `U` produce
  `U[]`, including typed empty arrays with zero calls. Selection and sorting
  retain the input's complete type and element provenance.

- Named runnable roles use the result contract declared by their agic or flow.
- Declaration output defaults to `Text`. Inline output defaults to `Boolean`
  for keep/drop/until, `Number` for sort, and `Text` otherwise. An inline run
  returning an array needs an explicit array type.
- Map/generate preserve array-valued child results as nested arrays. Run and
  reduce may return any value type.
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
  Map/keep/drop/sort/reduce require `_` in the child's signature or inline
  body. Run/generate permit its omission.
- `ask` evaluates its `Content` for the human owner and returns the owner's
  canonical `Percept`, represented in the language as `Part[]`.
- A direct `let NAME = BODY` evaluates its `Content` as one `Percept` local
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


### Array Input And Empty Results

| Statement | Primary input | Empty input / zero count | Child calls for N items |
| --- | --- | --- | --- |
| `generate K` | Whatever the runnable signature accepts; may be absent | `K=0` produces an empty `U[]`; an array input is passed whole to every call | K |
| `map` | An outer array | Returns empty `U[]` | N |
| `keep` / `drop` with a predicate | An outer array | Returns an empty value with the source type | N |
| Positional `keep` / `drop` | An outer array | Returns an empty value with the source type | 0 |
| `sort` | An outer array | Returns an empty value with the source type | N |
| `reduce` without an initializer | A nonempty outer array | Rejects empty input; one item is returned as the seed | N - 1 |
| `reduce` with an initializer | An outer array | Returns the initializer coerced to the reducer output type | N |

The initializer is stored as `initial` in the AST. The currently published
grammar still spells the source clause `from:`; the approved rename to
`initial:` will be introduced with a separate grammar update.

Here `U` is the child output type. Zero-call generation, mapping, predicate
selection, sorting, and initialized reduction still validate the target, its
output contract, and required named arguments; they make no model calls.
Array consumers skip per-element conversion when there are no elements.
Generate validates its complete call inputs even when its count is zero.

For map/reduce/keep/drop/sort, known non-array or missing inputs are rejected
during source checking. Open `Json` or unknown inputs are checked inside the
executing Step before child calls. JSON strings, objects, null, and absent input are not arrays. For nested
arrays only the outermost level is selected, and array-valued child results
stay nested. Predicate selection and sorting preserve the original items;
sorting is stable for equal scores.

Generation and positional selection counts are non-negative integers; lane
limits are positive integers. `keep first/last 0` returns an empty array;
`drop first/last 0` retains the input. Selection counts beyond the input length
are clipped: keep retains everything and drop removes everything.


### Runs

- `run RUNNABLE` resolves in the current program. Inline `run` creates an
  inline agic.
- Bare `TEXT` is shorthand for inline `run` and starts the same child run.
- `seek AGENT RUNNABLE` resolves in the target agent's program. Inline `seek`
  sends its body to the target agent.
- `run` calls once, including for empty or nested arrays, and retains the
  complete output type.
- `generate` starts `N` independent child runs and preserves result order.
- `map`, filter-based `keep/drop`, and `sort` start one child run per item.
- Reduce accepts an optional trailing `from:` initializer, evaluated once in the
  outer frame before entering its own iteration scope. The initializer introduces
  no local. In an adhoc multiline body, only baseline `from:` ends reducer text;
  deeper occurrences remain literal. A named reducer's colon block contains only
  the `from:` clause. Reduce retains one prior frame and has no window clause.
- Reduce without an initializer uses the first element as the cumulative seed
  and invokes the reducer N-1 times. Each call receives the current element as
  `_` and the previous result as `_1._`; output must match the source element
  type. A singleton is validated and returned without a child call.
- The AST's optional `initial` Content is evaluated once in the outer scope,
  coerced to reducer output type, then used for N calls. For an empty array,
  that coerced value is the result. Invalid initializers still fail. It introduces
  no local.
- Empty map/keep/drop/sort produce typed empty lists without child calls.
  Reduce rejects empty input only when its initializer is absent. Argument and output
  contracts still apply to empty arrays; unused reducer history is not evaluated.
- Positional `keep/drop` do not start child runs.


### Clauses

- Generate/map/reduce require `using` for named targets and must omit it for
  inline bodies. The rule also applies to named and discarded `let` results.
  Generate/map lane clauses precede the target: `map in 2 lanes using worker`.
  Run/exec/seek use direct targets; keep/drop retain `if`, sort retains `by`.

- `in P lanes` limits independent child work without changing result order.
  The fallback is the enclosing runnable's inherited lane setting, initially 4.
  A statement override does not change its children's default.
  It is supported by generate, map, predicate keep/drop, and sort. Use `in 1 lane`
  for numeric value 1, including `01`; other positive values require `lanes`.
- `keep/drop first|last N` select directly by outer array position.
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
verb -> count/direction -> lanes -> using + named runnable (or inline body)
keep/drop -> lanes -> if -> named runnable or inline body
sort -> direction -> lanes -> by -> named runnable or inline body
```


## Example

```too
flow research(_, topic) -> Report:
  run -> Text[]:
    Generate distinct research directions for {{_}}.

  keep in 4 lanes if:
    Keep {{_}} only if it is specific and verifiable.

  sort descending in 3 lanes by:
    Score {{_}} by relevance to {{topic}}.

  keep first 3

  run -> Report:
    Synthesize {{_}} into one report.

  repeat 2 times:
    run: Improve the report's evidence and structure.
    until: Return true when another revision would not materially help.

  run publish
```


## Reserved Words

- `think` is reserved for a statically defined model step.
- `use` is reserved for a statically defined tool step.
- `thunk` is reserved.
- Removed `rank`, `par`, `top`, and `bottom` forms are rejected at Flow
  statement boundaries; they are not compatibility aliases.

The syntax of `think`, `use`, and `thunk` remains undefined. Future `think` and `use`
statements must emit the same model and tool steps as calls requested
dynamically by model output.


## Migration From Rank

Replace unbound `rank score top N` with `sort descending by score` followed
by `keep first N`. Replace unbound `rank score bottom N` with the same
descending sort followed by `keep last N` to preserve the final item order.

Named or discarded legacy rank-with-selection has no general equivalent
rewrite: an unbound sort updates `_`, while the old bound or discarded rank
left `_` unchanged. Re-author those flows with explicit binding boundaries.
A helper Flow can return the complete array type to preserve the same behavior.


## Migration to Current Flow Statements

Scatter/gather are removed; run retains its existing single-call behavior and
input/output contracts. Existing programs can express those calls with `run R`
in place of `scatter using R` or `gather using R`. For inline scatter,
use `run -> Text[]: BODY` or retain its explicit array output type. Run accepts
empty arrays; move any required nonempty validation into the callee.
Rename `storm` to `generate` and `settle` to `reduce`. Remove `using` before
inline generate/map/reduce bodies; retain it before named targets. Keep bindings,
`from` initializers, explicit types, and lane counts.

Output protocol objects contain `type`, `value`, and `binding`; stored outputs
contain `value` and `binding`, using the self-describing value codec. There is no
Local wrapper or `dim` field. Update output references to `output/value`.
RunStore schema 51 rejects older stores without modifying them; retain the
matching older runtime to inspect those stores, and use a fresh store for new
runs. No compatibility reader or automatic migration is provided. Old executable
snapshots require source migration and a newly prepared state before retry/rerun.
