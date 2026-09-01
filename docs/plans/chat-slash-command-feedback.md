# Chat Slash Command Feedback and Resource Discovery

## Status

Proposed for human approval in issue #431. Implementation is not started.

## Goal

Make submitted Chat slash commands self-contained scrollback interactions. A
command must return an explicit success confirmation, read-only result, usage
diagnostic, or error; session setting changes must leave the session coherent;
and users must be able to query effective models, tools, and capabilities
without leaving Chat.

## Success Criteria

- Every submitted slash command that keeps Chat open writes its command and a
  clear outcome summary to scrollback instead of using the transient status
  bar.
- Missing required arguments show the command's canonical usage, including for
  `/model`, `/agic`, and `/flow`.
- A session model is never retained outside the session's `allow.models`
  ceiling.
- `/models`, `/tools`, and `/caps` list effective resources and accept the
  existing collection-query language.
- Local TUI, remote TUI, and scripted Chat use one slash registry and outcome
  contract, with equivalent text semantics.
- Adding a command or a new result content type does not require duplicating
  parsing, error routing, or rendering branches.

## Command Surface

The existing setting and control commands remain unchanged. Add three
read-only discovery commands:

```text
/models [QUERY]
/tools [QUERY]
/caps [QUERY]
```

The complete argument tail is one existing `MatchUnion`; it is not split on
whitespace and does not introduce `--query` syntax. No argument lists the full
effective base collection. Examples:

```text
/models openrouter/*[reasoning]
/tools filesystem/*
/caps skill/*[scope=home]
/caps *[origin=local;form=authored]
```

`/caps` remains an umbrella rather than a collection schema. It applies the
query independently to `psyches`, `skills`, `services`, and `prompts`, then
combines results in their established order. Qualified identities use singular
kind prefixes such as `skill/reviewer`.

These commands query the agent's effective published resources before the
session allow ceiling. This lets a user discover a resource that is currently
outside the ceiling and then change `/allow` or `/model`. They never mutate
`SessionSetting`, start a thread, or submit a run. Popup, completion, and picker
behavior remain separate and out of scope.

The canonical allow field remains plural:

```text
/allow models=openrouter/*
```

`model=...` is not an alias because singular `model` is a binding and plural
`models` is a collection query.

## Unified Slash Outcome

Slash command recognition and metadata live in one registry. Input parsing
recognizes the structural `/NAME [BODY]` form without maintaining a second
allowlist or enforcing command-specific arity. The registry owns aliases,
summary, usage, and handler. It therefore becomes the sole source for `/help`,
unknown-command handling, and usage diagnostics.

Handlers return one semantic `SlashOutcome`:

```text
SlashOutcome
  kind: success | result | usage | error
  content: text | table | durable-run-result
```

The concrete types should be closed enough for exhaustive rendering but keep
outcome kind separate from content. `success` confirms that a state-changing or
control action was accepted; `result` presents data from a read-only command.
Text covers short confirmations and help, table covers resource discovery, and
the existing durable result value supports `/show`. A later content type can
add a renderer without changing every handler. Handlers do not call status-bar
error methods and do not render Rich objects directly.

The dispatcher converts known user-facing exceptions into `error` outcomes.
Unknown commands, malformed slash bodies, command failures, and slash-shaped
input that is incorrectly combined with other input use the same path. Direct
status-bar errors remain only for transient asynchronous run and transport
state, not completed slash submissions.

Commands that perform a state-changing or control action return `success` when
Chat remains open. For example, setting changes confirm the normalized value,
queue edits confirm the affected item, and accepted steering confirms the
action. Help, resource discovery, queue inspection, and `/show` return
`result`. `/exit` is the only silent success because it closes Chat before
another scrollback interaction is useful.

## Success, Result, Usage, and Error Presentation

The TUI appends one immutable scrollback interaction containing the submitted
command and its outcome. The command keeps the existing slash-command accent.
The body starts with a concise line that describes the concrete effect or
result. Successful and read-only outcomes do not print generic `Success:` or
`Result:` prefixes:

```text
/model
Usage: /model [MODEL] [effort=VALUE]

/missing
Error: Unknown command: /missing

/model openai/gpt-5 effort=high
Model set to openai/gpt-5 · high

/models openrouter/*[reasoning]
Found 2 models
MODEL                         STATE    EFFORT
openrouter/openai/gpt-5                low, medium, high
openrouter/openai/o3                   low, medium, high
```

