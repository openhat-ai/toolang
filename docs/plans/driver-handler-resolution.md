# Driver Handler Resolution

## Status and Goal

Feature definition, recording the human's decisions on 2026-10-16. This PR
contains no implementation; the document remains subject to human review.

Give `chat`, `chore`, and `talk` one executor-owned handler selection contract.
A handler is an ordinary agic or flow used to handle a driver's input, not a
new runnable kind. Runtime supplies a runnable handler for `talk` only.

Success means every driver follows the same selection order, runtime Talk can
execute without a user entry, and selection never hides an error by executing
a different runnable.

This definition supersedes the handler selection order in
[Execution drivers and chores](serve-execution-drivers.md) and earlier global
runnable-default rules. User module
composition continues to follow [Unified Runnable Modules](flat-runnable-modules.md).
Driver enablement and admission gates, triggers, thread lifecycle, messaging
transport, standalone script selection, and nested runnable calls are outside
this change. Model defaults and other resource configuration remain unchanged.

## Resolution Contract

The executor receives a concrete driver name, an optional invocation-selected
runnable, and captured Setup/State. It selects the first available candidate:

| Priority | Candidate |
| --- | --- |
| 1 | The runnable specified by the invocation |
| 2 | A user-provided public runnable with the driver's exact name |
| 3 | A runtime-provided handler with the driver's exact name |
| 4 | The user's unnamed top-level entry |
| 5 | Fail handler resolution |

User handlers may be agics or flows. A valid export from
`runnables/<driver>.too` is a named public handler even when its declaration is
unnamed; it does not participate in priority 4. The unnamed fallback is the
agent entry, retaining the existing [unnamed identity contract](unnamed-runnable-identity.md).

An invocation may select any runnable allowed by the existing reference rules,
including an unnamed entry through `_`. Only priorities 2 and 3 require the
driver's name. An explicitly selected unnamed entry therefore wins over the
runtime Talk handler. Only an omitted selection enters automatic lookup; an
empty or malformed ref is an error. Exhausting the candidates reports the driver
and missing handler before accepting a run.

Keep existing user-source composition unchanged: a public handler declared in
`agent.too` conflicts with the same public export from `runnables/`, including
across runnable kinds. Do not introduce `agent.too` precedence or silent
shadowing. Runtime handlers are a separate fallback source and do not create
user-source composition conflicts.

Fallback applies only when a candidate is absent. An invocation-selected ref
that is missing or invalid fails immediately. Once a handler is selected,
kind mismatch, invalid inputs, unavailable resources, execution failure, or
cancellation cannot select a lower-priority handler. User candidate presence is
determined from the captured accepted State. Invalid authored sources continue
through existing State acceptance and diagnostics; selection neither reparses
excluded sources nor suppresses their diagnostics. A rejected source is not a
candidate.

## Caller and Executor Boundary

Session selection and input overrides are caller behavior. Callers merge their
existing selection inputs into one optional invocation-selected ref; the
executor does not distinguish their origins or add session/input priority tiers.

There is no global default runnable. Remove `[default].runnable` from root and
agent configuration and remove the Setup-owned `RunDefaults.runnable` field.
Reject the removed config key, `TOOLANG_DEFAULT_RUNNABLE`, and runtime startup
`--default runnable=...` with actionable guidance. Do not silently ignore them
or reinterpret them as a Chat selection. Driver-specific entry names and the
resolution contract above replace that configuration; no new driver config
table is introduced.

Chat TUI may retain a default runnable in its session setting. It selects the
Chat entry for that session only; each submission forwards it as the invocation
selection unless the current input overrides it. A one-run override does not
change the session default. Unset or reset session selection returns to automatic
`chat` handler resolution. Existing Chat `--default runnable=...` initializes
this session setting only; it must not configure Setup, another Chat session,
Talk, or Chore. Session state and invocation bindings retain their runnable
fields independently of removing the global configuration field.

Automatic handler selection belongs to execution admission, before handler
input coercion, resource validation, or run acceptance. Callers must not resolve
an automatic default early and then present it as an explicit invocation choice.
Local and remote execution, default displays, and request construction must use
the same execution-owned resolution contract and preserve whether selection was
explicit or automatic. A displayed default is a preview, not an invocation
selection: automatic admission resolves against the captured acceptance State
even if the preview used an older State. Existing input source-revision checks
still apply. Do not infer the driver from thread prefixes or client IDs.

Selection uses the captured user State and runtime handler sources. It does not
refresh watchers, parse session commands, inspect the environment, or read user
source files. Already concrete bindings retain their existing validation.

## Runtime-Provided Talk Handler

Ship the default Talk behavior as a package-owned `.too` Program exporting a
named `talk` agic. Reuse normal language parsing, input validation, resources,
execution, controls, and run recording; do not retain a second Python handler
execution path or generate a runnable in the user's files.

Move the current default message-handling instructions from
`src/toolang/driver/talk.py` into that runnable. Preserve replies through
conversation tools, history lookup, duplicate avoidance, and the final handling
summary. The primary input remains the existing JSON batch, accepted as
`Json`; the driver supplies batch data without prepending behavioral instructions
for any selected handler. Named user handlers retain their existing batch contract.

