# Chat Input Namespaces

Status: Approved for implementation on 2026-08-29; legacy aliases were removed
from the approved scope on 2026-08-29.

> The prompt-input forms and prompt-composition rules in this historical plan
> are superseded by [Define Call Input](./call-input.md).

## Work Type

Feature definition for terminal-chat commands, execution-policy directives,
prompt expansion, resource inclusion, and their durable provenance.

This definition changes authored input syntax and durable run preparation data.
It does not implement product code.

## Verified Current Behavior

- Terminal Chat calls commands such as `:help`, `:model`, and `:show` slash
  commands internally even though their public spelling uses a leading colon.
- A leading colon before primary input also owns execution-policy commands such
  as `:allow`, `:default`, `:limit`, `:model MODEL`, and `:flow FLOW`.
- A policy-only Chat input changes later session runs. The same policy prefix
  followed by runnable input applies only to that run.
- Reusable prompt templates are invoked from `Content` with `/name`. Prompt
  calls support no input, remaining-content input, and fenced input.
- `/` and `@` are special only at column zero outside ordinary Markdown code
  fences. `:` is special only in the policy prefix before primary input.
- `RunRequest` carries unresolved authored source, but `resolve_spec()` expands
  prompt calls before constructing `RunSpec`. The durable preparation control
  retains resolved locals and the Agent State revision, not the authored prompt
  invocation.
- A model step persists the normalized instructions, messages, tools, and
  continuation that were actually sent through the model adapter.
- The input box renders the submitted source while the run is live, but durable
  thread and run projections are rebuilt from resolved locals.

## Goal And Success Criteria

Give every leading marker one stable responsibility:

- `/` invokes a terminal-chat interaction;
- `$` expands a reusable prompt template inside `Content`;
- `:` declares execution policy before runnable input; and
- `@` includes one resource inside `Content`.

Preserve authored intent for editing and inspection while resolving every
prompt against the run's immutable Agent State before execution. Model adapters
must receive only normalized model calls, never Toolang prompt syntax.

The change succeeds when:

- Chat commands use canonical slash spelling and occupy a complete submission;
- prompt calls use canonical dollar spelling on every `Content` surface;
- colon policy semantics remain shared by Chat, Script, Task, and Chore;
- every special line follows one column-zero and escaping rule;
- prompt input can be absent, inline, remaining-content, or fenced;
- the input box and durable transcript preserve authored prompt syntax;
- durable provenance identifies every expanded prompt and immutable definition;
- model-call inspection continues to reproduce the exact expanded request;
- retry and rerun do not change when a prompt definition changes later;
- former colon quick commands and slash prompt calls are rejected; and
- the complete default verification passes offline.

## Scope

In scope:

- shared authored-input grammar and evaluation;
- terminal-chat command parsing, handling, help, rendering, and completion;
- prompt invocation syntax, arguments, input scopes, nesting, and escaping;
- policy-prefix classification and existing session/run scoping;
- authored-input and prompt-invocation provenance in run preparation records;
- transcript, history, retry, rerun, and model-call inspection projections;
- local and remote authored-run protocol parity;
- canonical syntax documentation and focused tests.

Out of scope:

- prompt template body syntax or parameter types;
- new execution-policy fields or selector behavior;
- new Chat commands beyond argument forms needed for existing model and runnable
  selection;
- provider adapter, model plugin, or model API protocol changes;
- `kind:name` runnable identities and `key:value` selector fields;
- shell execution syntax or a `!` input namespace;
- expanding prompt bodies into the input box automatically;
- a command that resubmits old authored input against the latest prompt;
- changing include resolution, upload references, or file-picker behavior;
- WebUI layout beyond consuming the same authored and resolved API facts.

## Namespace Contract

The canonical markers are:

| Marker | Namespace | Surface | Meaning |
| --- | --- | --- | --- |
| `/` | interaction | terminal Chat | perform one immediate Chat action |
| `$` | prompt | every `Content` surface | expand one reusable prompt template |
| `:` | policy | run-capable submission prefix | declare session or run execution policy |
| `@` | resource | every `Content` surface | include one caller-authorized resource |

