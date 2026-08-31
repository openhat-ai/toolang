# Define Run Settings and Overrides

## Status

Approved for implementation on 2026-08-31.

## Goal

Give Chat one UI-independent session setting and runs one input-local override.
Slash setting commands change defaults for future runs in the current session.
Leading colon commands affect only the runnable input submitted with them. The
resulting effective values are snapshotted into the existing `RunRequest`.

## Conceptual Model

Only three public concepts are needed:

```text
SessionSetting = current defaults for future runs in one Chat session
RunOverride = sparse changes attached to exactly one runnable input
RunRequest  = self-contained materialized request accepted for execution
```

Their input forms have a strict boundary:

| Input form | Result | Lifetime |
| --- | --- | --- |
| slash setting command | update `SessionSetting` | subsequent runs in this Chat session |
| leading colon override | build `RunOverride` | runnable input in the same submission only |
| popup selection | edit the input draft | no setting or run mutation |

A colon prefix without primary or named runnable input is invalid. It never
updates the session. A slash setting command occupies the complete Chat
submission and never becomes a run override.

There is no input-level `PolicyEdit`, `PolicyCommand`, or `SessionSettingEdit`.
Model, runnable, allow, and limit are fields of `SessionSetting` or `RunOverride`,
not policy groups. The existing `RunPolicy` remains only as the materialized
allow-and-limit value inside `RunRequest`; it is not part of authored input.

## Success Criteria

- Colon commands affect one run and slash setting commands affect the session;
  neither form is reclassified according to whether more input happens to be
  present.
- `SessionSetting` contains concrete session defaults and no presentation selector
  state.
- `RunOverride` contains only fields authored for one run.
- Model identity and model parameters can change independently.
- Runnable identity has one canonical value with `agic` and `flow` shorthand.
- Allow and limit accept multiple assignments without collection shortcuts.
- Run acceptance validates model reasoning support and snapshots one unchanged
  `RunRequest` shape.

## Scope

In scope:

- typed `SessionSetting` and aggregate `RunOverride` values;
- model identity and convenient effort input;
- runnable identity and kind-qualified shorthand;
- allow ceilings and run limits;
- colon override and slash setting parsing;
- session updates, request construction, queue snapshots, local/remote parity,
  input documentation, and offline tests.

Out of scope:

- popup, picker, completion ranking/rendering, status-bar, or keyboard design;
- selector-list syntax;
- temperature or another new model parameter;
- model catalog/list/completion metadata, pricing, or provider adapter changes;
- changes to the outer `RunRequest`, persistence, retry, or rerun semantics;
- persistent settings outside the lifetime of one Chat session.

## Values and Ownership

`SessionSetting` is a concrete, immutable session value:

```python
@dataclass(frozen=True, slots=True)
class SessionSetting:
    model: ModelRequest | None
    runnable: str | None
    allow: AgentCeiling
    limits: RunLimits
```

At Chat initialization, the caller resolves configured surface values and uses
them to create the initial `SessionSetting`. Slash commands replace only their
authored fields and return a new value. The UI may project labels or completion
state from it, but UI dictionaries are not accepted by request construction.

`RunOverride` is sparse. It can carry:

```text
- an optional model identity operation and model parameter operations;
- an optional runnable identity operation;
- an optional AgentCeiling for additional allow restrictions;
- authored fields of RunLimits, including an explicit none value.
```

Missing means retain the effective `SessionSetting` value. The required distinctions
between missing, `default`, `none`, and `auto` live inside these typed fields;
they do not require a public generic edit type. Parser-only intermediate values
remain private to the parser.

Immutable surface defaults remain available only for the `default` keyword.
They are not another mutable session layer.

Materialization is one direction:

```text
surface baseline -> SessionSetting
SessionSetting + RunOverride + RunnableInputRaw -> RunRequest
```

Queueing stores the materialized `RunRequest`, so later slash commands cannot
change an already queued request.

## Shared Command Bodies

Colon and slash forms share command bodies but have different destinations:

```text
:model BODY     -> model field of this run's RunOverride
/model BODY     -> model field of the session SessionSetting

:runnable BODY  -> runnable field of this run's RunOverride
/runnable BODY  -> runnable field of the session SessionSetting

:allow BODY     -> additional allow ceiling for this run
/allow BODY     -> session allow defaults

:limit BODY     -> limit fields for this run
/limit BODY     -> session limit defaults
```