The runtime handler executes as the receiving agent with its captured Setup,
workspace, resource limits, and effective agent capabilities. Package ownership
does not grant extra tools or models. Keep current model selection and messaging
delivery/cursor semantics. Missing required resources fail normally.

Keep runtime handler source separate from authored user modules and their public
export validation. Capture its source and distinguish its identity from a user
`talk` in normal execution records, so inspection and restart cannot silently
substitute a different source. Reuse existing snapshot and restart mechanisms;
do not add a handler registry plugin system or write built-ins into agent homes.

Runtime supplies no `chat` or `chore` handler. Consequently, with no invocation
selection or user named handler, Talk selects the runtime handler even when an
unnamed user entry exists; Chat and Chore select the unnamed entry or fail.

## Acceptance Tests

Use deterministic offline providers and the existing fake messaging backend.

1. Resolution matrix: cover every applicable tier for all three drivers, with
   both user runnable kinds; absent Chat/Chore handlers and absent unnamed entry
   fail.
2. Explicit selection: invocation refs win over all automatic candidates;
   `_` bypasses runtime Talk. Missing, empty, malformed, or kind-mismatched refs
   fail without another candidate. Equivalent refs from session and input
   selection behave identically.
3. User sources: valid named and unnamed file exports act as public handlers;
   `agent.too`/file public-name conflicts retain current diagnostics and rejection.
   Excluded sources retain their diagnostics and do not reenter selection.
4. Runtime Talk: a batch with no user runnable executes the packaged handler;
   an unnamed user entry does not replace it, while a user named `talk` does.
5. Failure boundaries: invalid inputs, unavailable resources, failed runs, and
   cancellation never dispatch another handler or duplicate message delivery.
6. Input and resources: every Talk handler receives only the batch as input;
   runtime instructions come from its Program, agent ceilings remain effective,
   and custom agic/flow handlers retain their model behavior.
7. Execution integration: local/remote selection agrees for the same driver,
   invocation, and State. Stale previews neither pin automatic selection nor
   bypass input source-revision checks. Recorded runtime source survives
   reload/restart without replacing the user's State.
8. Configuration removal: root/agent `[default].runnable`, the global environment
   value, and runtime startup runnable defaults fail with replacement guidance;
   model defaults remain supported and Setup exposes no runnable default.
9. Chat session isolation: startup and session commands select the Chat entry
   only for that session; input overrides affect one submission, reset restores
   automatic Chat lookup, and other sessions, Talk, and Chore are unaffected.

## Implementation Touchpoints and Verification

- `src/toolang/execution/{runnables,calls,schemas,types}.py` and `executor/`:
  shared selection, admission, concrete bindings, input validation, and source
  capture. Automatic requests carry driver context and an optional selection;
  direct runnable requests retain exact ref validation.
- `src/toolang/base/types/policy.py`, `setup/{config,watcher}.py`,
  `cli/common/policy.py`, and `execution/policy.py`: remove the global runnable
  default, its config/environment/startup inputs, and implicit Setup reads while
  retaining model defaults and session/invocation runnable selections.
- `src/toolang/cli/toolang/commands/{runtime,script}.py` and
  `cli/common/agent_server.py`: remove global runnable overrides from startup
  and Setup construction; Chat startup selection stays local to its session.
- `src/toolang/driver/talk.py`, `driver/chore/scheduler.py`,
  `cli/toolang/commands/chat/`, and `api/routers/{agent,runs}.py`: forward caller
  selections, remove duplicated automatic lookup, and keep defaults consistent.
  Update `api/schemas.py` for the same automatic admission contract.
- `src/toolang/state/{builtin,state,cache}.py` and packaged `.too` assets:
  runtime Program preparation and durable source identity without changing
  user export composition. Keep parsing with its existing language/State owners.
- Existing execution, messaging, chore scheduler, State, Setup, CLI policy, and
  local/remote Chat tests; `docs/{program,work,messaging,input-syntax}.md` for the
  implemented contract.

The implementation PR must run the default lint, format, type, and offline test
checks and generate its user-facing changelog through
`too aid.too update_changelog`. This definition-only PR verifies source claims,
relative links, and `git diff --check`.

## Compatibility, Risks, and Open Questions

Talk will use its runtime handler instead of implicitly running the user's
unnamed entry. To retain custom message handling, declare a named `talk` handler
or provide an invocation-selected runnable. Named user handlers retain their
batch input contract, and user-source collisions keep their current behavior.
For user runnables previously used with the injected default instructions, those
instructions must now be authored in the selected runnable.
Removing the global runnable default is a breaking configuration change. Replace
`[default].runnable` and its global overrides with the relevant named driver
handler; for an interactive Chat-only choice, use a Chat session setting or
session startup option. No authored-file migration is performed, and the old
value is not rewritten into a persistent Chat default.

Main risks are callers accidentally turning automatic defaults into invocation
selections, runtime source ownership changing user resources, and restart losing
the original handler identity. Cover these through the acceptance scenarios.
No open design questions; implementation and merge require human approval.
