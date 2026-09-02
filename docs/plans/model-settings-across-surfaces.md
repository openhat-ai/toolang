# Define Model Settings Across Surfaces

## Status

Approved for implementation on 2026-09-02.

## Work Type

Feature definition. This document defines behavior and does not implement it.

## Goal

Use one typed model-setting vocabulary across configuration, environment,
startup CLI, invocation CLI, Chat slash settings, and input-local colon
overrides. Reasoning effort is the first implemented parameter; adding a future
parameter such as temperature must not require a new syntax or parser for each
surface.

The canonical one-shot invocation is:

```sh
too examples/deep_search.too research \
  --model 'deepseek/deepseek-v4-flash effort=high' \
  -- hello kitty
```

## Verified Current Behavior

- `ModelRequest` already carries closed, typed `ModelParameters`; the current
  schema contains `reasoning.effort` and `reasoning.budget_tokens`.
- `RunDefaults.model` and `RunBindings.model` are strings. `[default].model`,
  `TOOLANG_DEFAULT_MODEL`, and agent startup `--default model=...` therefore
  carry only an identity and lose request parameters.
- `/model` and `:model` share `ModelBody`, but its parser is currently owned by
  execution policy and recognizes only the convenience field `effort`.
- Script, Chat, and rerun expose `--default FIELD=VALUE`; `--default
  effort=high` fails because effort is not a default field.
- Local Script feeds CLI `--allow`, `--default`, and `--limit` into Setup
  construction. Remote Script lowers the same flags above the remote Setup as
  session policy, so equal commands can have different policy ownership.
- Prepared runs already persist the effective `ModelRequest`. Retry preserves
  it, and rerun can carry a replacement request.
- The approved run-input settings contract reserves direct assignments such as
  `temperature=0.2`, but configuration, environment, and CLI have no matching
  extension contract.

## Success Criteria

- Every canonical human-authored model setting lowers through one shared
  `ModelBody` parser and one typed application rule. Optional slash or colon
  shortcuts normalize to that canonical form first.
- Agent Setup publishes a complete default `ModelRequest`, not a ref plus
  parameters stored elsewhere.
- Configured parameters reach local and remote Chat and Script unchanged and
  are validated against the effective model before publication or acceptance.
- Run-capable CLI policy is an invocation/session layer above Setup and below
  colon overrides, independent of whether execution is local or remote.
- A future direct parameter is enabled on every human surface by adding one
  typed model field and one shared parameter definition; arbitrary provider
  options remain impossible.
- Existing identity-only configuration, environment values, startup commands,
  and run-surface `--default model=...` calls have defined compatibility.
- `RunBindings` remains identity-only, while `RunRequest` and prepared-run
  persistence retain their existing `ModelRequest` shape.

## Scope

Included:

- root and agent default-model configuration;
- `TOOLANG_DEFAULT_MODEL` and agent startup `--default model=...`;
- Script and Chat invocation settings plus rerun model replacement;
- slash and colon model bodies;
- local/remote Script ownership of invocation allow and limit settings;
- the typed parameter extension mechanism, with effort as its only current
  implementation;
- default APIs, help, diagnostics, compatibility, documentation, and tests.

Excluded:

- implementing temperature or another new parameter in this change;
- accepting arbitrary, adapter-specific, or untyped parameter maps;
- changing provider defaults, model catalogs, adapters, accounting, or model
  metadata except where default projection must carry a `ModelRequest`;
- adding invocation flags to scheduled tasks or chores; their authored bodies
  continue to use colon overrides;
- changing retry's model request.

## Relationship To Existing Definitions

This definition extends the approved run-input settings contract: `/model` and
`:model` keep their existing lifetime and body, while their parser becomes the
shared parser for non-input sources. It amends the identity-only Setup default
contract by changing only `RunDefaults.model` to a complete request. It
preserves the existing rule that future temperature belongs directly under
`ModelParameters`, not under reasoning and not in provider options.

It also refines that definition's no-alias rule: `/model MODEL_BODY` and
`:model MODEL_BODY` are the canonical forms, while a frontend may have explicit
shortcuts that normalize to them. This change does not introduce a particular
shortcut.

## Concepts

There are two distinct model values:

```text
ModelOverride = sparse identity and typed parameter operations from one source
ModelRequest  = concrete exact ref plus materialized ModelParameters
```

`ModelOverride` remains part of `RunOverride`; it is not an arbitrary string
map. Missing fields, `default`, `unset`, and per-parameter `auto` are typed
operations. `ModelRequest` contains none of those operations.

`RunDefaults.model` changes from `str | None` to `ModelRequest | None`.
`RunBindings.model` remains `str | None` because it records the effective bound
identity, while the complete request is already carried and persisted
separately.

