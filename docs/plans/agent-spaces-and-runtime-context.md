# Define Agent Spaces, Model Calls, and Compaction

## Status

Approved for implementation in the phases below.

## Terms

- **protocol**: immutable Toolang developer instructions.
- **instruct**: selected system instructions; `instruct = none` removes only
  this layer.
- **psyche**: resident behavior guidance, below protocol and instruct but above
  workspace rules. The complete effective psyche is always in instructions.
- **workspace rule**: path-scoped `AGENTS.md` content recalled before a tool may
  access the matching workspace path.
- **skill**: progressively disclosed guidance recalled by a model command.
- **service**: an external service, its guidance, and its discovered tools.
- **context**: current data with no instruction authority.
- **prompt**: a content template expanded in place in an authored message.
- **runspace**: the agent-owned `coop/` or `lab/` directory selected for a Run.
- **workspace**: a named external directory authorized by the human.
- **far**: compacted remote history, projected as at most one user message.
- **near**: recent, role-preserving history selected by compaction.
- **run**: the current Run's un-compacted messages, beginning with its resolved
  input.
- **line**: root-to-current-Run identities and resolved inputs. It is context
  data, not a history partition.
- **recall**: provision a complete rule, guidance, or runtime catalog body,
  whether for the first time, after a change, or after compaction forgot it.
- **support**: runtime-only provenance proving that a complete, current recalled
  body is present in selected messages.

Instruction authority is `protocol > instruct > psyche > workspace rule >
skill/service guidance`. Context has no instruction authority. State concepts
such as cap form, scope, origin, and host paths remain inspectable facts, not
model vocabulary.

Do not introduce generic `Space`, `RunAccess`, `RunWorkspace`, active/current
workspace, focus, mutable Run cwd, or global loaded-content state.

## Goal

Provide agent-owned runspaces, human-authorized workspaces, a deterministic
`ModelCall`, and budgeted history compaction. Runtime recall must ensure that a
model sees the current workspace rules before its requested path operation
executes, while skills and services remain explicit model choices.

## Success Criteria

- Every Run has one inherited `coop | lab` runspace, and State exposes all
  human-configured workspaces without a new workspace domain model.
- Every provider call comes from one inspectable canonical ModelCall whose
  fixed content and active Run fit the total context budget.
- V1 compaction deterministically selects complete recent groups into `near`,
  always emits empty `far`, and never mutates the active Run.
- Workspace operations execute only after current applicable rules are visible;
  skill and service guidance is recalled explicitly and without duplication.
- Compaction, parallel Runs, changed setup, and retry cannot bypass provenance,
  path authorization, or exact historical inspection.

## Scope

Included:

- runspace layout, selection, inheritance, memo context, and persistence;
- workspace configuration, CLI, State publication, and filesystem access;
- instruction layers, cap catalogs, directives, and canonical ModelCalls;
- rule and guidance recall protocols;
- a first compaction module with structured `near` and empty `far`;
- `line`, child/parallel isolation, retry, inspection, and offline tests.

Excluded:

- plugin-owned `memory/` behavior;
- far summarization and memory recall behavior;
- active workspaces, model-facing workspace management, or mutable Run cwd;
- parsing paths from arbitrary shell command text;
- initial Chat/TUI `@file` support; and
- task, chore, or prompt `@file` expansion.

## ModelCall

The canonical model request has four model-visible parts:

```text
ModelCall
├── instructions
├── messages
│   ├── far
│   ├── near
│   └── run
├── tools
└── output_contract
```

The selected model target, continuation, provenance, digests, and budgets are
supporting metadata. The provider wire format still receives one flattened
message sequence:

```text
# far, when present
user: {{far}}

# near begins; native roles are preserved
assistant: ...
tool: ...
user: ...
assistant: ...
# near ends

# run begins
user: <context>{{context}}</context>
      {{user_input_with_expanded_prompts}}
assistant: ...
tool: ...
user: <rule workspace="toolang" path="/execution">...</rule>
```

The comments above label partitions; they are not messages or wrapper text.
`near` excludes the current Run. The Run partition grows across model, tool,
and runtime-authored user messages and is never compacted while active. Tool
results stay in native `tool` messages. Partitions are projections chosen by
compaction, not the durable transcript format.

### Instructions

Instructions are ordered, escaped fragments:

```text
<protocol>
{{stable_toolang_protocol}}
</protocol>

<instruct>
{{selected_instruct}}
</instruct>

<psyche ref="behavior">
{{complete_psyche}}
</psyche>

<skill-catalog>
{{complete_skill_catalog}}
</skill-catalog>

<service-catalog>
{{complete_service_catalog}}
</service-catalog>
```