`agic` and `flow` are runnable shorthand in both namespaces:

```text
:agic NAME      -> :runnable agic:NAME
:flow NAME      -> :runnable flow:NAME
/agic NAME      -> /runnable agic:NAME
/flow NAME      -> /runnable flow:NAME
```

## Model Body

```text
ModelBody = MODEL_IDENTITY? MODEL_PARAMETER_ASSIGNMENT*
MODEL_IDENTITY = EXACT_REF | default | none
MODEL_PARAMETER_ASSIGNMENT = MODEL_PARAMETER=VALUE
MODEL_PARAMETER = effort
```

The body requires an identity or at least one assignment. The optional token
without `=` is identity and must come first. Tokens containing `=` are model
call parameters. `ref=...`, `reasoning=...`, and `reasoning.effort=...` are
invalid input names.

Session examples:

```text
/model openai/gpt-5
/model effort=high
/model effort=4096
/model effort=auto
/model openai/gpt-5 effort=high
/model default
/model none
```

One-run examples:

```text
:model effort=high

Explain this code.

:model openai/gpt-5 effort=4096

Solve this problem.
```

`effort` is input-only convenience:

```text
effort=auto -> omit explicit reasoning
effort=4096 -> reasoning.budget_tokens = 4096
effort=high -> reasoning.effort = high
```

The parser checks `auto`, then accepts `0|[1-9][0-9]*` as a token budget. Every
other value is a candidate effort level validated after the effective model is
known. Signs, leading zeros, decimals, exponents, and separators are not budget
syntax.

Bare `default` selects the captured surface model. Bare `none` selects no model.
Assigned `effort=default` and `effort=none` are effort levels, not identity
operations, and succeed only when the model advertises them.

A parameter-only command retains model identity and every unmentioned model
parameter. An explicit identity is a selection boundary: it clears unmentioned
explicit parameters, even when it reselects the current ref, then applies
parameters in the same command. `effort=auto` clears only explicit reasoning
and preserves alias or provider defaults.

An effort must be advertised by the selected model. A budget requires an
advertised token-budget control and must satisfy its minimum. A parameter-only
command requires an effective model. Existing Chat list metadata may reject a
known unsupported effort while applying a slash command, but it is not expanded
to describe budget controls. Core model resolution therefore remains
authoritative and validates every explicit value before run acceptance.

The shorthand lowers to the canonical `ModelParameters.reasoning` structure.
Direct request JSON, catalog records, selectors, completion metadata, and
adapters do not accept the flattened spelling. The canonical reasoning value
permits at most one of effort and `budget_tokens`.

A future direct parameter uses the same assignment form:

```text
/model temperature=0.2
/model temperature=auto
:model effort=high temperature=0.2
:model openai/gpt-5 effort=high temperature=0.2
```

These examples reserve the syntax. Temperature remains out of scope until it
exists in typed model parameters.

## Runnable Body

The generic form accepts an exact kind-qualified ref or a uniquely resolvable
unqualified identity:

```text
:runnable agic:abc
:runnable flow:abc
:runnable abc

/runnable agic:abc
/runnable flow:abc
/runnable abc
```

The shorthand is exact:

```text
:runnable agic:abc <=> :agic abc
:runnable flow:abc <=> :flow abc
/runnable agic:abc <=> /agic abc
/runnable flow:abc <=> /flow abc
```

Unqualified `abc` resolves only when it identifies one available runnable. It
fails when missing or ambiguous. `SessionSetting` and `RunRequest` store the exact
kind-qualified ref.

`default` is a reset keyword only in the generic form:

```text
/runnable default

:runnable default

Run once with the surface runnable.
```

Kind-specific forms always treat their argument as a runnable name, so
`:agic default` means `:runnable agic:default`.

Colon runnable commands may also introduce named runnable input:

```text
:agic review focus=security count=2
```

`focus` and `count` belong to `RunnableInputRaw`. Their presence is runnable
input, so the submission is valid even without primary text. Slash runnable
commands update settings and therefore reject named runnable input.

There is no no-runnable keyword because an accepted run must resolve a runnable.

## Allow Body

```text
AllowBody = ALLOW_ASSIGNMENT+
ALLOW_ASSIGNMENT = ALLOW_FIELD=QUERY | ALLOW_FIELD=all | ALLOW_FIELD=none
ALLOW_FIELD = models | tools | psyches | skills | services | prompts
```