## Shared Model Body

Every canonical string surface uses:

```text
ModelBody = MODEL_IDENTITY? MODEL_PARAMETER_ASSIGNMENT*
MODEL_PARAMETER_ASSIGNMENT = PARAMETER=VALUE
```

The identity must be first. Assignments use POSIX token quoting and escaping.
The body requires an identity or at least one assignment. Unknown and duplicate
parameters fail atomically.

Current examples:

```text
deepseek/deepseek-v4-flash
effort=high
effort=4096
effort=auto
deepseek/deepseek-v4-flash effort=high
default
unset
```

Human-facing parameter names are flat and stable. Canonical typed storage may
be nested:

| Human name | Current value grammar | Canonical `ModelParameters` path |
| --- | --- | --- |
| `effort` | `auto`, unsigned integer, or reasoning level | `reasoning.budget_tokens` or `reasoning.effort` |
| `temperature` | reserved; future finite numeric value or `auto` | `temperature` |

`reasoning.effort`, `reasoning.budget_tokens`, `parameters`, and dotted paths
are not accepted on human surfaces. Direct API requests continue to use the
canonical nested shape.

## Setting Sources And Syntax

The same semantic setting has the following encodings:

| Source | Canonical example |
| --- | --- |
| TOML | `[default]` with `model = "deepseek/deepseek-v4-flash effort=high"` |
| Environment | `TOOLANG_DEFAULT_MODEL='deepseek/deepseek-v4-flash effort=high'` |
| Agent startup CLI | `--default model='deepseek/deepseek-v4-flash effort=high'` |
| Script/Chat invocation CLI | `--model 'deepseek/deepseek-v4-flash effort=high'` |
| Chat session | `/model deepseek/deepseek-v4-flash effort=high` |
| One run | `:model deepseek/deepseek-v4-flash effort=high` |
| Direct API | `model.ref` plus canonical `model.parameters` |

Shell quotes group a multi-token `ModelBody`; they are not part of its value.

All setting groups follow the same ownership matrix:

| Setting | Config | Environment / agent startup | Invocation CLI | Chat session | One run |
| --- | --- | --- | --- | --- | --- |
| model | `[default] model = "BODY"` | `TOOLANG_DEFAULT_MODEL` / `--default model=BODY` | `--model BODY` | `/model BODY` | `:model BODY` |
| runnable | `[default].runnable` | `TOOLANG_DEFAULT_RUNNABLE` / `--default runnable=REF` | Chat `--runnable REF` | `/runnable BODY` | `:runnable BODY` |
| allow | `[allow]` | `TOOLANG_ALLOW_*` / startup `--allow` | run-surface `--allow` | `/allow BODY` | `:allow BODY` |
| limit | `[limit]` | `TOOLANG_LIMIT_*` / startup `--limit` | run-surface `--limit` | `/limit BODY` | `:limit BODY` |

Config, environment, and options on agent `run`, `start`, or `serve` build the
frozen Setup baseline. Options on Script, Chat, or rerun build an invocation
session above that baseline. Slash settings mutate only the current Chat
session; colon settings affect only the submitted run. An option name such as
`--allow` therefore has one policy meaning for each command category and never
changes meaning based on local versus remote execution.

### Configuration

Configuration uses the same complete `ModelBody` string as every other human
surface:

```toml
[default]
runnable = "agic:chat"
model = "deepseek/deepseek-v4-flash effort=high"
```

An agent-level value may omit the identity when its parameters modify an
inherited root default:

```toml
[default]
model = "effort=high"
```

A parameter-only body with no effective model is invalid. Unknown or duplicate
parameters fail atomically. A structured `[default.model]` table is not
supported; configuration intentionally has no second model-setting syntax.

Root config, agent config, environment, and agent startup CLI are applied in
that order. Each source produces a sparse `ModelOverride`; Setup publishes only
the final concrete `ModelRequest`.

### Environment And Startup CLI

`TOOLANG_DEFAULT_MODEL` contains one complete `ModelBody`. No
`TOOLANG_DEFAULT_EFFORT`, `TOOLANG_MODEL_TEMPERATURE`, or per-parameter
environment family is introduced.

Agent `run`, `start`, and `serve` retain `--default` because it changes the
frozen Setup publication. Its `model` value becomes a complete `ModelBody`:

```sh
too AGENT run --default model='deepseek/deepseek-v4-flash effort=high'
```

### Invocation CLI

Run-capable surfaces use a first-class model body rather than adding one CLI
option per parameter:

```text
too FILE.too RUNNABLE [--model MODEL_BODY] ...
too AGENT chat [--model MODEL_BODY] [--runnable RUNNABLE] ...
too AGENT rerun RUN [--model MODEL_BODY] ...
```

