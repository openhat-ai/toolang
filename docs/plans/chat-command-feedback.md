# Chat Command Feedback and Resource Discovery

## Status

Approved on 2026-09-01. Definition issue: #431. Implemented by #434.

The resource-table and normal model-status presentation was subsequently
refined by
[Concise Chat Resource Tables and Model Status](chat-resource-table-presentation.md),
implemented by #441. That later definition is authoritative where its table
or normal-status presentation differs from this plan. #443 subsequently
corrected reasoning-effort delivery without changing the command outcome,
outcome and session-coherence contracts defined here. Input routing and
command names follow [Chat Commands and Run Directives](chat-prefix-rules.md).

## Goal

Make recognized Chat commands self-contained scrollback interactions. A
submitted command must return an explicit success, result, usage, or error;
incomplete or unrecognized command syntax and rejected run input must remain
editable with an actionable status diagnostic; session setting changes must
leave the session coherent; and users must be able to query effective models,
tools, and capabilities without leaving Chat.

## Success Criteria

- Every recognized command that keeps Chat open clears the input and
  writes its command plus `success`, `result`, `usage`, or `error` outcome to
  scrollback.
- Malformed directives, unknown colon commands, and run input rejected before
  dispatch or queue insertion keep the input unchanged and reports an actionable status
  diagnostic instead of creating scrollback.
- Missing required arguments show the command's canonical usage, including for
  `:model`, `:agic`, and `:flow`.
- `/?` and `:?` explain their surface's purpose and composition constraint
  before listing copyable command or override forms.
- `/keys` explains the interactive Chat keyboard scope and lists every
  Toolang-owned shortcut with its context-dependent behavior.
- A session model is never retained outside the session's `allow.models`
  ceiling.
- `/models`, `/tools`, and `/caps` list effective resources and accept the
  existing collection-query language.
- Local TUI, remote TUI, and scripted Chat use one command registry and outcome
  contract, with equivalent text semantics.
- Adding a command or a new result content type does not require duplicating
  parsing, error routing, or rendering branches.
- Status-bar errors are reserved for incomplete or unrecognized command syntax,
  rejected run input, UI controls, and unresolved asynchronous state.
- Status diagnostics have explicit transient or persistent lifetimes, concise
  actionable wording, and a presentation that does not rely on color alone.

## Command Surface

Use the command set and argument grammar in
[Input Syntax](../input-syntax.md). Slash commands inspect or control Chat;
colon commands query or update session settings. `/models`, `/tools`, and
`/caps` inspect allowed resources, with `-a` selecting all available resources.
Their optional query uses the existing collection-query grammar. `/keys`
shows keyboard help. Resource discovery does not mutate settings or submit a run.

The canonical allow field is plural: `:allow models=openrouter/*` updates the
session's model ceiling. Singular `model` denotes a binding rather than a
collection query.

## Unified Command Outcome

Command recognition and metadata live in one registry. Input parsing
uses the shared reserved names to distinguish commands from run directives.
Command-specific arity belongs to the command registry. The registry owns aliases,
summary, usage, and handler. It therefore becomes the sole source for `/help`,
unknown-command handling, and usage diagnostics.

Handlers return one semantic `CommandOutcome`:

```text
CommandOutcome
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
been recognized. Registry recognition is the presentation boundary: an
incomplete marker or unknown colon command produces an editable diagnostic;
unreserved slash names enter runnable resolution. Once a registered command is
identified, every
`CommandOutcome` is a completed command submission: `success`, `result`, `usage`,
and `error` all enter scrollback and clear the input. Outcome kind describes
what the command returned; it does not decide whether the command was submitted.
Handlers do not write status directly, so scripted Chat can project the same
outcomes as plain text.

Commands that perform a state-changing or control action return `success` when
Chat remains open. For example, setting changes confirm the normalized value,
control actions confirm the accepted change. Help, resource discovery, and `/show` return
`result`. `/exit` is the only silent success because it closes Chat before
another scrollback interaction is useful.

## Success, Result, Usage, and Error Presentation

For a recognized command, the TUI appends one immutable scrollback interaction
containing the command and its outcome, then clears the input. The command keeps
the existing command accent. The body starts with a concise line that
describes the concrete effect, result, usage, or failure. Successful and
read-only outcomes do not print generic `Success:` or `Result:` prefixes:

```text
:model
Usage: :model [MODEL] [effort=VALUE]

:model missing/model
Error: Model selection is unknown: missing/model

:model openai/gpt-5 effort=high
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
returns `error`. Both are completed outcomes for a recognized command and
therefore enter scrollback. This rule applies to every registered command, not
only the three initially reported cases. In particular:

```text
:model [MODEL] [effort=VALUE]
:agic AGIC
:flow FLOW
:runnable RUNNABLE
:allow FIELD=QUERY...
:limit FIELD=VALUE...
```

An empty resource query is valid and lists all items. A valid query with no
matches is a read-only result such as `No models found`; an invalid query is an
error.

