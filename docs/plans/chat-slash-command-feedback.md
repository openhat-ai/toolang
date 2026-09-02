# Chat Slash Command Feedback and Resource Discovery

## Status

Approved on 2026-09-01. Definition issue: #431. Implemented by #434.

The resource-table and normal model-status presentation was subsequently
refined by
[Concise Chat Resource Tables and Model Status](chat-resource-table-presentation.md),
implemented by #441. That later definition is authoritative where its table
or normal-status presentation differs from this plan. #443 subsequently
corrected reasoning-effort delivery without changing the slash outcome,
input-routing, status-diagnostic, resource-query, or session-coherence
contracts defined here.

## Goal

Make recognized Chat slash commands self-contained scrollback interactions. A
submitted command must return an explicit success, result, usage, or error;
incomplete or unrecognized command syntax and rejected run input must remain
editable with an actionable status diagnostic; session setting changes must
leave the session coherent; and users must be able to query effective models,
tools, and capabilities without leaving Chat.

## Success Criteria

- Every recognized slash command that keeps Chat open clears the input and
  writes its command plus `success`, `result`, `usage`, or `error` outcome to
  scrollback.
- Bare or unrecognized slash syntax, and run input rejected before dispatch or
  queue insertion, keeps the input unchanged and reports an actionable status
  diagnostic instead of creating scrollback.
- Missing required arguments show the command's canonical usage, including for
  `/model`, `/agic`, and `/flow`.
- `/?` and `:?` explain their surface's purpose and composition constraint
  before listing copyable command or override forms.
- `/keys` explains the interactive Chat keyboard scope and lists every
  Toolang-owned shortcut with its context-dependent behavior.
- A session model is never retained outside the session's `allow.models`
  ceiling.
- `/models`, `/tools`, and `/caps` list effective resources and accept the
  existing collection-query language.
- Local TUI, remote TUI, and scripted Chat use one slash registry and outcome
  contract, with equivalent text semantics.
- Adding a command or a new result content type does not require duplicating
  parsing, error routing, or rendering branches.
- Status-bar errors are reserved for incomplete or unrecognized command syntax,
  rejected run input, UI controls, and unresolved asynchronous state.
- Status diagnostics have explicit transient or persistent lifetimes, concise
  actionable wording, and a presentation that does not rely on color alone.

## Command Surface

The existing setting and control commands remain unchanged. Add three read-only
resource discovery commands and one keyboard-help command:

```text
/models [QUERY]
/tools [QUERY]
/caps [QUERY]
/keys
```

For each resource discovery command, the complete argument tail is one existing
`MatchUnion`; it is not split on whitespace and does not introduce `--query`
syntax. No argument lists the full effective base collection. Examples:

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

The three discovery commands query the agent's effective published resources
before the session allow ceiling. This lets a user discover a resource that is
currently outside the ceiling and then change `/allow` or `/model`. None of the
four commands mutates `SessionSetting`, starts a thread, or submits a run. Popup,
completion, and picker behavior remain separate and out of scope.

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
Malformed bodies and command failures use that path once the command name has
been recognized. Registry recognition is the presentation boundary: a bare
slash or a name absent from the registry produces an unrecognized-input
diagnostic in status. Once a registered command is identified, every
`SlashOutcome` is a completed command submission: `success`, `result`, `usage`,
and `error` all enter scrollback and clear the input. Outcome kind describes
what the command returned; it does not decide whether the command was submitted.
Handlers do not write status directly, so scripted Chat can project the same
outcomes as plain text.

Commands that perform a state-changing or control action return `success` when
Chat remains open. For example, setting changes confirm the normalized value,
queue edits confirm the affected item, and accepted steering confirms the
action. Help, resource discovery, queue inspection, and `/show` return
`result`. `/exit` is the only silent success because it closes Chat before
another scrollback interaction is useful.

## Success, Result, Usage, and Error Presentation

For a recognized command, the TUI appends one immutable scrollback interaction
containing the command and its outcome, then clears the input. The command keeps
the existing slash-command accent. The body starts with a concise line that
describes the concrete effect, result, usage, or failure. Successful and
read-only outcomes do not print generic `Success:` or `Result:` prefixes:

```text
/model
Usage: /model [MODEL] [effort=VALUE]

/model missing/model
Error: Model selection is unknown: missing/model

/model openai/gpt-5 effort=high
Model set to openai/gpt-5 · high

/models openrouter/*[reasoning]
Found 2 models
MODEL                         PRICE ($/1M)      EFFORT
────────────────────────────  ────────────────  ──────────────────
openrouter/openai/gpt-5       $ 1.25 / $10.00   low, medium, high
openrouter/openai/o3          $ 2.00 / $ 8.00   low, medium, high
```

The summary itself must be unambiguous: use an action phrase such as `Model set
to ...`, `Allowed 2 models`, or `Steer accepted`, and a result phrase such as
`Found 2 models`. Summaries describe the user's operation, not the underlying
query mechanism, so they do not say only that items `matched`. Help results may
start directly with their purpose sentence instead of repeating a title already
implied by the submitted command. Styling may distinguish outcome kinds but must
not carry their meaning alone. `Usage:` remains explicit guidance and `Error:`
remains an explicit command failure. A table or durable run result follows its
summary. The status bar is refreshed after a successful setting mutation but is
not the command's confirmation. Outcomes remain in scrollback after later input
and runs.

Insufficient arguments return `usage`, while a supplied but invalid argument
returns `error`. Both are completed outcomes for a recognized slash command and
therefore enter scrollback. This rule applies to every registered command, not
only the three initially reported cases. In particular:

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

A bare `/` is incomplete input rather than an unknown empty command. It remains
in the input box and shows a status hint. `/?` is a recognized help command and
writes the command list to scrollback. An unknown named command has not reached
slash dispatch, so it likewise remains editable and includes the same recovery
action:

```text
/
! Enter a command after / · See /? for help

/?
Slash commands act immediately.
Setting commands change defaults for future runs in this Chat session.

Submit one slash command by itself; it cannot be combined with run input.
See :? to change settings for one run only.

Available commands:
  /model [MODEL] [effort=VALUE]  Set the session model or effort
  /agic AGIC                     Switch the session agic
  /flow FLOW                     Switch the session flow
  /runnable RUNNABLE             Switch the session runnable
  /allow FIELD=QUERY...          Set session resource ceilings
  /limit FIELD=VALUE...          Set session run limits
  /models [QUERY]                Find models
  /tools [QUERY]                 Find tools
  /caps [QUERY]                  Find capabilities
  /queue [ACTION]                Inspect or edit queued submissions
  /steer MESSAGE                 Steer the active run
  /show [RUN_ID]                 Show a run result
  /keys                          Show keyboard shortcuts
  /help, /?                      Show this help
  /exit                          Exit Chat

/missing
! Unknown command /missing · See /? for help
```

The introductory purpose and constraint text is part of the help contract, not
an implementation note. Command rows continue to come from the slash registry,
so aliases, usage, summaries, and future commands cannot drift from dispatch.

Add `/keys` as a read-only `result` command. Its help is intentionally limited
to Toolang-owned interactive Chat bindings; ordinary cursor and text-editing
keys supplied by the terminal are outside this contract:

```text
/keys
These shortcuts control interactive Chat.
Standard cursor and text-editing keys are not listed.

Available shortcuts:
  Enter                    Submit input
  Alt-Enter, Ctrl-J        Insert a newline
  Shift-Enter              Insert a newline when supported by the terminal
  Up, Ctrl-P               Previous history at the first line; otherwise move up
  Down, Ctrl-N             Next history at the last line; otherwise move down
  Esc                      Dismiss a transient status message
  Esc Esc                  Cancel the active run
  Ctrl-C                   Clear input; otherwise cancel a run; press twice to exit
  Ctrl-D                   Exit when input is empty and no run is active
  Ctrl-L                   Clear the display when no run is active
  Ctrl-Q                   Exit Chat immediately
```

Shortcut labels and summaries live beside the binding definitions and supply
the `/keys` rows. The help must not duplicate key sequences in the slash handler.
Conditionally supported bindings remain explicit, as with Shift-Enter, rather
than being presented as universally available.

Scripted Chat projects the same outcomes to plain text. Because it has neither
an editable input box nor a status bar, it also writes unrecognized slash and
colon diagnostics to plain text. It no longer owns a separate subset of slash
commands or separate exception formatting.

## Status Diagnostic Contract

The status bar reports the current interactive condition; it is not a history
surface. Destination is determined by input role and recognition, not outcome
kind:

1. For slash input, Chat looks up the command name before validating the
   remaining source. Bare `/` and names absent from the registry remain in the
   input box and produce a transient status diagnostic. A registered name is a
   submitted command; its `success`, `result`, `usage`, or `error` outcome enters
   scrollback, and Chat clears and records the input.
2. Colon input configures only the pending run; it is not a command surface.
   Parsing, setting validation, resource resolution, or request materialization
   failure leaves the input and cursor unchanged and reports a transient status
   diagnostic. Only a complete colon override plus runnable input can continue
   to queue insertion or run dispatch, after which Chat clears and records it.
3. Ordinary runnable input follows the same run acceptance rule without the
   colon-specific help guidance. Its pre-dispatch failures likewise stay in
   status with editable input.

Submitting an empty buffer is a silent no-op and does not change status, history,
or scrollback. Repeated Enter retries status-rejected input. Editing it clears
the transient diagnostic but does not otherwise change the text.

The existing `/?` alias is a registered slash command and returns slash-command
help in scrollback. Add `:?` as the one special Chat-only colon interaction. It
returns colon help to scrollback, clears and records the input, and does not
create a `RunRequest`:

```text
:?
Run overrides change settings for this run only.
Session defaults stay unchanged.

Put one or more override lines first.
Include the run input in the same submission.

Available overrides:
  :model MODEL
  :model effort=VALUE
  :agic AGIC
  :flow FLOW
  :runnable RUNNABLE
  :allow FIELD=QUERY...
  :limit FIELD=VALUE...
```

The purpose and same-submission constraint must appear before the available
forms so a first-time user can understand why a colon override cannot run by
itself. The override rows come from the shared setting-body specifications used
by slash settings and colon parsing; `:?` must not maintain a second independent
list of parameter names. This keeps future model parameters such as temperature
consistent across `/model`, `:model`, `/?`, and `:?`.

A bare `:` is incomplete input, remains in the input box, and shows
`Enter a run override after : · See :? for help`. A colon override without
runnable input is also rejected but keeps its more specific diagnostic, such as
`Add runnable input after the override · See :? for help`. An unrecognized
override starts with `Unknown run override :NAME · See :? for help`. Other colon
syntax errors may likewise point to `:?`; resolution and runtime-specific errors
keep their more actionable detail instead of appending generic help mechanically.

Beyond rejected input, the status bar is limited to conditions without an
accepted scrollback result of their own:

- keyboard or UI-control guidance, such as the first Ctrl-C of the exit
  confirmation or attempting to clear during an active run;
- failures of asynchronous control requests, such as cancel or steer after the
  action has already been dispatched;
- connection, recovery, and submission-safety state whose validity extends
  beyond one input.

Each status diagnostic has one of two lifetimes. A transient diagnostic clears
on the next prompt edit, recognized slash submission, accepted run, Esc, or
successful retry. A persistent diagnostic represents an unresolved connection
or submission-safety condition. It survives prompt edits, Esc, command
results, and session-setting refreshes, and clears only when the corresponding
recovery state arrives or Chat is restarted. A persistent diagnostic has
precedence over transient diagnostics; transient UI errors must not hide it.
Setting changes still refresh the underlying runnable and model labels while a
diagnostic is visible, so the current labels reappear as soon as it clears.

The status bar renders one physical line. An error uses a dedicated `!` marker
and error style, followed by one concise sentence; it does not add an `Error:`
prefix. Text is sentence-cased, identifies the failed action, and gives the next
step when one is known. It does not expose exception class names, tracebacks,
serialized provider payloads, or internal transport terminology. Provider text
passes through the existing friendly-error sanitizer. Text that exceeds the
available width is elided with a trailing ellipsis rather than wrapped or
allowed to displace the prompt.

Local interaction messages use these forms:

```text
! Enter a command after / · See /? for help
! Enter a run override after : · See :? for help
! Unknown command /missing · See /? for help
! Unknown run override :missing · See :? for help
! Press Ctrl-C again to exit
! Wait for the active run to finish before clearing
! Start a run before steering
! Could not cancel the run: MESSAGE
! Could not steer the run: MESSAGE
! Connection lost · Reconnecting…
! Submissions paused: MESSAGE
```

Rejected-input and local-control messages are transient. Connection loss remains
persistent until recovery and submission-safety failures remain persistent until
an authoritative recovery or restart. The exact remote `MESSAGE` is sanitized,
but the local action prefix and lifetime are stable. Normal status, running
activity, and settings use the existing presentation when no diagnostic is
active.

