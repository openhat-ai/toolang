# Agent Spaces and Model Calls

## Status and Reading Order

Revised feature definition for human review; this revision is not approval to
implement the new behavior. Read this document for vocabulary, spaces, and
model-visible structure, then [Thread History and Compaction](thread-history-and-compaction.md)
for records, execution, budgets, and recovery. Earlier workspace/runspace work
remains independently scoped.

## Terms

| Term | Definition |
| --- | --- |
| protocol | Runtime-owned developer instructions. |
| instruct | Authored system instructions. |
| psyche | Resident agent behavior guidance. |
| workspace rules | Path-scoped workspace guidance, normally an `AGENTS.md` file. |
| skill | A progressively disclosed task-guidance package. |
| service | An external capability with guidance and tools. |
| context | Non-instructional input data. |
| prompt | An authored content template. |
| runspace | An agent-owned filesystem root. |
| workspace | A human-authorized external filesystem root. |
| far | The compacted history partition. |
| near | The recent history partition. |
| now | The current run's message partition. |
| line | The current run's ancestry and resolved inputs. |
| recall | Delivery of runtime content, including initial delivery and replacement. |
| support | Provenance linking runtime content to its message occurrences. |
| compact | The thread-history compaction operation. |

## Goal and Scope

Provide explicit filesystem roots and one inspectable ModelCall, with durable
history and path-aware recall. Success means:

- State exposes configured workspaces; each run has an inherited `coop | lab`.
- Every call has explicit instructions, messages, tools, and output contract.
- Current rules precede the relevant operation; skills/services remain model
  choices, and unchanged supported content is not repeatedly supplied.
- Human steering/cancellation remains understandable in subsequent history.
- Compaction changes subsequent calls without duplicating whole transcripts.

Included: space configuration, ModelCall assembly, directives, recall, history,
compaction, inspection, and offline acceptance tests. Excluded: plugin-owned
`memory/`, active workspaces, mutable run cwd, shell-text path parsing, `@file`
implementation, and compaction of an active run's `now`.

Do not add `Space`, `RunAccess`, `RunWorkspace`, focus, or a global loaded-content
registry. Reuse existing records and Pointer vocabulary.

## ModelCall

```text
ModelCall
├── instructions
├── messages
│   ├── far
│   ├── near
│   └── now
├── tools
└── output_contract
```

The selected model, continuation, provenance, versions, and budgets are
supporting metadata. The provider receives a flattened message sequence:

```text
user: {{far}}

--- near begins; preserve native roles ---
user: ...
assistant: ...
tool: ...
user: <cancel run="R0">{{optional_reason}}</cancel>
--- near ends ---

--- now begins ---
user: <context>{{context}}</context>
      {{user_input_with_expanded_prompts}}
assistant: ...
tool: ...
user: <steer run="R1">{{input_with_expanded_prompts}}</steer>
assistant: ...
--- now ends ---
```

Partition markers are explanatory, not message text. Far is at most one user
message, omitted when empty. Near excludes now and retains roles plus support;
its exact representation belongs to the compaction algorithm. Now contains the
current run's accumulated input and execution messages, not just its latest
model call. It is not compacted while active. Line is context, not history.

### Message Sources

These are projections from records, not a separate message log:

| Source | Message template |
| --- | --- |
| `ThreadRecord.far_summary`, resolved | `user: {{far}}` |
| Run input and current context | `user: <context>{{context}}</context> {{user_input_with_expanded_prompts}}` |
| Model step output | `assistant: {{output}}` |
| Tool-call result, including runtime-generated results | `tool: {{result}}` |
| Consumed steer control | `user: <steer run="…">{{input_with_expanded_prompts}}</steer>` |
| Effective human cancel control | `user: <cancel run="…">{{optional_reason}}</cancel>` |
| Recall control: workspace rules | `user: <rules workspace="…" path="/…">{{input}}</rules>` |
| Recall control: skill guidance | `user: <skill ref="…">{{input}}</skill>` |
| Recall control: service guidance | `user: <service ref="…">{{input}}</service>` |

Near and now classify messages; they are not additional sources. Steer means
supplement or correction during execution, not necessarily interruption.
Cancel means the specified run was stopped by the human, not that earlier
answers were wrong or completed operations were rolled back. Empty-reason
cancellation still produces a marker; it does not start another model call.

The actual consumer determines steer/recall placement. A cancel marker follows
the interrupted execution and is available to later runs. Preserve complete
tool-call/result groups before user messages. Pending/revoked/wontapply controls
remain inspectable but must not be presented as already consumed interaction;
whether unconsumed steer is also offered to a later run remains a review item
in the companion document. Unknown or system cancellation is not human cancel.