`<instruct>` is omitted for `none`; there may be multiple `<psyche>` fragments.
Catalog bodies use compact canonical data containing only opaque `ref`, concise
description, and recall call information. They do not expose cap form, scope,
origin, metadata, or host paths. Catalogs are complete or ModelCall preparation
fails; they are never silently truncated.

Any psyche change rebuilds the complete instruction set and invalidates its
cache prefix. Incremental psyche replacement is deferred.

The protocol defines authority, tags, recall behavior, tool-result reuse, and
route use. Volatile values such as Thread ID, agent home, program source,
environment, runnable routes, and future model choices do not enter this stable
prefix.

Runtime data belongs in the current user message:

```text
user: <context>
        {{context_data}}
        <runnable-catalog>
        {{authorized_runnable_routes}}
        </runnable-catalog>
      </context>
      {{user_input_with_expanded_prompts}}
```

The runtime omits an unchanged catalog already supported by `near + run` and
recalls the complete catalog when absent or stale. Future child-model catalogs
use the same placement. User-authored lookalike tags never acquire runtime
authority; provenance, not text parsing, establishes trust.

### Canonical Guarantees

- Provider options cannot override instructions, messages, tools, model, or
  output contract after the canonical ModelCall is recorded.
- A total context preflight accounts for instructions, messages, tool schemas,
  media, output contract, and output/reasoning reserve.
- Continuation reuse is bound to the relevant ModelCall prefix fingerprint;
  changed instructions, tools, model, or output contract invalidate it.
- Adapters project canonical instructions to the strongest supported provider
  role and normalize tool errors consistently.
- Persistence and inspection retain the exact canonical ModelCall and can show
  fragment source, authority, size, digest, inclusion reason, and provider
  projection with sensitive values redacted by default.
- Prompt expansion uses strict variables. Unknown variables and malformed or
  unescaped framing fail before a provider call.

## Directives

Directives support only `=`. A directive may appear at most once; `+=` and `-=`
are invalid. Every selection intersects the effective ceiling and can never
widen it. Omission selects the full ceiling for resource directives and no
routes for routing directives; `none` selects an empty set.

| Directive | ModelCall effect |
| --- | --- |
| `models` | Selects the model target; no message content in this version. |
| `tools` | Selects `ModelCall.tools`. |
| `psyches` | Selects resident `<psyche>` instruction fragments. |
| `skills` | Selects `<skill-catalog>` entries and allowed skill recall. |
| `services` | Selects `<service-catalog>` entries, service recall, and service tools. |
| `prompts` | Limits prompt templates expanded in place in messages. |
| `hands`, `handoffs` | Select authorized routes in runtime context. |
| `recall` | Selects which historical `far` and `near` partitions are generated. |

`recall` accepts `auto`, `none`, `far`, `near`, `memory`, or a combination of
`far`, `near`, and `memory`. `auto` and `none` must stand alone; duplicates are
invalid. Omission means `auto`.

In the first version, `auto` and explicit `near` enable near history. `far` is
always empty. `memory` is parsed, persisted, and inspectable but has no behavior.
Therefore `far` alone or `memory` alone recalls no history. Recall policy never
removes the active Run's required messages and does not disable rule or guidance
recall.

## Runspaces and Workspaces

```text
agents/<agent>/
  agent.too
  config.toml
  coop/MEMO.md
  lab/MEMO.md
  .runtime/
```

`coop` is the default collaboration runspace; `lab` is for exploration.
Materialization creates missing directories and one-newline memo files,
preserves existing bytes, and rejects conflicting shapes. Clone copies
`config.toml` unchanged and performs no workspace-specific work.

Public call sites resolve a concrete runspace into `RunRequest`; core execution
has no default. Descendants, retry, and rerun preserve it, and a Run never
switches runspace. Legacy records decode as `coop`. The selected bounded UTF-8
`MEMO.md` is context data: collaboration notes in `coop`, exploration notes in
`lab`. A future memory plugin exclusively owns `memory/`.

The agent config grants named workspaces:

```toml
[workspaces]
toolang = "/Users/alice/src/toolang"
website = "/Users/alice/src/website"
```

```text
too <agent> workspace add PATH [--name NAME]
too <agent> workspace list
too <agent> workspace remove NAME
```

Names are stable ASCII identifiers. `add` canonicalizes an existing directory
and rejects duplicate names, canonical paths, and nested roots. `list` reports
name, path, and availability. `remove` changes only config. Commands preserve
unrelated TOML and never duplicate workspace data in `MEMO.md`.

State publishes an immutable, name-sorted `Mapping[str, str]`; no `Workspace`
domain object is needed. Retry resolves access against the latest compatible
State publication, so removing a workspace revokes future retry access without
altering exact historical ModelCalls. No absolute-path access snapshot is
persisted. Containers expose available workspaces at deterministic guest paths
and require restart for mount changes.

