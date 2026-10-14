# Chat Commands and Run Directives

Status: Approved for implementation on 2026-10-11 (PR #761).

## Goal and Scope

Distinguish standalone commands from directives describing one run submission.
Use `:` for session settings and `/` for interactions, overrides, and runnable
calls, with explicit command-collision handling and signature-based validation.
Cover Chat and the shared run-input envelopes used by Script/Task/Chore.
Preserve setting bodies, State bindings/shadowing, visibility, policy precedence,
execution, and storage. Script CLI selectors/flags, a completion UI, and private
runnable access are outside this change. Success means every accepted submission
has one command or one run, with deterministic name resolution, input boundaries,
and signature validation across surfaces.

## Terminology

Follow the existing [Call Input terminology](../call-input.md#terminology):
**parameter** is a signature declaration; **argument** (named input) is its
supplied named value; **input** means primary input bound to `_`; **Call Input**
is the complete bundle of input and arguments. A **submission** is the complete
submitted buffer, containing either a standalone command or a run submission.
A **call header** identifies the target and arguments before the primary-input
boundary.

A **command** performs a standalone interaction without submitting a run.
A **run directive** describes or instructs one run submission, specifying its
settings, runnable, or input. Run directives comprise **slash directives**,
**dollar directives**, and **at directives**, named after their leading markers.
Commands comprise **slash commands** for interactive operations and **colon
commands** for session settings.

Use **unqualified reference** for `review`, **kind-qualified reference** for
`agic:review` or `flow:review`, and **module-qualified reference** for
`module::flow:review`. Here **kind** means `agic`/`flow`; **type** means a value
type such as `Text`/`Number`. These grammar terms do not require schema renames.

## Dispatch

| Category | Form | Syntax | Purpose |
| --- | --- | --- | --- |
| Command | Slash command | `/help`, `/models`, `/output`, etc. | Query or control the interactive environment |
| Command | Colon command | `:model`, `:allow`, `:limit`, `:runnable`, `:agic`, `:flow`, `:workdir` | Inspect or change session settings |
| Run directive | Slash directive | `/model`, `/allow`, `/limit`, `/workdir`; `/RUNNABLE`, `/agic:RUNNABLE`, `/flow:RUNNABLE` | Apply a run override or specify the root runnable call |
| Run directive | Dollar directive | `$PROMPT` with arguments/input | Expand a prompt call inside Content |
| Run directive | At directive | `@RESOURCE` | Include a resource inside Content |

Each standalone command occupies a whole submission; invalid combinations have
no partial effect. The leading marker alone does not determine the category:
`/` introduces both slash commands and slash directives. Missing session setting
bodies show focused usage, except `:workdir` queries the session workdir. Unknown
colon commands reject. `/help` and `/?` cover the complete syntax; `:?` shows
session settings. Existing prompt/include semantics remain.

Use `:workdir [PATH]` for the session workdir and `/workdir PATH` for a run
override. Preserve workdir query, path resolution, reset
(`default`/`unset`), and propagation into later run requests.

After `/`, an exact built-in name selects its handler; errors never fall back
to a runnable. Otherwise use the valid State's final public binding:

- Reserved slash commands: `help`, `?`, `keys`, `models`, `tools`, `caps`,
  `agics`, `flows`, `output`, `show`, `exit`, `quit`.
- Reserved run override directives: `model`, `allow`, `limit`, `workdir`.
- Resolve collisions with `/agic:help`, `/flow:help`, `/flow:model`, etc.
  Kind/module qualifications constrain the existing target; they do not redo
  shadowing or expose private targets. Preserve existing qualified-reference
  and unnamed-entry resolution. Missing or mismatched refs fail.
- `runnable`, `agic`, `flow`, `default`, and `cd` are ordinary runnable names here.
  `/runnable review`, `/agic review`, and `/flow review` are not selector forms.
  There is no magic `/default`; specify the concrete target. Colon settings
  retain their existing `default` reset/name distinction.

Use exact case-sensitive names at column zero, with no space after the marker.
`/ review` and bare `/` are invalid. Preserve envelope normalization,
indentation, Markdown fences, and `//`, `::`, `$$`, and `@@` literal handling.
Once primary input begins, slash lines are content; expansion/unescaping never
redispatches commands.

## Run Submissions

A run submission contains an optional override prefix, then either a root
runnable call or Content for the default runnable. There is at most one root
call. Its header ends directive-prefix parsing: following lines belong to its
primary input, subject to the capture boundary. A command encountered before
primary input cannot be combined with overrides or a call.

Leading `/model`, `/allow`, `/limit`, and `/workdir` lines may be consecutive.
These slash directives form the **override prefix**. Preserve setting-body
grammar, duplicate-field rules, and policy precedence. A prefix requires
**nonempty Call Input**: at least one supplied primary-input or argument key,
not a truthy value.

| Submission | Admission requirement |
| --- | --- |
| Root runnable call, no run overrides | Signature alone decides whether Call Input may be empty |
| Overrides plus body | Body satisfies the default runnable's signature |
| Overrides plus explicit call | At least one primary/named input is supplied, and the complete signature is satisfied |
| Override prefix with empty Call Input | Reject; choosing a runnable does not supply input or arguments |

Consequences: `r()` can run directly but cannot accept a prefixed run override:
its signature forbids every possible supplied input. `r(topic?)` can run alone,
but with overrides must receive `topic`. A required `_` cannot be replaced by
named arguments. Explicit empty input counts only where its type accepts it.

```text
/model effort=high
/limit tokens=12000
/review topic=security -- Review this change.
```

All validation is atomic. Run overrides never update session defaults.
Run submissions queue during active runs; commands remain immediate and
Meta+Enter remains literal steer text. Local/remote and TUI/line Chat agree.
Script/Task/Chore share run syntax and reserved names, reject Chat-only
slash commands and unescaped colon headers at the submission boundary, and never
mutate a Chat session. Content already captured as primary input keeps its
existing literal-marker rules.

## Arguments and Primary Input

Arguments are `name=value`, in any order, before the primary-input marker.
Use POSIX quoting without shell expansion, for example `topic="security review"`.
Reject duplicate/unknown names, positional arguments, `_=` assignments, and
values incompatible with the signature. `_` is supplied only as primary input.

| Primary-input form | Syntax | Boundary |
| --- | --- | --- |
| Implicit stream input | Root call header followed by body, without a capture marker | Submission end |
| Line input | `-- TEXT`, with nonempty text | Current line end |
| Stream input (explicit) | `-` followed by body | Enclosing stream end; submission end for a root call |
| Fenced input | `$PROMPT ... ---` followed by a block | Exact closing `---` line; prompt calls only |

**Absent primary input** and **explicit empty primary input** are states, not
capture forms: `/review [name=value...]` alone omits `_`; `/review ... -` with
no following body supplies an empty `_`. Arguments may exist in either state.

After a root runnable `-- TEXT` line, any nonblank content rejects the entire
submission. Do not append it to the input or treat it as another call/command.
The stream marker `-` must end the header. Preserve the existing
[Call Input semantics](../call-input.md). Root runnable calls reject an
unquoted standalone `---` capture marker.

Fenced capture is reserved for `$prompt` calls inside Content. Its closing
`---` returns to the enclosing Content: subsequent text remains part of the
same runnable input, outside that prompt's primary input. Prompt calls retain all
three capture forms; ordinary `---` lines in already captured Content retain
their literal meaning outside a prompt capture.

```text
/review -
$summarize ---
Source material.
---
Also check the security implications.
```

Here `/review` receives the expanded summary followed by the final instruction.
There is no closing fence for the root call: it ends with the submission.

## Signature Cases

Agics and flows share these rules. In the table, `r` is the declaration name;
`/r` may be replaced by its qualified form. Types and requiredness come from the
[signature](../program.md#runnable-signatures), not from the command spelling.
Signature shapes are **implicit primary input** (`r`), **empty signature**
(`r()`), **primary input only** (`r(_)`), **named parameters only** (`r(topic)`),
and **primary input and named parameters** (`r(_, topic)`). Required/optional
describes each parameter independently of the signature shape.

| Declaration shape | Primary input | Named arguments | Example valid call |
| --- | --- | --- | --- |
| `r` (no parentheses) | Required, implicit `Part[]` | None | `/r -- text` |
| `r()` | Forbidden | None | `/r` |
| `r(_)` | Required `Part[]` | None | `/r -- text` |
| `r(_: Text)` | Required declared type | None | `/r -- text` |
| `r(topic)` | Forbidden | Required `topic: Text` | `/r topic=security` |
| `r(topic?)` | Forbidden | Optional `topic: Text` | `/r` |
| `r(topic, count: Number, detail?)` | Forbidden | Required `topic`, `count`; optional `detail` | `/r count=2 topic=security` |
| `r(_, topic, detail?)` | Required | Required `topic`; optional `detail` | `/r topic=security -- text` |
| `r(_, detail?)` | Required | Optional `detail` | `/r -- text` |

`_` must be first and cannot be optional. Named types default to `Text`; `_`
defaults to `Part[]`. Optional arguments remain absent when omitted: no automatic
empty/null/default value is inserted. Parameter default expressions, varargs,
and positional argument binding are not added. An output `-> T` changes none
of these input requirements.

Presence and type validation are separate:

- `topic=""`, `count=0`, and `enabled=false` are supplied values, not omissions.
- `/r -` supplies an empty `_`; bare `/r` does not. Empty `_` is valid for
  `Text` and `Part[]`, invalid when `_` is forbidden, and subject to conversion
  for other types. Never synthesize it to satisfy a required parameter.
- Typed arguments use the existing Content resolution and conversion:
  `count=2` for `Number`, `enabled=false` for `Boolean`, `data='{"ok":true}'`
  for `Json`, and `values='[1,2]'` for `Number[]`. Structs validate declared
  fields; `Part` requires one resolved part and `Part[]` accepts multiple.
  Invalid values fail before run admission; native-value API rules are unchanged.

## Migration and Risks

This breaks existing input syntax; incompatible lifetimes cannot be aliases.
Move old slash session settings to `:` and rename `/cd` to `:workdir`. `/cd`
becomes ordinary runnable lookup and no longer changes session settings. Move
colon run overrides to `/`, and `:flow review`/`:agic review`/`:runnable review`
calls to `/review` or a qualified name. Replace one-run `:runnable default` with
the concrete surface-default target. Old `:?` run help moves to `/help`.
Replace root runnable `---` capture with `-` and remove only its
closing fence; keep contained `$prompt` fences. This narrows the current Call
Input contract.

Old `:flow review` now changes the session, so syntax checks cannot protect
history replay. Previously accepted overrides with empty Call Input now reject.
Document migration of reused input and scheduled bodies; never rewrite stored
history heuristically. Adding a reserved built-in can shadow a short call;
list colliding runnable names with usable qualified calls and document the change.
Dollar/at directive evaluation, including the prohibition on nested prompt calls,
is unchanged. The namespace rules here supersede the corresponding parts of
[Chat Input Namespaces](chat-input-namespaces.md).

Open questions: none.

## Implementation and Acceptance

Implementation touchpoints under `src/toolang/`:

- `cli/toolang/commands/chat/{input,policy,commands,main,tui}.py`: dispatch,
  settings, help, outcomes, and queue parity.
- `execution/input.py`: submission parsing, input presence, and the shared
  reserved-name registry; no runtime services or Chat handlers.
- `execution/{policy,calls}.py`: setting application and runnable resolution.
- `lang/input.py`: call-input capture and signature validation.
- `cli/toolang/commands/script.py`, `work/scheduler.py`: shared-input integration.

Update current input/Chat/model documentation, affected bundled examples, and
relevant tests. Generate the breaking-change/migration changelog entry through
`too aid.too update_changelog`; preserve historical records.

Acceptance checks:

1. Every signature-table row passes for both agics and flows; missing required,
   forbidden primary, duplicate/unknown, and mistyped inputs reject.
2. Direct calls, supported captures, named-only arguments, and absent/empty
   inputs obey the override matrix, including `()`, optional arguments, `0`,
   and `false`. Root `-- TEXT` accepts only trailing whitespace and rejects any
   following text, prompt, or command atomically. Root fenced capture rejects;
   fenced `$prompt` expansion leaves following text in the same runnable input.
3. Built-in collisions require qualification; invalid built-ins never fall back.
   Ordinary `default`/`cd` names, State bindings, qualified refs, and literal
   content keep their boundaries.
4. Colon commands preserve session-setting behavior, including `:workdir` query,
   path resolution, and reset. `/workdir` remains run-local; `/cd` follows
   ordinary runnable lookup. Removed selector forms reject. Help and examples
   match, and all run-input surfaces agree.

Definition checks: verify preserved behavior against the parser, signature
binder, and existing tests; validate local links and run `git diff --check`.
Implementation requires the acceptance tests above for proposed behavior and
the repository default verification.
