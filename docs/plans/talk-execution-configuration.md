# Execution configuration and Talk policy

Approved execution configuration contract. Configuration files use structured
model tables.

## Goal and ownership

Use top-level `allow`, `model`, and `limit` for shared resources and ordinary
execution. Add purpose-specific policy under `talk` and `compact`, with the
same structured model shape. Select policy at an execution entry, independently
of the thread prefix; a thread holds context and a Run captures concrete policy.

| Configuration | Responsibility |
| --- | --- |
| `[allow]` | Published resource availability for every execution purpose |
| `[model]` | Ordinary model selection and request parameters |
| `[limit]` | Ordinary root-tree budget defaults |
| `[talk.model]`, `[talk.limit]` | Overrides for new automatic message-handling roots |
| `[compact.model]` and `[compact]` sizing fields | Compaction policy for every thread kind |

Ordinary roots include Chat, Chore, Script/API entry points and independently
spawned work. Explicit caller requests retain their current authority and
model-free API semantics. Thread kinds `chat_`, `chore_`, `exec_`, `work_`, and
`talk_` do not select policy; a spawn reusing any idle thread is ordinary work.

## Scope

This definition replaces the canonical configuration model syntax, adds Talk
model/limit policy, and resets independent spawn defaults. Preserve current
handler/plugin patterns, protocol and record schemas, resource ceilings,
compaction accounting, and public commands. No per-thread policy, `[run]`
wrapper, `[chore]`, or new environment/CLI options are included.