The first character of a special line must be at physical column zero. Leading
spaces or tabs make the complete line ordinary text. Chat may remove envelope
blank lines, but it must preserve indentation on the first nonblank line.

Ordinary Markdown code fences suspend all marker recognition. Prompt fenced
input is different: its delimiters select the prompt's input boundary, and the
captured body is evaluated recursively as `Content` after those delimiters are
removed.

Doubling a marker at column zero produces a literal leading marker:

```text
//help      -> /help
$$review    -> $review
::model     -> :model
@@README.md -> @README.md
```

The escape applies only where the single marker would otherwise be special.
After a line has become ordinary text, its remaining characters are not
rescanned for markers.

## Terminal-Chat Commands

A slash command is recognized only from the first nonblank line of a terminal
Chat submission and must occupy the complete normalized submission. A slash
command cannot be combined with policy directives or runnable input.

Canonical commands are:

```text
/help                     /show [RUN_ID]
/?                        /queue [ACTION]
/model [MODEL]            /steer MESSAGE
/agic [AGIC]              /quit
/flow [FLOW]              /exit
/runnable [RUNNABLE]
```

`/model` without an argument lists models; there is no plural `/models`
command. No-argument runnable forms retain their current listing behavior.
Supplying one model or runnable selector validates and applies the corresponding
session default without creating a run. `/agic` and `/flow` qualify the selected
runnable kind. They do not accept runnable named inputs; use a colon runnable
directive to start a run with named input.

`/queue` and `/steer` retain their current aliases and behavior. Help, errors,
status guidance, transcript blocks, and reconnect guidance use slash spelling.
An unknown complete slash command is an error. Slash has no execution meaning
inside an already-started `Content` value or on non-Chat surfaces.

## Execution-Policy Directives

Colon remains the only policy marker. Canonical directives and shortcuts keep
their existing fields and meanings:

```text
:allow DOMAIN=SELECTORS
:default FIELD=VALUE
:limit FIELD=VALUE

:model MODEL
:agic AGIC [NAME=VALUE ...]
:flow FLOW [NAME=VALUE ...]
:runnable RUNNABLE [NAME=VALUE ...]

:models SELECTORS       :psyches SELECTORS
:tools SELECTORS        :skills SELECTORS
:caps SELECTORS         :services SELECTORS
                        :prompts SELECTORS
```

Policy directives are consecutive column-zero lines before primary `Content`.
A policy-only Chat submission updates later session runs. A policy prefix paired
with primary or named runnable input applies only to that run. Script, Task, and
Chore continue to parse policy plus runnable input without terminal-chat
commands.

Colon policy syntax is durable. Former colon spellings for immediate Chat
actions are not aliases.

## Prompt Expansion

A prompt is a reusable `Content` template, so its invocation marker is `$`, not
the terminal-interaction marker `/`.

```text
PromptCall = PromptHeader
           | TailPrompt
           | InlinePrompt
           | FencedPrompt(n)

PromptHeader = "$" PromptName (Space Argument)*

TailPrompt
    = PromptHeader Space "-" LineBreak RemainingContent

InlinePrompt
    = PromptHeader Space "--" Space InlineText

FencedPrompt(n)
    = PromptHeader Space Fence(n) LineBreak FencedContent(n) FenceLine(n)
```

`Argument` remains `name=value` with the current POSIX word quoting and escaping
rules. `_` remains reserved for prompt primary input. The `-`, `--`, or opening
fence delimiter must be an unquoted standalone token after all arguments.

### No Primary Input

With no delimiter, the prompt receives an empty primary input and later lines
remain in the enclosing `Content`:

```text
$review focus=security
Continue outside the prompt.
```

### Remaining-Content Input

A terminal `-` consumes the following physical lines through the end of the
current `Content` scope:

```text
$review focus=security -
Review this API:
@src/app.py
```

At least one following physical line is required. Captured lines are evaluated
as `Content`, so column-zero `$` and `@` markers remain active and prompt calls
resolve from the innermost invocation outward. Indented markers remain text.

### Inline Input

A standalone `--` consumes the nonempty remainder of its physical line:

```text
$review focus=security -- Review this API
```

The whitespace run separating `--` from `InlineText` is syntax and is not part
of the input. The remaining text is one text part; `$`, `@`, `:`, and `/` inside
it are literal because they are not at column zero. Later lines remain in the
enclosing `Content`.

`--` without non-whitespace inline text is invalid. Inline input is text-only;
callers use remaining-content or fenced input for includes, nested prompts, or
multimodal parts.

### Fenced Input

An exact backtick fence of length three or greater captures a bounded multiline
body while leaving later lines in the enclosing `Content`:

````text
$review focus=security ```
Review this API:
@src/app.py
```

Continue outside the prompt.
````

The closing fence must be a complete line matching the opening fence exactly.
Fence lines are excluded. An unclosed fence is invalid. The captured body is
evaluated as `Content`, including column-zero prompt and resource markers.

## Parsing And Resolution Boundary

Parsing stays independent of catalog and runtime services:

1. Chat classifies a complete recognized `/command` before run-only parsing.
2. The policy parser consumes consecutive column-zero colon directives.
3. The language parser produces prompt-call syntax from column-zero `$` lines
   and resource includes from column-zero `@` lines.
4. Execution selects the runnable and immutable Agent State snapshot.
5. `resolve_input_parts()` resolves includes and recursively expands prompts,
   validates arguments, and detects cycles.
6. Runnable input coercion produces the immutable `RunSpec`.
7. Agic preparation constructs normalized messages and model steps construct the
   exact `ModelCall` supplied to the selected adapter.

Missing prompts, invalid arguments, cycles, missing delimiters, and input
coercion failures reject the request before the run is accepted. Adapters never
load prompt definitions, parse authored syntax, or perform expansion.

## Input Box And Completion

The input box is an authored-source editor. Selecting a prompt inserts its
`$name` header and argument placeholders; it never replaces the call with the
template body. Selecting a Chat command inserts `/name` and any command-specific
argument placeholder.

Completion namespaces remain separate:

- `/` lists built-in Chat commands only;
- `$` lists available prompts and their parameter signatures;
- `:` lists policy directives and valid fields where completion is available;
- `@` retains the existing resource-picker behavior.

The submitted transcript block displays authored syntax. A UI may offer an
explicit read-only expansion view, but editing that view or silently replacing
the input is out of scope.

## Durable Authored And Effective Facts

One run must retain both user intent and execution truth.

The root preparation control adds the normalized authored policy and
`RunnableInputRaw` sources used for the request. It also stores an ordered
prompt-invocation provenance collection. Each invocation contains:

- prompt name;
- canonical argument bindings;
- input scope: `none`, `tail`, `inline`, or `fenced`;
- parent invocation index for nesting, or none;
- cap ref and content hash; and
- the already-recorded Agent State revision supplies the immutable state
  identity.

Prompt bodies need not be duplicated in every run because immutable Agent State
and the content hash identify the definition. The effective resolved locals
remain in the preparation control. Model steps continue to content-address and
persist the normalized instructions, messages, tools, and continuation actually
sent to the adapter.

Projections use these facts deliberately:

- transcript, input history, and run authored-input inspection use the authored
  sources;
- prompt usage inspection uses invocation provenance;
- conversation recall uses resolved user-message parts;
- model-call inspection uses the persisted effective `ModelCall`; and
- retry and rerun reuse durable resolved input and the recorded state rather
  than expanding the authored call against current prompts.

Submitting the authored source again is a new run and resolves against the new
run's current immutable state. This is the existing way to opt into a changed
prompt definition.

## Canonical Rollout