`--model effort=high` modifies the effective default identity. A multi-token
body must be shell-quoted. There is no separate `--effort` flag; otherwise each
future model parameter would require another surface-specific option.

Script and Chat also retain `--allow` and `--limit`. These are invocation
session values for both local and remote execution. Local Script stops feeding
them or `--model` into Setup construction. CLI allow can only narrow resources
published by Setup; CLI limits overlay Setup limits.

Chat applies its invocation model, runnable, allow, and limits atomically to the
initial `SessionSetting`. Later slash commands update that mutable session.

Rerun uses the persisted source `ModelRequest` as its current value. A
parameter-only body modifies that request without reconstructing it from the
string binding. An explicit identity replaces it. `default` selects the current
Setup default. Rerun rejects `unset` because the existing request has no explicit
remove-model state; omission already preserves a model-free source. Retry
continues to expose no model replacement.

### Slash And Colon

The canonical forms are:

```text
/model MODEL_BODY    # subsequent runs in this Chat session
:model MODEL_BODY    # runnable input in this submission only
```

Both call the same parser as environment and CLI strings. Only their lifetime
differs.

Slash and colon frontends may register shortcuts. A shortcut is an input-only
rewrite to the complete canonical command before `ModelBody` parsing; it cannot
add parameter semantics or bypass validation. Help, documentation,
completions, diagnostics, and any generated command text use `/model` or
`:model` with named assignments such as `effort=high`. Shortcuts are not
accepted by config, environment, or CLI model options. Adding or removing any
specific shortcut remains a separate UX decision.

## Layering And Reset Semantics

The complete order is:

```text
root config
  < agent config
    < process environment
      < agent startup CLI
        < invocation CLI / initial Chat session
          < later slash session updates
            < input-local colon override
```

For one model body:

- no identity retains the current identity and every unmentioned parameter;
- an exact identity is a selection boundary: it clears inherited explicit
  parameters, then applies assignments in the same body;
- `default` restores the captured lower-layer model request, then applies
  assignments in the same body;
- `unset` clears the Setup preference; at session or one-run layers it selects
  no model and cannot combine with parameter assignments;
- `PARAMETER=auto` clears only that explicit parameter, revealing model or
  provider behavior;
- missing is never the same as `auto`.

At Setup sources, the captured lower layer is the result before that config,
environment, or startup input. In Chat and one-run input, `default` restores
the immutable Setup/surface request rather than a previous mutable session
value. Chat and Script materialize the first effective collection model when
Setup has no configured preference. A parameter operation without an effective
model is invalid.

Allow and limit use the same lifetime order but retain their existing typed
semantics: frozen config/environment/startup allow may replace Setup
publication policy; invocation, slash-session, and colon-run allow are
intersecting ceilings. Each higher-layer limit field replaces the lower value.

## Future Parameter Contract

Model parameters are a closed, base-owned set. The shared input implementation
owns one static definition per human-facing parameter with:

- its public assignment name;
- its accepted `ModelBody` text grammar;
- its typed `ModelOverride` operation and canonical `ModelParameters` field;
- `auto` clearing behavior.

Model resolution remains the owner of capability validation and application to
`ModelTarget`; adapters continue to own provider-wire translation. These
layers consume typed parameters and never parse human input.

Adding temperature later therefore requires one coordinated typed change:

```text
ModelParameters.temperature
ModelOverride temperature operation
shared input parameter definition
catalog capability validation
adapter application
```

After that change, every form becomes available together:

```toml
[default]
model = "openai/example temperature=0.2"
```

```sh
TOOLANG_DEFAULT_MODEL='openai/example temperature=0.2'
too AGENT run --default model='openai/example temperature=0.2'
too FILE.too RUNNABLE --model 'openai/example temperature=0.2' -- input
```

```text
/model openai/example temperature=0.2
:model openai/example temperature=0.2
```

Until the typed temperature change is approved and implemented, every surface
rejects `temperature` as unknown. There is no generic JSON option, arbitrary
`--param`, adapter passthrough, or plugin-extensible request-parameter map.

## API And Persistence

`GET /api/v1/runs/defaults` returns `model` as `ModelRequest | null`, using the
same object shape as `RunRequest.model`. Remote Chat and Script therefore
receive configured parameters instead of reconstructing a request from a ref.
This is an intentional protocol change and exact-key/version checks fail
visibly with an older peer.

Model-list responses keep `default` as a ref string because it marks one list
row rather than transporting a setting. `RunRequest`, prepared-run
`model_request`, retry persistence, model-call records, and accounting schemas
do not change.

Setup validates its materialized default request against the effective model
collection and parameter metadata before publishing it. A failed refresh keeps
the last valid Setup. Run acceptance remains authoritative for invocation and
input-local settings.