The summary itself must be unambiguous: use an action phrase such as `Model set
to ...`, `Allowed 2 models`, or `Steer accepted`, and a result phrase such as
`Found 2 models` or `Chat commands`. Summaries describe the user's operation,
not the underlying query mechanism, so they do not say only that items
`matched`. Styling may distinguish outcome kinds but must not carry their
meaning alone. `Usage:` remains explicit guidance and `Error:` remains an
explicit failure. A table or durable run result follows its summary. The status
bar is refreshed after a successful setting mutation but is not the command's
confirmation. Outcomes remain in scrollback after later input and runs.

Insufficient arguments return `usage`, while a supplied but invalid argument
returns `error`. This rule applies to every registered command, not only the
three initially reported cases. In particular:

```text
/model [MODEL] [effort=VALUE]
/agic AGIC
/flow FLOW
/runnable RUNNABLE
/allow FIELD=QUERY...
/limit FIELD=VALUE...
/steer MESSAGE
```

An empty resource query is valid and lists all items. A valid query with no
matches is a read-only result such as `No models found`; an invalid query is an
error.

Scripted Chat projects the same outcomes to plain text. It no longer owns a
separate subset of slash commands or separate exception formatting.

## Resource Results

Discovery results use compact, copyable identities rather than exposing
provider brackets or internal keys:

- models: canonical `provider/model`, `current` or `default` state, and
  advertised reasoning effort levels;
- tools: canonical `toolset/tool`, plugin, and concise description;
- caps: kind-qualified query identity, scope, and concise description.

The table summary reports how many resources were found. Ordering is the base
collection's stable order; query syntax does not express priority. Empty
collections and empty matches use explicit result messages rather than errors.

Filtering must use the owning collection definitions:

- `ModelCollection` / `MODEL_DEFINITION` for models;
- `ToolCollection` / `TOOL_DEFINITION` for tools;
- `query_cap_views()` over the four cap definitions for combined caps.

The Chat layer must not implement substring filtering or duplicate query
parsing.

## Session Setting Coherence

Applying one slash setting remains atomic: parse the update, materialize a
candidate `SessionSetting`, reconcile selected identities, then replace the
current setting only if the operation succeeds.

`SessionSetting` has one selected resource identity: `model`. Runnable is a
route identity rather than an allow collection member, and tools and caps have
no selected session field. Model reconciliation therefore follows these
rules:

1. If `allow.models` is unchanged, the existing model behavior is unchanged.
2. If `/allow` changes `models` and the current model still matches the new
   ceiling, preserve the complete `ModelRequest`, including explicit effort.
3. If the current model does not match, clear the model and its parameters to
   `None`; do not silently select the first match because collection order is
   not model preference.
4. If the new ceiling contains no models, including `models=none`, the model is
   likewise `None`.
5. The result explicitly reports a cleared model so the user can follow with
   `/models QUERY` and `/model REF`.
6. An explicit `/model REF` must be within the current `allow.models` ceiling
   or fail without changing the session. `/model default` follows the same
   rule when the captured surface default is outside the current ceiling.
7. Parameter-only `/model effort=...` retains the allowed model identity and
   continues to use the existing model-support validation. A cleared model
   cannot accept model parameters.

A successful `/allow` reports the effective count for every field changed by
that command, rather than echoing the authored query. For example, one command
may report `Allowed 2 models, 5 tools`; `models=none` reports `Allowed 0 models`.
These counts describe the resulting session ceiling and use the same effective
resource snapshot as reconciliation.

Example:

```text
/allow models=openrouter/*
Allowed 2 models
Model cleared: deepseek/abc is outside allow.models

/models openrouter/*[reasoning]
Found 2 models
MODEL                         STATE    EFFORT
openrouter/openai/gpt-5                low, medium, high
openrouter/openai/o3                   low, medium, high

/model openrouter/openai/gpt-5 effort=high
Model set to openrouter/openai/gpt-5 · high
```

Changing `tools`, `psyches`, `skills`, `services`, or `prompts` preserves the
model. Runnable compatibility with narrowed resources remains authoritative at
run materialization because a runnable may resolve nested resource needs; this
feature does not invent a second runnable-resource validator in Chat.

## Local and Remote Resource Contract

Extend the Chat client with query-aware model, tool, and cap discovery that
returns presentation-neutral records. Local Chat reads one current Setup and
State snapshot and evaluates queries through their owning collections. Remote
Chat requests the same projected records from the resident agent API.

The API extends `GET /api/v1/models` and `GET /api/v1/caps` with repeatable
`query` parameters and adds `GET /api/v1/tools` with the same convention.
Filtering happens against the server's current effective Setup or State before
projection, so remote clients do not reconstruct private collection records or
query fields. Existing no-query response behavior remains compatible.