The namespace change has no legacy alias window:

- slash is the only spelling for immediate Chat commands;
- dollar is the only spelling for prompt calls;
- former colon quick commands are rejected, while colon policy directives stay
  unchanged;
- no-argument `:models` is invalid, while `:models SELECTORS` remains a durable
  policy directive;
- `/prompt` is ordinary text on non-Chat `Content` surfaces and an unknown Chat
  command at the start of a Chat submission; and
- help, completion, examples, generated guidance, and formatting emit only the
  canonical spellings.

Release notes must call out shell quoting: `$` is interpreted by common shells,
so one-shot shell arguments use single quotes, for example
`too run alice '$review focus=security'`. TUI input, authored files, and JSON
request strings require no shell-specific escaping.

## Design Touchpoints

- `src/toolang/lang/input.py`
- `src/toolang/execution/calls.py`
- `src/toolang/execution/schemas.py`
- `src/toolang/execution/records.py`
- `src/toolang/execution/store.py`
- `src/toolang/execution/executor/executor.py`
- `src/toolang/execution/executor/steps/model.py`
- `src/toolang/api/schemas.py`
- `src/toolang/api/conversion.py`
- `src/toolang/cli/toolang/commands/chat/input.py`
- `src/toolang/cli/toolang/commands/chat/slashes.py`
- `src/toolang/cli/toolang/commands/chat/blocks.py`
- `src/toolang/cli/toolang/commands/chat/tui.py`
- focused language, execution, API, Chat unit, integration, and system tests
- `README.md`, `docs/input-syntax.md`, `docs/chat.md`, and `docs/program.md`

No model adapter, model plugin, prompt template body, selector, runnable identity,
or external provider contract change is expected.

## Acceptance Tests

1. `/help`, `/show`, queue, steer, exit, model, and runnable commands parse only
   as complete terminal-chat submissions at column zero.
2. Slash model and runnable arguments update validated session defaults without
   creating runs; their no-argument forms retain listing behavior.
3. Policy-only colon input retains session scope, while the same directives
   paired with runnable input apply only to that run.
4. `$prompt` expands with no input, remaining-content input, inline input, and
   fenced input on Chat, Script, Task, Chore, and authored runnable bodies.
5. Inline `--` requires text, consumes only the current line, leaves later lines
   outside the invocation, and treats embedded markers literally.
6. Tail `-` and fenced input evaluate column-zero nested prompts and includes;
   indented markers remain text.
7. Ordinary Markdown fences suspend marker recognition, and `//`, `$$`, `::`,
   and `@@` produce literal leading markers where their single forms are special.
8. Missing prompts, invalid or duplicate arguments, cycles, malformed
   delimiters, and unclosed fences fail before run acceptance.
9. Local and remote authored requests resolve the same source against the same
   immutable state and produce equivalent `RunSpec` values.
10. The durable preparation record preserves authored sources, ordered nested
    invocation provenance, resolved locals, cap refs, hashes, and state revision.
11. Transcript and input history show authored `$prompt` syntax, while recall
    and adapter requests contain expanded semantic content.
12. Model-step inspection rebuilds the exact expanded `ModelCall` even after the
    prompt definition changes or disappears from current state.
13. Retry and rerun remain unchanged after prompt mutation; explicitly
    resubmitting the authored source uses the new prompt definition.
14. Former colon quick commands and Chat `/prompt` spellings are rejected, and
    non-Chat `/prompt` text is not expanded.
15. Shell-facing examples quote dollar prompt calls and do not accidentally
    expand environment variables.
16. The complete default verification passes offline.

## Risks And Open Questions

Dollar-prefixed text passed through a shell can expand as an environment
variable. Canonical shell examples use single quotes; Toolang does not add a
second shell-aware grammar layer.

Persisting authored source and invocation provenance enlarges preparation
records and requires an additive store migration. Keep effective model calls
content-addressed and avoid duplicating prompt bodies per run.

There are no open product decisions.