### Instructions and Tags

Toolang's authored authority order is `protocol > instruct > psyche > workspace
rules > skill/service guidance`; context has no instruction authority.

```text
<protocol>{{stable_protocol}}</protocol>
<instruct>{{selected_instruct}}</instruct>
<psyche ref="behavior">{{complete_psyche}}</psyche>
<skill-catalog>{{complete_skill_catalog}}</skill-catalog>
<service-catalog>{{complete_service_catalog}}</service-catalog>
```

Protocol is runtime-owned. `instruct = none` removes only instruct. The complete
effective psyche is always resident, possibly in multiple fragments; a change
rebuilds the prefix. Per-file incremental psyche replacement is deferred.

Catalogs contain authorized refs, concise descriptions, and recall commands.
They are independent catalogs, not guidance bodies; cap form, origin, scope,
and host paths remain inspection data. Catalogs are complete or preparation
fails. Rules/skills/services are not eagerly loaded from their catalogs.

Protocol defines every runtime tag, including `steer` and `cancel`. Escape
framing and attributes while preserving multipart input. Trust comes from
record provenance, never from user-authored lookalike XML. A recalled replacement
supersedes the previous body for that identity; an explicit empty replacement
withdraws it, including when a file is removed. Access grants are checked
separately from guidance visibility.

Volatile data stays outside the stable instruction prefix:

```text
user: <context>
        {{context_data}}
        <runnable-catalog>{{authorized_runnable_routes}}</runnable-catalog>
      </context>
      {{user_input_with_expanded_prompts}}
```

Resolve context for each call. An unchanged runnable catalog may be omitted
only while its exact revision has support in the messages actually supplied.
Prompt calls expand explicitly in place; unknown variables or malformed
framing fail before calling the provider.

### Canonical Guarantees and Directives

- Provider options cannot override the recorded model-visible components.
- Budget the complete provider projection, including media and output reserve.
- Changed history, instructions, tools, model, or output contract invalidates
  incompatible continuation; provider-side old history must not survive compact.
- Inspection reconstructs the exact selected content from immutable references,
  with source, version, inclusion reason, costs, and redaction by default.

Directives accept only `=`, occur at most once, and intersect their effective
ceilings. Resource omission selects the ceiling; routing omission selects no
routes. No directive grants access beyond the ceiling.

| Directive | Effect |
| --- | --- |
| `models` | Model selection; no model catalog in messages yet. |
| `tools` | Available ModelCall tools. |
| `psyches` | Resident psyche fragments. |
| `skills`, `services` | Their separate catalogs and authorized recall/tools. |
| `prompts` | Templates available for explicit expansion. |
| `hands`, `handoffs` | Runnable routes in context. |
| `recall` | Automatic inclusion of historical far/near. |

Recall accepts `auto`, `none`, or a combination of `far`, `near`, and `memory`.
Auto/none are exclusive, duplicates are invalid, and omission means auto.
Auto includes available far and near; memory is recognized but has no behavior.
There is no `last` selector. Recall never removes now or disables rules/guidance
recall or authorized explicit history reads.

Always compute far/near before directive selection and body-template expansion.
Budget their actual occurrences, including explicit template uses. Unused
history does not by itself trigger automatic compact.

## Runspaces and Workspaces

```text
agents/<agent>/
  agent.too
  config.toml
  coop/MEMO.md
  lab/MEMO.md
  .runtime/