A future Chat/TUI session cwd may affect only human `@file` lookup and
completion. It is not Run state, a tool default, an access grant, or a rule
selector. Authored task/chore/prompt resources must stay below their owning
package and reject absolute paths, parent traversal, and symlink escape.

## Filesystem Access and Rule Recall

```text
allowed roots = selected runspace + configured workspaces
```

Filesystem tools use explicit absolute paths; shell uses an explicit absolute
`cwd` and opaque command text. A generic execution preflight canonicalizes every
declared local path, rejects traversal and symlink escape, identifies one root,
and authorizes it before plugin invocation. Sandboxing remains the boundary for
paths hidden inside arbitrary process input.

Path-aware tools declare internal argument metadata such as
`local_paths=("path",)`; this is absent from their model-facing schemas. The
outer runtime implements preflight once. Plugins do not inspect messages,
compaction, rules, or access state. Every local-path read or write tool and
shell `cwd` uses preflight; tools without local path arguments do not.

For a workspace target, preflight finds the `AGENTS.md` chain from workspace
root to the target directory. A rule identity is `(workspace name, virtual
path)`; the path is rooted at `/` inside that workspace. The current content
digest selects its revision. The complete chain is recalled root-to-leaf, with
deeper rules applying more specifically. Runspaces use `MEMO.md` and do not
discover `AGENTS.md`.

If every required revision is supported by `near + run`, the runtime invokes
the plugin. Otherwise the whole tool-call batch is atomic: no plugin runs, every
call receives an error result, and the runtime appends each missing complete
rule as a separate user message:

```text
assistant: fs.read(...)
tool: {"error":"operation not executed; retry required"}
user: <rule workspace="toolang" path="/execution">
      {{complete_rule}}
      </rule>

assistant: fs.read(...)
tool: {{actual_result}}
```

The model retries or changes its action; the runtime never retries
automatically. Multi-path calls recall the ordered union of rule chains.
Invalid UTF-8 and oversized rules fail instead of becoming lossy summaries.
When a visible source changes, its new complete revision is recalled by the
same protocol. A watcher may append that recall before the next model call, but
preflight remains the authoritative check for every requested operation.

## Guidance Recall

Skills and services cannot be transparent to model reasoning. The model chooses
an opaque catalog ref and calls the corresponding command:

```text
assistant: skill.recall(ref="fastapi")
tool: {"status":"recalled","ref":"fastapi"}
user: <skill ref="fastapi">
      {{complete_skill_guidance}}
      </skill>

assistant: service.recall(ref="github")
tool: {"status":"recalled","ref":"github"}
user: <service ref="github">
      {{complete_service_guidance}}
      </service>
```

The command succeeds once its complete body is appended; it is not retried.
Normal service calls still return service data in tool messages. Service tool
schemas discovered by recall enter the next `ModelCall.tools`. A changed or
forgotten body is recalled through the same command. If current support already
exists in `near + run`, the command acknowledges it without appending a duplicate
user message.

Internally, one tool completion may produce exactly one normal tool result and
zero or more runtime-authored messages appended after it. This is an execution
facility, not a plugin return convention: ordinary plugins still return one
ordinary result, and no `_too_inject` field or recall-specific plugin logic is
introduced.

## Compaction Module

`src/toolang/execution/compaction.py` is a pure execution module. It has no
store, filesystem, watcher, cap, service, or plugin dependency. Compaction sees
messages, costs, atomic grouping, and opaque support assertions; it does not
parse XML or understand rule/skill/service semantics.

Its small public surface is:

```text
group_messages(messages, support) -> MessageGroup[]
compact(groups, policy, history_budget) -> CompactionResult
is_visible(support, identity, digest) -> bool
```

- `group_messages` validates provider ordering and binds an assistant tool-call
  message, every correlated tool result, and associated runtime-authored user
  messages into an indivisible group.
- `compact` selects historical groups and returns `far`, `near`, retained
  support, and omitted cost/count diagnostics.
- `is_visible` performs an exact generic support lookup. The runtime queries
  the union of compaction support and active-Run support.

`MessageGroup`, `CompactionResult`, and support records belong in execution
`types.py`; storage conversion remains in records/store code. Token costs come
from the selected model's estimator and are inputs, not a provider dependency
of compaction.

### First Algorithm

1. The ModelCall builder resolves instructions, tools, output contract, output
   reserve, and the exact active Run. If those fixed parts exceed the total
   budget, preparation fails.
2. Their cost is reserved. The remainder becomes `history_budget`.
3. `group_messages` validates eligible durable history and its support.
4. If near is enabled, `compact` walks backward from the newest group until the
   next complete group cannot fit, then restores chronological order. Otherwise
   near is empty.