## Compatibility

- Existing `[default] model = "REF"`, `TOOLANG_DEFAULT_MODEL=REF`, and startup
  `--default model=REF` remain valid because an identity is a valid
  `ModelBody`.
- Legacy `none` in configuration, environment, and `--default model=...`
  normalizes to canonical `unset`; slash, colon, and first-class `--model`
  accept only `unset`.
- Script, Chat, and rerun keep hidden `--default model=VALUE` compatibility,
  normalize it to `--model VALUE`, and emit one deprecation warning.
- Chat similarly maps hidden `--default runnable=VALUE` to `--runnable VALUE`.
- Combining a compatibility field with its canonical option is an error.
- Removing run-surface compatibility inputs requires another approved change;
  agent startup `--default` is not deprecated.
- The run-defaults HTTP projection changes atomically with local and remote
  clients; no dual response shape is retained.

## Design Touchpoints

- `src/toolang/base/types/model.py`: typed parameter values and the shared,
  closed `ModelOverride` vocabulary currently consumed by `RunOverride`.
- A base-owned shared model-setting parser/application module consumable by
  Setup and execution without reversing package dependencies.
- `src/toolang/base/types/policy.py`: `RunDefaults.model` becomes a
  `ModelRequest | None`; `RunBindings` stays unchanged.
- `src/toolang/setup/config.py` and watcher validation: string model bodies,
  layered overrides, and default parameter validation.
- `src/toolang/cli/common/policy.py`: parse environment, startup defaults, and
  invocation compatibility at the CLI boundary.
- `src/toolang/execution/policy.py`: reuse the base-owned model body instead of
  owning an effort-only parser; receive canonical command names after any
  frontend shortcut normalization.
- Script, Chat, and thread command modules for canonical invocation options,
  concrete session materialization, and local/remote parity.
- run-default and model-list API projection plus remote client decoding.
- configuration, CLI, execution, local/remote, API, help, and integration tests;
  model, input-syntax, configuration, and execution documentation.

Raw CLI and environment strings are resolved at their owning call sites. Core
execution receives only `ModelOverride`, `ModelRequest`, or concrete session
values.

## Acceptance Tests

1. Equivalent config, environment, startup CLI, invocation CLI, slash, and
   colon model bodies produce the same typed `ModelRequest`.
2. Root, agent, environment, startup, invocation, slash, and colon layers obey
   identity-boundary, parameter-only, `default`, `unset`, `auto`, and missing
   semantics without mutating lower values.
3. Setup publishes configured reasoning, rejects unsupported effort or budget,
   and retains its last valid publication after an invalid refresh.
4. Local and remote Script submit equivalent model requests, allow ceilings,
   and limits. Invocation allow cannot re-add resources excluded by Setup.
5. Chat initializes all invocation settings atomically, carries configured
   parameters through `/runs/defaults`, validates explicit settings before
   creating a thread, and preserves slash/colon lifetime behavior.
6. Rerun preserves its source request by default and supports parameter-only,
   identity, `auto`, and `default` replacement; retry preserves the complete
   persisted request.
7. Unknown parameters, dotted names, duplicate assignments, invalid values,
   parameter-plus-`unset`, and parameter-without-model fail on every surface
   with consistent diagnostics.
8. Parameter parsing is closed and shared: no human surface contains its own
   effort parser, and the reserved temperature forms remain rejected until a
   typed temperature parameter exists.
9. Slash and colon help, completion, diagnostics, and generated examples use
   the canonical `/model` and `:model` spellings. Any registered shortcut
   normalizes to an equivalent canonical command before body parsing.
10. Identity-only inputs and hidden run-surface compatibility behave as
   documented. Agent startup help retains `--default`; canonical Script, Chat,
   and rerun help shows `--model` and Chat `--runnable`.
11. Direct run requests and prepared records retain the existing canonical
    model request shape; model-list defaults remain ref strings.
12. `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`,
    and `uv run pytest` pass.

## Risks

- Changing `RunDefaults.model` and `/runs/defaults` affects many identity-only
  call sites. Keeping `RunBindings` and prepared persistence unchanged limits
  the migration boundary.
- Extending the existing configuration string makes spaces meaningful where
  they previously caused an invalid ref. TOML quoting and `ModelBody` quoting
  remain separate layers and need clear examples.
- Reclassifying local Script CLI allow as a session ceiling intentionally
  removes its ability to expand configured resources. Expansion belongs to
  config, environment, or agent startup.
- A shared parameter definition can become an untyped registry if it accepts
  arbitrary handlers. It must remain static and backed by closed dataclasses.
- An explicit Chat parameter may require one model-metadata request before the
  TUI opens; early validation is preferred to a delayed first-run failure.

## Open Questions

None.