## Resource Results

Discovery results use compact, copyable identities rather than exposing
provider brackets or internal keys. The later
[resource-table definition](chat-resource-table-presentation.md) owns the final
columns, layout, truncation, and normal model-status presentation:

- models: canonical `provider/model`, input/output price, advertised reasoning
  effort levels, and a trailing `*` on the configured session default;
- tools: canonical `toolset/tool` and concise description, excluding private
  toolsets from this presentation only;
- caps: kind-qualified query identity, scope, form, and concise description.

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
3. If the current model does not match, select the configured default when it
   remains in the ordered result, otherwise select the first result with a fresh
   `ModelRequest`.
4. If the new ceiling contains no models, including `models=none`, the model is
   `None`.
5. The result explicitly reports a fallback selection or an empty collection.
6. An explicit `/model REF` must be within the current `allow.models` ceiling
   or fail without changing the session. `/model default` selects the effective
   default relative to the current ceiling.
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
MODEL                         PRICE ($/1M)      EFFORT
────────────────────────────  ────────────────  ──────────────────
openrouter/openai/gpt-5       $ 1.25 / $10.00   low, medium, high
openrouter/openai/o3          $ 2.00 / $ 8.00   low, medium, high

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
canonical refs, query errors, empty matches, and the presentation metadata
required by the later resource-table definition.

## Scope

Included:

- one slash registry and typed outcome/content contract;
- scrollback rendering for all outcomes of recognized slash commands;
- retained input plus status rendering for bare or unrecognized slash syntax;
- plain-text projection for scripted Chat;
- consistent arity and unknown-command handling;
- confirmations for setting and control commands that keep Chat open;
- model reconciliation after `allow.models` changes;
- `/models`, `/tools`, and `/caps` query commands;
- `/keys` help generated from Toolang-owned Chat binding metadata;
- query-aware effective-resource API and local/remote parity;
- retained input for rejected runnable submissions and a defined status
  diagnostic boundary, lifetime, wording, and one-line presentation;
- bare `/` and `:` guidance plus accepted `/?` and `:?` help results;
- concise first-use help prose plus registry- or setting-spec-generated rows;
- concise Chat, query, and API documentation plus offline tests.

Excluded:

- popup, completion, picker, key-binding, or proactive draft-validation changes;
- query grammar, schemas, set semantics, or collection ordering changes;
- persistent settings outside one Chat session;
- automatic model preference, ranking, or fallback selection;
- eager runnable compatibility checks after allow changes;
- new model parameters or changes to `RunRequest`, run persistence, or
  execution semantics.

## Design Touchpoints

- `src/toolang/cli/toolang/commands/chat/input.py`: structural slash parsing
  without a duplicated command allowlist, plus the Chat-only `:?` help input.
- `src/toolang/cli/toolang/commands/chat/slashes.py`: registry metadata,
  outcomes, command dispatch, confirmations, discovery and `/keys` commands,
  and the `/?` purpose-and-constraint introduction.
- `src/toolang/cli/toolang/commands/chat/shortcuts.py`: presentation-neutral
  metadata for Toolang-owned key sequences, labels, contextual summaries, and
  conditional availability.
- `src/toolang/cli/toolang/commands/chat/base.py` and `policy.py`: client
  resource contract, atomic session/model reconciliation, shared setting-body
  help forms, and the `:?` purpose-and-constraint introduction.
- `src/toolang/cli/toolang/commands/chat/{local,remote}.py`: snapshot-owned
  resource queries and transport parity.
- `src/toolang/cli/toolang/commands/chat/tables.py` and `blocks.py`: structured,
  width-aware table layout and Rich outcome projection.
- `src/toolang/cli/toolang/commands/chat/{tui,main}.py`: centralized interactive
  and plain-text outcome projection; remove the scripted command fork; clear and
  record recognized slash commands or accepted runs; route unrecognized slash
  syntax and rejected run input to status without losing the editable source.
- `src/toolang/cli/toolang/commands/chat/widgets.py`: transient and persistent
  status precedence and the single-line error marker and elision behavior;
  binding handlers consume the shared shortcut metadata.
- `src/toolang/api/routers/{agent,caps}.py` and API schemas: query-aware model,
  tool, and combined-cap projections.
- Existing model, tool, cap collection definitions remain the sole query
  owners.