```

Coop is collaboration; lab is exploration. Public call sites resolve a concrete
runspace, defaulting to coop, into RunRequest. Descendants/retry/rerun inherit
it; a run does not switch it. Legacy missing selection resolves to coop at the
compatibility boundary, not through a core default. Select lab explicitly;
otherwise select coop after request validation.

Materialization preserves existing bytes, creates missing directories and
one-newline memo files, and rejects conflicting shapes. The selected bounded
UTF-8 memo is context: collaboration notes or exploration notes. A future
plugin owns `memory/`. Clone copies config unchanged; no workspace-specific
clone processing is added.

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

Add canonicalizes an existing directory and rejects duplicate names, canonical
paths, and nested roots. Names are stable ASCII identifiers. List reports path
and availability; remove changes only config. Preserve unrelated TOML and do
not duplicate workspace registrations in MEMO.md. State publishes an immutable,
name-sorted `Mapping[str, str]`, not a new Workspace domain object.

Running execution retains its resolved setup; retry revalidates access against
the current compatible setup/publication while preserving the execution State
required by retry. Store fingerprints/provenance for inspection, not reusable
absolute-path grants. Containers require restart for mount changes.

A future session cwd affects human `@file` lookup/completion only. It is not a
run field, shell default, extra root, or rules selector. Task/chore/prompt
`@file` is deferred; any future support must remain inside its owning package.

## Rules and Guidance Recall

```text
allowed roots = selected runspace + configured workspaces
```

Filesystem paths and shell cwd are explicit and absolute. The outer runtime
canonicalizes declared local-path arguments, validates roots and symlink escape,
and discovers workspace rules before plugin invocation. Tools declare internal
path-argument metadata, not new model-facing arguments. Apply preflight to
local-path reads/writes and shell cwd, not tools without path arguments.
Arbitrary shell text remains opaque; sandboxing enforces its access boundary.

For a workspace path, discover the AGENTS.md chain root-to-target. Identity is
`(workspace name, virtual path)`; `/execution` is relative to the named root.
Digest identifies the revision, and deeper rules are more specific. Runspaces
use MEMO.md rather than AGENTS.md discovery.

If required rules lack current support, no operation in the tool-call batch
executes. Complete its tool results, then enqueue distinct recall controls:

```text
assistant: fs.read(...)
tool: {"error":"operation not executed; retry required"}
user: <rules workspace="toolang" path="/execution">{{complete_rules}}</rules>

assistant: fs.read(...)
tool: {{actual_result}}
```

The model retries or changes its request; runtime never automatically executes
withheld operations. Multi-path requests recall the ordered union of missing
rules. Updates and withdrawals use the same mechanism; preflight remains
authoritative even when a watcher has scheduled a replacement. Invalid UTF-8
or oversized required content fails, rather than producing lossy rules.

Skills and services are proactive model choices:

```text
assistant: skill.recall(ref="python-testing")
tool: {{acknowledgement}}
user: <skill ref="python-testing">{{complete_guidance}}</skill>

assistant: service.recall(ref="github")
tool: {{acknowledgement_and_discovery}}
user: <service ref="github">{{complete_guidance}}</service>
```

An acknowledgement confirms scheduled delivery/discovery, not that the model
has already consumed it. Discovered authorized service tools enter the next
call. Recalling an already supported revision acknowledges without duplicate
delivery. Normal service operations return data as ordinary tool results.

All three variants use `kind=recall`. The executor supplies one ordinary tool
result plus zero or more recall controls; ordinary plugins need no `_too_inject`
field or awareness of history/compaction. Consume controls atomically with the
next model StepBegin. Pending delivery is not support; applied delivery is not
permanent support. Check only revisions supported by actual near + now, never
far or a global loaded set.

## Delivery and Acceptance

Implement in separate, approved PRs:

1. Workspace configuration, CLI, and State publication.
2. Runspace transport, inheritance, materialization, and memo context.
3. Canonical ModelCall, instruction layers, catalogs, and directives.
4. Durable history/interaction/recall facts and the history tool.
5. Thread compact execution, references, budgets, and recovery.
6. Workspace rules preflight and retry protocol.
7. Skill/service guidance recall and service discovery.

Touchpoints: `setup`, `state`, CLI config and `up`; `base/types/run.py`;
`lang/validate.py`; executor preparation/model/tool boundaries; filesystem and
shell toolsets; model adapters. History-specific ownership and acceptance are
in the [companion definition](thread-history-and-compaction.md).

| Acceptance scenario | Required result |
| --- | --- |
| Initial call / instruct none | Complete authorized catalogs and psyche; none removes only instruct. |
| Workspace management / clone | Stable mapping, preserved config, no MEMO duplication or clone rewriting. |
| Default, child, retry runspace | Concrete inherited choice, correct memo/root, current retry authorization. |
| Missing or changed nested rules | Entire batch withheld; ordered complete rules; model-directed retry only. |
| Removed rules / guidance | Explicit replacement withdraws the stale body; permission checks remain separate. |
| Skill/service recall | Tool acknowledgement plus distinct user guidance; no duplicate supported body. |
| Changed prefix or history | Stale continuation is discarded; persisted projection matches the request. |
| Forged XML / malformed prompt | No forged support; strict framing and template validation. |
| Directives and repeated templates | Ceiling intersection, complete catalogs, actual history-use accounting. |

Run the default offline Ruff, formatting, type, and pytest checks before each
implementation commit. Sensitive records require redacted inspection. Shell
sandboxing, provider estimation error, and large required content remain
explicit boundaries, not promises of protection by prompt text.