A bare `/` is incomplete run-directive input and remains editable. Unknown
colon commands also remain editable; unreserved slash names are root runnable
calls. The [status diagnostic contract](#status-diagnostic-contract) defines
these boundaries and current messages.

`/help` and `/?` explain standalone commands and run directives; `:?` shows
session settings. Command rows come from the registry, so aliases, usage,
summaries, and future commands stay aligned with dispatch.

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
the `/keys` rows. The help must not duplicate key sequences in the command handler.
Conditionally supported bindings remain explicit, as with Shift-Enter, rather
than being presented as universally available.

Scripted Chat projects the same outcomes to plain text. Because it has neither
an editable input box nor a status bar, it also writes rejected-run and
unknown-command diagnostics to plain text. It no longer owns a separate subset of
commands or separate exception formatting.

## Status Diagnostic Contract

The status bar reports editable rejected input and asynchronous conditions.
Command-versus-directive dispatch follows [Input Syntax](../input-syntax.md):

1. A recognized command is a completed interaction. Its success, result, usage,
   or error enters scrollback, and Chat clears and records the submission.
2. Unknown colon commands and rejected run submissions remain editable and
   report a transient diagnostic. Slash names outside the reserved built-ins
   are root runnable calls, resolved through State.
3. A run submission clears only after run dispatch or queue insertion succeeds.
   An empty submission is a silent no-op.

`/help` and `/?` explain commands and run directives. `:?` shows session settings.
Required command bodies have focused help. Diagnostics use the same terminology:

```text
! Enter a directive name immediately after / · See /help for help
! Include primary or named input after run overrides · See /help for help
! Enter a command after : · See :? for help
! Unknown command :missing · See :? for help
! Press Ctrl-C again to exit
! Start a run before steering
! Could not cancel the run: MESSAGE
! Could not steer the run: MESSAGE
! Connection lost · Reconnecting…
! Submissions paused: MESSAGE
```

Transient diagnostics clear on editing, Esc, a recognized command, or an accepted
run. Connection and submission-safety diagnostics persist until authoritative
recovery or restart and take precedence over transient errors. Setting changes
refresh the underlying labels without dismissing persistent diagnostics.

The status bar uses one physical line, an error marker, and a concise sentence.
Sanitize provider text and elide overflow; do not display exception classes,
tracebacks, serialized provider payloads, or internal transport details.
Asynchronous cancel/steer failures and connection state belong here because they
have no completed command outcome of their own.

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

Applying one colon command remains atomic: parse the update, materialize a
candidate `SessionSetting`, reconcile selected identities, then replace the
current setting only if the operation succeeds.

`SessionSetting` has one selected resource identity: `model`. Runnable is a
route identity rather than an allow collection member, and tools and caps have
no selected session field. Model reconciliation therefore follows these
rules:

1. If `allow.models` is unchanged, the existing model behavior is unchanged.
2. If `:allow` changes `models` and the current model still matches the new
   ceiling, preserve the complete `ModelRequest`, including explicit effort.
3. If the current model does not match, select the configured default when it
   remains in the ordered result, otherwise select the first result with a fresh
   `ModelRequest`.
4. If the new ceiling contains no models, including `models=none`, the model is
   `None`.
5. The result explicitly reports a fallback selection or an empty collection.
6. An explicit `:model REF` must be within the current `allow.models` ceiling
   or fail without changing the session. `:model default` selects the effective
   default relative to the current ceiling.
7. Parameter-only `:model effort=...` retains the allowed model identity and
   continues to use the existing model-support validation. A cleared model
   cannot accept model parameters.

A successful `:allow` reports the effective count for every field changed by
that command, rather than echoing the authored query. For example, one command
may report `Allowed 2 models, 5 tools`; `models=none` reports `Allowed 0 models`.
These counts describe the resulting session ceiling and use the same effective
resource snapshot as reconciliation.

Example:

```text
:allow models=openrouter/*
Allowed 2 models
Model cleared: deepseek/abc is outside allow.models

/models openrouter/*[reasoning]
Found 2 models
MODEL                         PRICE ($/1M)      EFFORT
────────────────────────────  ────────────────  ──────────────────
openrouter/openai/gpt-5       $ 1.25 / $10.00   low, medium, high
openrouter/openai/o3          $ 2.00 / $ 8.00   low, medium, high

:model openrouter/openai/gpt-5 effort=high
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

- one command registry and typed outcome/content contract;
- scrollback rendering for all outcomes of recognized commands;
- retained input plus status rendering for malformed directives or unknown
  colon commands;
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

- `src/toolang/cli/toolang/commands/chat/input.py`: command and run-submission classification
  without a duplicated command allowlist, plus the Chat-only `:?` help input.
- `src/toolang/cli/toolang/commands/chat/commands.py`: registry metadata,
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
  record recognized commands or accepted runs; route rejected run
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

1. Every recognized command produces the appropriate structured outcome, with
   equivalent local, remote, and line-Chat text.
2. Missing required bodies show focused usage. Invalid bodies leave session
   settings unchanged and produce a command error.
3. Rejected run submissions remain editable; accepted runs and commands clear
   and record the input. Command/directive boundaries match the prefix plan.
4. Transient and persistent diagnostics retain their distinct lifetimes.
5. Resource queries use the published collection grammar and current allowed/all
   scope; command handlers do not implement their own query parser.
6. Model-ceiling changes reconcile the selected model atomically and refresh
   the session status. Queued run snapshots remain unchanged.
7. Existing rendering, request, reconnect, and queue checks pass.

## Risks

- An empty effective model collection leaves an agic session without a model.
  The status and scrollback result make that state explicit.
- Effective resource snapshots may refresh between discovery and submission.
  Run materialization remains authoritative and reports any later change.
- Resource tables can be wide. Their Chat projections stay intentionally
  compact and do not duplicate full standalone CLI inventory tables.
- Input handling must distinguish registry recognition from run acceptance: a
  recognized command clears even after `usage` or `error`, while a failed run
  override remains editable. Tests must cover both paths so neither outcome can
  accidentally erase or retain the wrong input.

## Open Questions

None.