Multiple fields may be assigned atomically:

```text
/allow models=openai/* tools=tool:* skills=reviewer

:allow models=openai/* tools=tool:*

Run with an additional resource ceiling.
```

Repeated assignments for the same field in one complete allow body or colon
prefix accumulate queries in authored order and deduplicate exact repeats.
`all` and `none` cannot be combined with another value for the same field.

A slash update replaces each authored session field; absent fields retain their
current values. `/allow models=all` removes the session's model restriction and
`/allow models=none` selects an empty collection.

A colon allow value is an additional `AgentCeiling`. It intersects with the
session ceiling and cannot broaden it. `:allow models=all` means no additional
model restriction for that run; the session restriction still applies.

Collection shortcuts do not exist:

```text
:models QUERY
:tools QUERY
:psyches QUERY
:skills QUERY
:services QUERY
:prompts QUERY
```

Use `:allow FIELD=QUERY` for a run or `/allow FIELD=QUERY` for the session.

## Limit Body

```text
LimitBody = LIMIT_ASSIGNMENT+
LIMIT_ASSIGNMENT = LIMIT_FIELD=VALUE | LIMIT_FIELD=none
LIMIT_FIELD = agic_model_calls | agic_tool_calls | tokens | cost | time
```

Examples:

```text
/limit agic_model_calls=100 tokens=20000 cost=2.5 time=120

:limit tokens=4000 time=30

Run within tighter limits.
```

`agic_model_calls`, `agic_tool_calls`, `tokens`, and `time` require non-negative
integers. `cost` requires a finite non-negative decimal. `none` disables that
field's limit.

Each field may appear at most once in a complete submission. A slash update
replaces authored session fields and retains absent fields. A colon value
replaces the effective session value for that run only.

## Submission and Atomicity

Chat input has two relevant paths:

```text
slash setting command -> validate -> new SessionSetting

colon prefix? + RunnableInputRaw -> RunOverride + RunnableInputRaw
                                 -> RunRequest
```

Plain runnable input has an empty `RunOverride`. A colon prefix must be followed
by primary input or contain named runnable input. The following is invalid and
does not update the session:

```text
:model effort=high
```

Use `/model effort=high` to change the session.

One slash command occupies the complete normalized Chat submission. It cannot
be mixed with primary input, named input, another slash command, or a colon
override. `/allow` and `/limit` accept multiple assignments so related session
changes remain atomic.

One colon prefix accepts at most one model body and one runnable identity.
Model parameter names and limit fields may each be assigned at most once. Allow
commands and limit commands may span multiple lines; only allow queries for the
same field accumulate. These checks apply to the complete prefix, independent
of line order.

Parsing, duplicate detection, ref resolution, validation available from existing
Chat metadata, and session replacement are atomic. Any such failure leaves
`SessionSetting` unchanged. Capability validation that requires core catalog data
occurs at run acceptance and does not retroactively change the session.

Immediate slash interactions such as help, inspection, queue control, steering,
and exit remain `QuickCommand` behavior. Setting commands are not quick actions
that list resources or open UI.

## Resolution

The effective values resolve as follows:

| Override field | Result for this run |
| --- | --- |
| absent | retain the `SessionSetting` value |
| model parameter only | retain model identity and other parameters; change the authored parameter |
| explicit model ref | select it, clear inherited explicit parameters, then apply same-command parameters |
| model `default` | use the captured surface model and its configured behavior |
| model `none` | use no model |
| runnable ref | use its exact resolved ref |
| runnable `default` | use the captured surface runnable |
| allow | add a ceiling intersected with the session ceiling |
| limit field | replace that session field for this run |

The exact model request, runnable request, allow ceiling layers, and limits are
written to the existing `RunRequest`. `SessionSetting`, `RunOverride`, parser state,
completion objects, and UI projections never enter it.

## Slash Completion

The complete slash setting family is:

```text
/model ModelBody
/runnable RunnableBody
/agic NAME
/flow NAME
/allow AllowBody
/limit LimitBody
```

Every body is required. Submitting `/model`, `/runnable`, `/agic`, `/flow`,
`/allow`, or `/limit` alone is invalid. It does not list values or open a picker.