5. `far` is always empty. Older groups are omitted from the request but remain
   in durable execution history and exact inspection.
6. The builder flattens `far + near + run`, then performs a final total-budget
   check before the provider call.

No group is split or summarized. In particular, assistant/tool protocol groups
and their runtime recall messages remain atomic. Support is retained only with
the complete selected group that proves it. `far` never satisfies visibility.

This deliberately allows forgetting: after a rule or guidance body leaves
near, the next relevant preflight or model command recalls it again. Future far
summarization may consume omitted history without changing the result shape or
the rule that only structured `near + run` support proves visibility.

## Run Trees and Persistence

A root Run captures one historical compaction snapshot. Descendants use that
snapshot plus their own Run messages and line; siblings never observe each
other, so parallel execution is timing-independent. `line` excludes outputs,
tools, siblings, and descendants. Same-Run handoff does not extend it.

A completed public root Run's native message stream, including tool results and
runtime recall messages, becomes eligible future Thread history. Child streams,
flow locals, and sibling exchanges remain execution evidence unless their
owning feature explicitly publishes them.

Each model step persists its exact canonical ModelCall, result, compaction
diagnostics, and structured support. Replay and inspection use stored facts.
Retry builds a new ModelCall from durable Run state plus current setup, so it
revalidates workspaces and rule revisions instead of reusing old access paths.

## Implementation Phases

1. Publish workspace configuration and CLI management through State.
2. Carry runspace through requests, records, descendants, retry, and inspection;
   materialize runspaces and provide the selected memo as context.
3. Separate protocol, instruct, psyche, catalogs, context, and prompt expansion;
   enforce directive and canonical ModelCall guarantees.
4. Add compaction types/module, budgeted near selection, structured support,
   persistence, and inspection. Keep far empty.
5. Add generic local-path preflight and workspace rule recall.
6. Add skill/service guidance recall and service-tool discovery integration.

Each phase is an independently verifiable implementation pull request.

## Implementation Touchpoints

- `setup`, `state`, `up`, and CLI config commands: runspace layout, workspace
  publication, hosting, and mounts.
- `base/types/run.py`, execution schemas/records/store, API, and clients:
  runspace transport, canonical ModelCalls, support, persistence, and inspect.
- `lang/validate.py` and generated language artifacts: directive grammar and
  recall values.
- `execution/executor/prepare.py`, new `execution/compaction.py`, and execution
  `types.py`: assembly, budgeting, grouping, projection, and visibility.
- execution tool dispatch, filesystem/shell plugins, and model adapters:
  preflight, runtime messages, provider projection, and continuation safety.

## Design Validation and Acceptance Tests

| Scenario | Required result |
| --- | --- |
| First ModelCall | Complete protocol, selected instruct/psyche/catalogs, current context/input, exact tools and output contract fit the total budget. |
| `instruct = none` | Protocol, psyche, catalogs, tools, and output contract remain; only instruct is absent. |
| Workspace read without visible rule | No call in the batch executes; tool errors request retry; complete applicable rules follow as runtime user messages. |
| Retried workspace read | Current digests are supported by `near + run`; the plugin executes once. |
| Nested or multi-path workspace call | The ordered union of applicable rule chains is recalled once, without host paths in model tags. |
| Rule changes or leaves near | Old/missing support does not count; the complete current revision is recalled before execution. |
| Skill/service use | The model selects a catalog ref; recall yields a tool acknowledgement plus a complete tagged user message; service tools appear on the next call. |
| `recall = none`, `far`, or `memory` in V1 | Historical near is absent and far is empty; active Run messages and rule/guidance recall still work. |
| Near budget cuts a tool exchange | The whole group is retained or omitted; roles, call/result correlation, and support remain valid. |
| Child and parallel Runs | Each sees the captured history, its own run/line, and no sibling messages. |
| Workspace removed before retry | Historical calls remain inspectable; the new attempt fails current authorization. |
| Changed ModelCall prefix | Continuation is invalidated; stored canonical call and actual provider projection agree. |

Acceptance tests cover these scenarios, strict directive/template validation,
legacy `coop` decoding, exact persistence, redacted inspection, local/container
path enforcement, and deterministic offline budget accounting.

## Risks

- Rules, memos, and tool results may expose sensitive local data in model calls
  and records; inspection must redact by default.
- Provider token estimators differ; reserve conservatively and fail closed after
  final serialization checks.
- Shell command text cannot be path-preflighted; sandboxing remains its security
  boundary.
- Empty far intentionally forgets old context in V1; callers needing continuity
  must keep required facts in near/run or recall them from an authoritative
  source.

## Open Questions

None.
