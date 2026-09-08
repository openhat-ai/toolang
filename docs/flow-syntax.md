# Flow Statement Syntax

This document defines the flow surface syntax in one place. It covers authored
statements and their observable semantics; executor, trace, and lowering
details remain in their owning documents.


## Notation

```text
NAME       local name
T          Toolang type
N          non-negative count or selection size
P          positive concurrency limit
VALUE_STMT a result-producing statement other than repeat
RUNNABLE   named agic or flow
AGENT      agent selector
EXPANDER   one-run runnable returning a list
MERGER     one-run runnable merging a list into one item
MAPPER     per-input runnable returning one item
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
the verb. Named runnable and lane clauses may exchange order; an inline
runnable always comes last.

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
`far`, `near`, and `line` are reserved runtime-local names and cannot be used
as authored bindings.
`repeat` is different: it produces no result and accepts no `let` binding. Its
body statements update the current flow locals normally as the loop proceeds.


## Statements

```text
# Produce one item
run RUNNABLE
run [-> T]: BODY
TEXT                                      shorthand for inline `run`
seek AGENT RUNNABLE
seek AGENT [-> T]: BODY
ask: BODY

# Expand one item into a list
scatter N using EXPANDER
scatter N using [-> T]: BODY
storm N [in P lanes] using MAPPER
storm N [in P lanes] using [-> T]: BODY

# Reduce a list into one item
gather using MERGER
gather using [-> T]: BODY
settle using REDUCER
settle using [-> T]: BODY

# Transform every list item
map [in P lanes] using MAPPER
map [in P lanes] using [-> T]: BODY

# Select or sort list items
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
repeat N times:
  STMTS
  [until: BODY]

repeat:
  STMTS
  until: BODY
```


## Natural Reading

```text
run      run a named agic or flow, or an inline agic
seek     seek another agent's help with a named runnable or inline request
ask      ask the human owner for input, judgment, or confirmation
scatter  scatter the current item into a list in one run
storm    storm N independent results from the current item
gather   gather the current list into one item in one run
settle   settle the current list into one item through sequential runs
map      map each current item to a new item while preserving order
keep     keep positional items or items accepted by a filter
drop     drop positional items or items accepted by a filter
sort     sort all items by score in the required ascending or descending order
repeat   repeat a statement block, bounded by N or until
```

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
- Use two spaces when authoring. Other widths and tabs are supported, but a
  structural indentation prefix cannot mix tabs and spaces or change spelling
  at an existing level. Tabs use eight-column stops for parsing.
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
- Inline bodies may declare their produced item type with `-> T`.
  Statement semantics determine whether those items form an `item` or `list`
  result shape.
- `ask` evaluates its `Content` for the human owner and returns the owner's
  canonical `Percept`, represented in the language as `Part[]`.
- A direct `let NAME = BODY` evaluates its `Content` as one `Percept` local
  with language type `Part[]`, without starting a child run.
- Inline `keep`, `drop`, and `until` bodies default to `Boolean`; inline
  `sort` defaults to `Number`. An explicit incompatible return type is rejected.
- Named filters must declare `Boolean`; named scorers must declare `Number`.
- Generated inline `keep`, `drop`, `sort`, and `until` evaluators do not
  recall thread history or receive tools. Use a named agic when evaluation
  intentionally needs either resource.
- `repeat` is control flow, not a value statement. It has no result or binding.
  Its body statements update the same working locals according to their own
  bindings. Zero iterations leave locals unchanged.


### Runs

- `run RUNNABLE` resolves in the current program. Inline `run` creates an
  inline agic.
- Bare `TEXT` is shorthand for inline `run` and starts the same child run.
- `seek AGENT RUNNABLE` resolves in the target agent's program. Inline `seek`
  sends its body to the target agent.
- `scatter` and `gather` each start one child run, then reshape its result.
- The current `scatter N` surface retains `N`, but execution uses the returned
  array length and neither validates nor truncates it.
- `storm` starts `N` independent child runs and preserves result order.
- `map`, filter-based `keep/drop`, and `sort` start one child run per item.
- `settle` starts one child run per item in sequence. The first run receives
  empty primary input `_` and source item `0` as argument `item`; each output
  becomes `_` for the next run. Inline settle agics receive an implicit
  `item: Part[]` parameter. This accumulator is internal to settle; outer flow
  locals receive only the final successful result through settle's binding.
- Positional `keep/drop` do not start child runs.


### Clauses

- `in P lanes` limits independent child work without changing result order.
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
- Every `repeat` has `N`, `until`, or both. When both are present, the first
  stopping condition reached ends the loop. `until` is always final, reads the
  latest locals after the iteration, and does not bind its Boolean result. A
  failed evaluator run or failed Boolean coercion fails the repeat and its
  enclosing flow; failure is never interpreted as `false`.

The common form is:

```text
verb -> count/direction -> lanes -> using/if/by -> runnable or inline body
```


## Example

```too
flow research(topic) -> Report:
  scatter 8 using -> Text:
    Generate distinct research directions for {{_}}.

  keep in 4 lanes if:
    Keep this direction only if it is specific and verifiable.

  sort descending in 3 lanes by:
    Score this direction by relevance to {{topic}}.

  keep first 3

  gather using -> Report:
    Synthesize the remaining directions into one report.

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
Wrapping the two statements in a helper flow does not preserve collection
shape across the current item-based runnable boundary.