- Chat input, query, caps, model, and API documentation describe the added
  commands and unchanged plural/singular boundary.

## Acceptance Tests

1. Every registered slash command appends its `success`, `result`, `usage`, or
   `error` outcome to scrollback, clears the input, and adds it to history.
   Later input does not erase the outcome, and success and result summaries do
   not print generic kind labels.
2. Bare `/` and names absent from the slash registry render an actionable status
   diagnostic and retain the exact input and cursor. Once a command name is
   recognized, body parsing and handler validation failures render in scrollback;
   `/model`, `/agic`, `/flow`, and every other missing required body use the
   registered usage text there.
3. `/?` explains immediate execution, session-default lifetime, standalone
   submission, and the `:?` alternative before registry-generated rows. It
   includes `/models`, `/tools`, `/caps`, and `/keys`; adding a test command
   requires no parser allowlist change. It does not repeat a `Chat commands`
   title before the purpose text.
4. `/keys` explains that it covers interactive Chat rather than general terminal
   editing, and lists every Toolang-owned binding from the same metadata used to
   bind it. Contextual behavior and conditional Shift-Enter support are explicit,
   without a separate `Chat shortcuts` title.
5. Every setting command returns `success` with its normalized affected value.
   Queue mutation and steer acceptance also return `success`; help, queue
   inspection, discovery, and show return `result`; failures use the same
   outcome dispatcher; `/exit` remains silent.
6. Narrowing `allow.models` preserves an allowed `ModelRequest`, clears an
   excluded model and effort, reports effective counts for every changed allow
   field, and reports the clearing. Invalid queries leave the complete prior
   setting unchanged.
7. Explicit model selection outside the current model ceiling fails atomically;
   allowed identity changes and effort-only changes retain existing reasoning
   support checks. `/model none` and `/model unset` are invalid session forms;
   `:model unset` is the model-free one-run form.
8. `/models`, `/tools`, and `/caps` list all effective base resources with no
   query, apply valid advanced queries through the owning definitions, preserve
   authored match-group order for models, show copyable identities and `Found N
   ...` summaries according
   to the later resource-table definition, and treat zero matches as a
   successful `No ... found` result.
9. Combined cap queries support qualified and unqualified identities and common
   predicates without introducing a `caps` schema.
10. Remote API query parameters and the new tool endpoint match local results,
   sanitize invalid-query failures, and preserve existing no-query model and
   cap consumers.
11. Scripted and TUI Chat dispatch the same command registry and produce
    semantically equivalent success, result, usage, and error text despite their
    different interactive presentation surfaces.
12. Existing run submission, queue snapshot, popup-free input, execution, and
    persistence behavior remains unchanged outside the defined acceptance and
    status boundary; ruff, formatting, ty, and the default offline pytest suite
    pass.
13. Bare `/`, bare `:`, unknown slash names, and unknown colon override names
    remain editable and show actionable status hints; `/?` and `:?` are help
    results in scrollback; an empty buffer remains a silent no-op.
14. Colon or ordinary runnable-input parse, validation, resolution, and
    materialization failures render in status, preserve input and cursor state,
    and are not added to history or scrollback. Valid run input retains the
    existing queue and live-run presentation.
15. `:?` explains one-run lifetime, unchanged session defaults, leading-line
    placement, and the same-submission input requirement before listing
    override forms from shared setting-body specifications, without a separate
    `Run overrides` title.
16. Transient status diagnostics clear on edit, recognized slash submission,
    accepted run, Esc, or retry; persistent connection and submission-safety
    diagnostics survive those actions and setting refreshes until recovery or
    restart. Persistent state cannot be hidden by a transient diagnostic.
17. Status errors use the `!` marker, stable action-oriented messages, sanitized
    external details, and one-line elision at narrow terminal widths.

## Risks

- An empty effective model collection leaves an agic session without a model.
  The status and scrollback result make that state explicit.
- Effective resource snapshots may refresh between discovery and submission.
  Run materialization remains authoritative and reports any later change.
- Resource tables can be wide. Their Chat projections stay intentionally
  compact and do not duplicate full standalone CLI inventory tables.
- Input handling must distinguish registry recognition from run acceptance: a
  recognized slash clears even after `usage` or `error`, while a failed run
  override remains editable. Tests must cover both paths so neither outcome can
  accidentally erase or retain the wrong input.

## Open Questions

None.