An editor may show a completion popup while any command is being typed and may
insert a selected command, identity, field, or value into the draft. Completion
does not submit input or mutate `SessionSetting`; selecting a completion and
submitting the resulting text are separate operations.

## Removed Input Forms

The new boundary intentionally removes ambiguous compatibility forms:

- settings-only colon input; use the corresponding slash command;
- generic `:default FIELD=VALUE`; use `/model`, `/runnable`, `:model`, or
  `:runnable` according to the desired lifetime;
- positional `/model REF EFFORT`; use `/model REF effort=EFFORT`;
- collection shortcuts such as `:models QUERY`; use `:allow models=QUERY`;
- bare slash setting commands as list or picker actions; use completion while
  editing a complete command.

No aliases are retained for these forms. This keeps lifetime in the first
character and parameter meaning in assignment syntax.

## Implementation Touchpoints

- `src/toolang/execution/types.py` for `SessionSetting`, aggregate `RunOverride`, and
  removal of generic input policy command types;
- `src/toolang/execution/policy.py` for typed colon parsing, override merging,
  allow/limit materialization, and removal of `default` and collection command
  groups;
- `src/toolang/execution/calls.py` for the one-run override plus
  `RunnableInputRaw` contract;
- `src/toolang/base/types/model.py` for the canonical reasoning budget carrier
  and effort/budget exclusivity required by numeric input lowering;
- `src/toolang/cli/toolang/commands/chat/input.py` for the strict slash-setting
  versus colon-run classification;
- `src/toolang/cli/toolang/commands/chat/policy.py` for atomic session updates
  and request construction from `SessionSetting` without selector maps;
- Chat slash dispatch and completion plumbing to separate submitted settings
  from draft completion;
- `docs/input-syntax.md`, Chat documentation, and focused execution, input,
  local/remote Chat, slash, completion, and queue tests.

The existing `RunRequest`, `RunPolicy`, selector grammar, catalog/list response,
persistence, retry/rerun, pricing, and provider adapter behavior remain stable.

## Acceptance Tests

1. A colon prefix plus runnable input produces one `RunOverride`; the same
   prefix without primary or named input fails and never changes the session.
2. Each slash setting command atomically updates `SessionSetting` and never produces
   a run. It rejects additional primary, named, colon, or slash input.
3. Colon and slash model bodies share identity and assignment parsing while
   applying to the run and session respectively.
4. Parameter-only model changes retain identity and other parameters. Explicit
   identity selection clears unmentioned parameters before applying assignments.
5. Reasoning effort, budget, and Auto lower to canonical typed values and are
   authoritatively validated against the effective model before run acceptance,
   without changing catalog/list metadata.
6. Runnable generic and kind-specific forms resolve to the same exact ref.
   Unqualified missing or ambiguous identities fail; named input remains only
   in `RunnableInputRaw`.
7. Allow accepts multiple fields and accumulates repeated queries within one
   body or run prefix. Slash updates replace authored session fields; run
   ceilings cannot broaden the session ceiling.
8. Limit accepts multiple fields, rejects duplicate or invalid fields, and
   applies slash values to the session and colon values to one run.
9. Removed settings-only colon, `:default`, collection shortcuts, positional
   model effort, and bare slash picker forms all fail with focused diagnostics.
10. Every failed slash update leaves the prior `SessionSetting` unchanged. Every
    queued `RunRequest` remains unchanged after later session updates.
11. Local and remote Chat produce identical `SessionSetting` and materialized
    `RunRequest` values from the same inputs and surface baseline.
12. Existing direct HTTP, persistence, retry/rerun, model-list, selector, and
    adapter tests remain green with the complete default offline suite.

## Risks

- The lifetime boundary is intentionally incompatible with settings-only colon
  input and no-argument slash picker behavior. Focused errors and completion
  make the replacement explicit.
- Sparse model operations still require missing, reset, clear, and Auto states
  internally. Keeping those states inside the aggregate override avoids generic
  edit vocabulary without conflating their meanings.
- Chat has only existing list metadata and may store a syntactically valid
  setting whose unsupported capability is discovered on the next run. Mandatory
  core validation preserves correctness without expanding the list protocol.
- Current UI selector maps and low-level policy commands must be migrated in one
  change so request construction has only the typed path.

## Open Questions

None. Approval authorizes this input-layer implementation and its required
request-construction integration. Presentation redesign and new model parameters
require separate definitions.