Contracts: [models](../design/models.md#runtime-configuration),
[drivers](../design/drivers.md), [Setup](../design/setup.md),
[run input](../design/call-input.md#requests-and-overrides), and
[execution](../design/execution.md#run-execution).

## Canonical configuration

Use existing root/agent `config.toml` and captured project layers. Model refs
and budgets below are illustrative; selected models must be available, allowed,
and support the controls. No built-in defaults change.

```toml
[allow]
models = ["gateway/*"]

[model]
ref = "gateway/work"
effort = "high"
max_output = 8192

[limit]
agic_model_calls = 200
time = 600

[talk.model]
ref = "gateway/router"
effort = "low"
max_output = 4096

[talk.limit]
agic_model_calls = 20
time = 120
cost = "0.20"

[compact]
summary = 4096
recent = "30%"
trigger = "80%"

[compact.model]
ref = "gateway/summary"
effort = "low"
max_output = 8192
```

Every model table has the same closed fields:

| Field | TOML values | Typed operation |
| --- | --- | --- |
| `ref` | Exact ref or supported selection sentinel | `ModelOverride.identity` |
| `effort` | Nonempty level string, `"auto"`, or non-negative integer budget | `ModelOverride.effort` |
| `max_output` | Positive integer or `"auto"` | `ModelOverride.max_output` |

Levels and integer budgets retain existing model capability validation. Booleans,
wrong types, multi-token ref strings, unknown fields, and arbitrary provider maps
fail. Temperature remains unsupported. Configuration tables lower directly to
shared typed operations. The canonical API `ModelRequest` shape stays unchanged.

### Model strings at command and environment boundaries

All existing string surfaces share the same field names and typed operations:
`ref`, `effort`, and `max_output`. Add `ref=VALUE` while keeping a bare identity
as an equally supported first-token shorthand, without deprecation or warnings:

```sh
too task.too --model 'ref=gateway/work effort=high max_output=8192'
too task.too --model 'gateway/work effort=high max_output=8192'
```

Named assignments may appear in any order; a bare identity must be first. A
body can contain only parameter assignments to modify the effective model.
Accept at most one identity, whether named or bare; reject duplicate identities
(including mixed forms), duplicate parameters, empty values, and unknown keys
atomically. Bare `default`/`unset` and `ref=default`/`ref=unset` are equivalent
where that surface already supports the selection sentinel.

Apply this shared grammar to `--model`, the model body of `--default model=BODY`,
`--compact-model`, the existing model environment variables, `/model`, and
`:model`. Keep current flag names, surface-specific selection restrictions, and
lifetimes. A multi-token CLI body remains one shell-quoted option value, not
multiple argv values. Formatting generated model settings uses explicit `ref=`;
both accepted spellings remain valid inputs.

## Resolution rules

Resolve ordinary model/limits from root then agent configuration (or project
layers outer-to-inner), followed by existing environment and startup overrides.
Apply each ordinary model operation to the preceding source's concrete request;
parameter-only input without a preceding model fails Setup resolution, as today.
For Talk, apply root then agent Talk settings above the resolved ordinary
baseline. A root Talk field therefore wins over an agent general field or a
general startup override. Explicit caller settings still win where the existing
entry point accepts them.

A missing or empty model table supplies no operation. Omitted fields inherit,
except that an explicit exact `ref`, even unchanged, clears inherited explicit
model parameters before applying its table's assignments. A table containing
only `effort` or `max_output` modifies the inherited request. `"auto"` clears
only that explicit parameter. Ordinary model operations materialize during Setup
resolution. Talk and compact retain ordered typed operations until their
entry-specific baseline is known; a parameter operation without an effective
model fails before admission. Do not flatten these deferred operations with a
generic dictionary merge that loses ref-reset semantics.

Selection sentinels use `ref` and the shared typed rules. In ordinary config,
`ref = "default"` retains that source's lower-layer model. In Talk it restores
the ordinary baseline; in compact configuration it restores the calling root's
model identity with independent parameters. `ref = "unset"` cannot combine with
parameters and is rejected for compact. In ordinary config it clears the
configured preference, retaining each entry's existing fallback/model-free
behavior. In Talk it is an explicit no-model selection without a second
fallback. A model-free Flow remains valid; an agic requires a model at admission.

New spawned work selects the general request, or the first model in its target's
effective resources if no ordinary preference exists. Talk uses this baseline
before applying scoped overrides. Compact starts from the calling root's model
identity with no inherited effort or output control, then applies its own model
tables. Thus a parameter-only compact table customizes the current root model,
and an omitted table preserves today's independent-parameter behavior. Apply
compact config layers in their normal order, followed by the existing compact
environment/startup override; those string overrides retain their exact-ref
requirement. Validate dynamic compact parameters against the actual selected
root/model at compaction preparation, not against an unrelated ordinary default.

Limits retain `agic_model_calls`, `agic_tool_calls`, `tokens`, `cost`, and `time`,
with existing units. Talk fields replace ordinary defaults by presence; missing
fields inherit, zero stays active, and `"none"` disables a field. A partial table
does not reset unrelated limits. These values are defaults, not additional
minimum ceilings.

### Future purpose-specific fields

The layout leaves room for `talk.allow`, `compact.allow`, and `compact.limit`;
they are not implemented or silently accepted in this scope. A future local
allow policy can only narrow the published resources and inherited caller
ceilings. It cannot make a globally unavailable model or tool available.
A future compact limit adds a local operation budget while the enclosing root's
budget remains active. These extensions require their own approved definitions.

For now, compact token/cost/time consumption remains part of its calling root,
including overridden Talk budgets. Its reducer model-call cap uses that root's
`agic_model_calls`. Compact still requires an allowed tool-call-capable model;
its internal history-read operation is not a configurable user toolset.

## Independent work and Chore

Use one reset boundary for Flow `spawn` and `runtime__spawn` from every source:

- Keep the captured parent Setup, State selection, workdir, workspace grants,
  callable authorization, ancestry, and resource ceilings under existing rules.
- Select ordinary model and `setup.limits` from that captured Setup for the new
  root. Do not fetch a newer Setup or copy source model/limit overrides.
- Apply the ordinary model before target resource validation. If inherited
  ceilings exclude it, fail explicitly without widening permissions. Build
  target resources without first validating the parent's discarded model. Flow
  spawn and runtime tool spawn must use the same preparation rule.

The same rule applies when reusing an idle thread, regardless of prefix or its
previous model. Nested `run`, owned asynchronous `run`, and same-Run `exec`
retain current policy and accounting. Preserve spawn lifetime, atomic admission,
and receipt deduplication.

Chore roots use ordinary defaults plus their authored directives, for example:

```text
/model gateway/work effort=high
/limit time=300 cost=0.50

Review the pending changes and summarize the result.
```

Those overrides apply to the chore and nested calls. Independent work returns
to ordinary defaults while retaining permission restrictions. Chat session and
one-submission overrides follow the same boundary; no Chore namespace is added.

## Compatibility, capture, and validation

Read only structured model tables. The old `[default]` namespace and scalar
compact model settings are ignored without migration warnings or dedicated
rejection rules. Remove the general-default `none` alias; model selection uses
`unset` where supported. Existing CLI/environment names and lifetimes remain;
their shared model parser accepts the optional explicit `ref=` spelling.

Capture typed settings in immutable Setup generations and include them in
change detection. Syntax/type/unknown-field errors reject initial preparation
or retain the last valid Setup on refresh, with source-aware diagnostics.
Preserve lazy discovery: availability/capability and final resource failures
surface before admission without silent substitution. Talk pre-admission
failure leaves its cursor unadvanced.

New batches capture the latest Setup. Nested execution, compaction, and spawn
use their captured Setup. Accepted Runs and receipts retain recorded concrete
policy. Before new spawn preparation, retrieve any admission linked to the
source Step and verify its original runnable/input/thread binding against that
recorded Step. A matching replay returns its original handle and policy even
if current defaults or target code differ; a changed source binding fails as a
conflict. Do not revalidate the recorded policy against new defaults or skip
existing source-binding conflict checks. Keep retry/rerun semantics and record
schemas unchanged.

## Implementation touchpoints

- `src/toolang/setup/config.py`, `types.py`, and `watcher.py`: structured model
  parsing, typed Talk/compact policy, source diagnostics,
  projection and generation identity. Ordinary defaults stay a concrete
  `ModelRequest`; Talk/compact capture ordered `ModelOverride` values. Setup
  must stay independent of execution.
- `src/toolang/driver/talk.py` and
  `src/toolang/execution/executor/executor.py`: materialize Talk policy at the
  existing handler/resource preparation boundary.
- `src/toolang/setup/models.py`: materialize structured compact overrides over
  the calling root identity while retaining independent model parameters.
- `src/toolang/base/model_settings.py`: shared named/bare ref parsing and
  formatting; keep string and table inputs on the same closed typed vocabulary.
- `src/toolang/execution/executor/spawn.py`: ordinary defaults for both spawn
  paths before validation, preserving nested helpers and existing receipts.
  Use `store.py` and `executor/tool_runtime.py` as needed for receipt lookup and
  conflict validation before preparing new work.
- Existing Setup, model, messaging, Chore, spawn, resource, and compact tests;
  focused configuration and execution documentation and migration examples.

## Acceptance tests

1. All three structured model tables lower equivalent values to the same typed
   operations; wrong types, unknown fields, and multi-token refs fail clearly.
2. Layering covers ref resets, parameter-only tables, empty/missing fields,
   `default`, `unset`, `auto`, root-Talk versus agent/general-startup precedence,
   and compact startup override order. Ordinary parameter-only input with no
   prior model fails; ordinary unset permits entry fallback while Talk unset
   suppresses fallback.
3. Old configuration has no effect and does not emit migration warnings or
   alter Setup generations. Bare model refs retain support without warnings;
   named/bare forms match across CLI/environment/slash/colon, parameter-only
   bodies remain valid, duplicate identities fail, and shell-quoted bodies
   remain single option values. API/persistence shapes retain their contracts.
4. Model availability/capabilities and handler/caller ceilings reject invalid
   choices before model calls or Talk cursor advancement, without fallback.
5. Both spawn paths from Talk, Chore, Chat, Script, and workers use ordinary
   model/limits for new and reused threads. Nested calls retain source policy;
   accepted Flow/tool receipts retain recorded policy on replay after defaults
   or target code change; mismatched source runnable/input/thread still fails.
6. Worker fallback, model-free Flows, model-requiring agics, and excluded general
   defaults obey target resources without expanding permissions.
7. Compact in each thread kind uses its structured settings and independent
   controls, including parameter-only inheritance against the calling model,
   while sharing root budgets. Deferred metadata failures do not force eager
   model discovery during syntax validation.
8. Talk limit replacement covers every field, zero, `"none"`, and cost parsing.
   Refresh/recovery preserves accepted policy and existing retry/rerun behavior.
9. Before implementation commits run `uv run ruff check .`,
   `uv run ruff format --check .`, `uv run ty check`, and `uv run pytest -n auto`.

## Risks

- Structured tables replace a shipped string surface. Existing configuration
  must migrate to the new tables; old model settings no longer take effect.
- Spawn resets Chat/Chore source overrides as well as Talk overrides; inherited
  resource ceilings can therefore reject a different ordinary worker model.
- Compact parameter-only tables extend its current exact-ref-only configuration;
  lazy validation must preserve independent parameters and root accounting.