The same model discovery contract supplies model-command resolution and
session reconciliation. Local and remote clients must therefore agree on
canonical refs, query errors, empty matches, current/default markers, and
advertised effort values.

## Scope

Included:

- one slash registry and typed outcome/content contract;
- scrollback rendering for success confirmations, results, usage, and errors;
- plain-text projection for scripted Chat;
- consistent arity and unknown-command handling;
- confirmations for setting and control commands that keep Chat open;
- model reconciliation after `allow.models` changes;
- `/models`, `/tools`, and `/caps` query commands;
- query-aware effective-resource API and local/remote parity;
- concise Chat, query, and API documentation plus offline tests.

Excluded:

- popup, completion, picker, key-binding, or input-draft interaction changes;
- query grammar, schemas, set semantics, or collection ordering changes;
- persistent settings outside one Chat session;
- automatic model preference, ranking, or fallback selection;
- eager runnable compatibility checks after allow changes;
- new model parameters or changes to `RunRequest`, run persistence, or
  execution semantics.

## Design Touchpoints

- `src/toolang/cli/toolang/commands/chat/input.py`: structural slash parsing
  without a duplicated command allowlist.
- `src/toolang/cli/toolang/commands/chat/slashes.py`: registry metadata,
  outcomes, command dispatch, confirmations, and discovery commands.
- `src/toolang/cli/toolang/commands/chat/base.py` and `policy.py`: client
  resource contract and atomic session/model reconciliation.
- `src/toolang/cli/toolang/commands/chat/{local,remote}.py`: snapshot-owned
  resource queries and transport parity.
- `src/toolang/cli/toolang/commands/chat/{blocks,tui,main}.py`: centralized Rich
  and plain-text outcome projection; remove status-bar slash errors and the
  scripted command fork.
- `src/toolang/api/routers/{agent,caps}.py` and API schemas: query-aware model,
  tool, and combined-cap projections.
- Existing model, tool, cap collection definitions remain the sole query
  owners.
- Chat input, query, caps, model, and API documentation describe the added
  commands and unchanged plural/singular boundary.

## Acceptance Tests

1. TUI submissions append clearly summarized success, result, usage, and error
   outcomes to scrollback and do not use the status bar for synchronous slash
   feedback; later input does not erase them. Success and result summaries do
   not print generic kind labels.
2. Unknown commands and slash parsing/body failures use the registered error
   presentation. `/model`, `/agic`, `/flow`, and every other missing required
   body use registered usage text.
3. Help is generated from the same registry and includes `/models`, `/tools`,
   and `/caps`; adding a test command requires no parser allowlist change.
4. Every setting command returns `success` with its normalized affected value.
   Queue mutation and steer acceptance also return `success`; help, queue
   inspection, discovery, and show return `result`; failures use the same
   outcome dispatcher; `/exit` remains silent.
5. Narrowing `allow.models` preserves an allowed `ModelRequest`, clears an
   excluded model and effort, reports effective counts for every changed allow
   field, and reports the clearing. Invalid queries leave the complete prior
   setting unchanged.
6. Explicit model selection outside the current model ceiling fails atomically;
   allowed identity changes and effort-only changes retain existing reasoning
   support checks.
7. `/models`, `/tools`, and `/caps` list all effective base resources with no
   query, apply valid advanced queries through the owning definitions, preserve
   base order, show copyable identities and `Found N ...` summaries, and treat
   zero matches as a successful `No ... found` result.
8. Combined cap queries support qualified and unqualified identities and common
   predicates without introducing a `caps` schema.
9. Remote API query parameters and the new tool endpoint match local results,
   sanitize invalid-query failures, and preserve existing no-query model and
   cap consumers.
10. Scripted and TUI Chat dispatch the same command registry and produce
    semantically equivalent success, result, usage, and error text.
11. Existing run submission, queue snapshot, status bar, popup-free input,
    execution, and persistence tests remain unchanged; ruff, formatting, ty,
    and the default offline pytest suite pass.

## Risks

- Clearing an excluded model can temporarily leave an agic session without a
  model. The explicit scrollback result makes this visible and avoids choosing
  an arbitrary fallback; the next `/model` restores a runnable model binding.
- Effective resource snapshots may refresh between discovery and submission.
  Run materialization remains authoritative and reports any later change.
- Resource tables can be wide. Their Chat projections stay intentionally
  compact and do not duplicate full standalone CLI inventory tables.

## Open Questions

None.
