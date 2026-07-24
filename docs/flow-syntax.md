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
and `A | B` marks alternatives. Required operands and roles come first;
optional clauses follow in the order shown.

`BODY` never includes its introducing colon:

```too
KEYWORD ...: LINE

KEYWORD ...:
  TEXT
```

When a statement consumes authored content, its `BODY` is a `ContentBody` and
follows [input-syntax.md](./input-syntax.md). A statement block uses `STMTS`
instead.


## Flow And Binding

```text
flow [NAME] [(PARAMS)] [-> T]:
  STMTS

STMT                    update `_`
let NAME = STMT         update `NAME`
let STMT                discard the result

let NAME: BODY          perceive a ContentBody and assign one `Percept` to `NAME`
```

Flow signatures use the executable parameter rules in
[program.md](./program.md), including implicit `_ : Part[]`, explicit `()`, and
named parameters.

`_` is the primary local. A statement may read all locals, but it computes its
result from a snapshot before the selected local is updated. Binding therefore
remains separate from statement execution.


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
scatter N EXPANDER
scatter N [-> T]: BODY
storm N MAPPER [par P]
storm N [par P] [-> T]: BODY

# Reduce a list into one item
gather MERGER
gather [-> T]: BODY
settle REDUCER
settle [-> T]: BODY

# Transform every list item
map MAPPER [par P]
map [par P] [-> T]: BODY

# Select or rank list items
keep first N
keep last N
keep FILTER [par P]
keep [par P]: BODY
drop first N
drop last N
drop FILTER [par P]
drop [par P]: BODY
rank SCORER [top N | bottom N] [par P]
rank [top N | bottom N] [par P]: BODY

# Repeat statements
repeat N:
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
scatter  scatter the current item into up to N items in one run
storm    storm N independent results from the current item
gather   gather the current list into one item in one run
settle   settle the current list into one item through sequential runs
map      map each current item to a new item while preserving order
keep     keep positional items or items accepted by a filter
drop     drop positional items or items accepted by a filter
rank     rank items by score and optionally keep the top or bottom N
repeat   repeat a statement block, bounded by N or until
```


## Rules

### Results

- Named runnable roles use the result contract declared by their agic or flow.
- Inline bodies use `-> T` only when their produced item type is configurable.
  Statement semantics determine whether those items form an `item` or `list`
  result shape.
- `ask` perceives its ContentBody for the human owner and returns the owner's
  canonical `Percept`, represented in the language as `Part[]`.
- A direct `let NAME: BODY` perceives its ContentBody as one `Percept` local
  with language type `Part[]`, without starting a child run.
- Inline `keep`, `drop`, and `until` bodies return `Boolean`.
- A `SCORER` or inline `rank` body returns `Number`.
- `repeat` is control flow. Its result is the last executed child result.


### Runs

- `run RUNNABLE` resolves in the current program. Inline `run` creates an
  inline agic.
- Bare `TEXT` is shorthand for inline `run` and starts the same child run.
- `seek AGENT RUNNABLE` resolves in the target agent's program. Inline `seek`
  sends its body to the target agent.
- `scatter` and `gather` each start one child run, then reshape its result.
- `storm` starts `N` independent child runs and preserves result order.
- `map`, filter-based `keep/drop`, and `rank` start one child run per item.
- `settle` starts one child run per item in sequence and carries its
  accumulator forward.
- Positional `keep/drop` do not start child runs.


### Clauses

- `par P` limits the concurrency of independent child work. It does not change
  result order.
- `keep/drop first|last N` select directly by current list position.
- `rank` performs a stable sort from highest score to lowest score. Equal
  scores preserve current list order.
- `top N` and `bottom N` are mutually exclusive. Selected items retain ranked
  order; omitting both returns the complete ranked list.
- Every `repeat` has `N`, `until`, or both. When both are present, the first
  stopping condition reached ends the loop. `until` is always final.

The common form is:

```text
keyword -> required operands and role -> optional clauses -> output -> body
```


## Example

```too
flow research(topic: Text) -> Report:
  scatter 8 -> Text:
    Generate distinct research directions for {{_}}.

  keep par 4:
    Keep this direction only if it is specific and verifiable.

  rank top 3 par 3:
    Score this direction by relevance to {{topic}}.

  gather -> Report:
    Synthesize the remaining directions into one report.

  repeat 2:
    run: Improve the report's evidence and structure.
    until: Return true when another revision would not materially help.

  run publish
```


## Reserved Words

- `think` is reserved for a statically defined model step.
- `use` is reserved for a statically defined tool step.
- `agic` is reserved for a future deferred or generated function construct.

The syntax of these words remains undefined. Future `think` and `use`
statements must emit the same model and tool steps as calls requested
dynamically by model output.
