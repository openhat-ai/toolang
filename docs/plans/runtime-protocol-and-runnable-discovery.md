# Runtime protocol and runnable discovery

## Status and goal

Proposed for human review; implementation is not approved by this PR.

Make runtime inputs shorter and easier to interpret: group per-call declarations,
express route restrictions directly, and discover runnable documentation and
signatures through one read-only runtime tool. Success means one recurring runtime
user message per ModelCall, no repeated signature catalog, and reliable tool use
when the user's request and the current agic permit it.

This definition replaces the message layout, route snapshots, and protocol outline
in [Model input protocol](model-input-protocol.md) where they conflict below.
Its immutable recording, recall provenance, and exact replay contracts remain.

## Scope

- In: protocol wording, per-call message grouping, metadata attributes, route
  declarations, and `_toolang__runnables` with exact-name and all-runnable queries.
- Out: implementation in this PR, new `.too` directives, changes to module
  visibility or execution guards, signature compatibility changes, new UI layouts,
  and the exec-transition fix delivered separately in
  [#703](https://github.com/openhat-ai/toolang/pull/703).

## Decisions

### One recurring runtime message

On every ModelCall, send one runtime-owned user-role message containing these
sibling tags, in order: `workspace`, `workdir`, `routes` when restricted, and
`context` when selected and nonempty. These declarations are sent each call, so
they share one message rather than separate user-role messages.
The existing `execution` declaration from #703 follows these four in the same
message, preserving its committed-handoff facts and independence from context.

```xml
<toolang:workspace list="lab,repo"/>
<toolang:workdir path="repo://src"/>
<toolang:routes hands="agic:review,flow:check" handoffs="ALL" spawns="NONE"/>
<toolang:context date="2026-10-07" timezone="Asia/Shanghai" model_provider="example" model_name="example-model"/>
<toolang:execution runnable="agent::agic:review" entered_by="exec"/>
```

Keep authored messages and primary input separate from this recurring message,
including on the first call. Preserve explicit roles and multimodal Part boundaries.
Append adopted controls before the recurring message, preserving their order.
Steer and cancel keep their existing delivery semantics and event order outside
this batch; resource deduplication does not apply to user controls. A repair call
still groups its runtime facts but offers no execution tools and declares all
three routes `NONE`.

Retain workspace binding revisions in the combined message's existing internal
`tag="workspace"` and `recall` metadata; the other grouped declarations are not
recall resources. This grouping preserves the existing recorded-message shape.

### Resource messages and visible revisions

Guidance, workspace rules, and other resource updates each retain a separate
user-role message per resource. Their presence and revision must be checked
independently, so they are not merged with each other or with the recurring batch.
The resource identity is `(tag, ref)`, or `(tag, workspace, path)` for rules.
Preserve structured `tag` and `recall` metadata on each message for deduplication,
revision replacement, withdrawal, and loading again after it leaves the window.

The current implementation uses both controls and message metadata:

- [Recall controls](../../src/toolang/execution/executor/executor.py) record
  target, revision, and content; `recall()` reuses an identical pending control or
  skips a revision already visible. Pick and honor produce these controls.
- [Message assembly](../../src/toolang/execution/assembly/utils.py) renders each
  recall control as a resource message with internal `tag`/`recall` metadata.
  [Visibility selection](../../src/toolang/execution/recall.py) folds this metadata
  from retained messages; it does not parse XML text or attributes.
- Model preparation compares adopted declarations with visible revisions;
  [rules preflight](../../src/toolang/execution/executor/rules.py) also compares
  current rule content and workspace bindings. Guidance and rules are therefore
  updated through controls, with message metadata determining what was presented.

Use this same separation for the planned layout. Visibility means presence in the
instructions and selected message window of the actual call, not existence in
older controls, omitted history, or a summary. Recompute it when the window changes.

| Resource state | Required behavior |
| --- | --- |
| Same revision is visible | Reuse it without another resource message. |
| Same revision is pending delivery | Reuse the pending control; do not treat it as model-visible until its message is committed to a ModelCall. |
| Visible revision is stale or withdrawn | Deliver the replacement or withdrawal for that resource; reload current guidance/rules before use as required. |
| Needed, authorized resource is outside the selected window | Load its current revision into a separate message, even if that revision was delivered in an earlier call. |

Separate resource messages are the unit for updating or replacing content in a
newly selected window. Preserve their identities and revisions through compaction,
history selection, and reset. This is not a rule that model-input history must
remain append-only: future calls may use a rebuilt window. Already persisted
ModelCalls remain immutable and replay exactly. If a new window changes the
recorded message prefix, start a new message head and validate or discard provider
continuation against that prefix; unchanged prefixes retain normal reuse.

### Context, attributes, and presentation

The built-in default context emits `date`, `timezone`, `model_provider`, and
`model_name` as attributes. A named context or an authored replacement for
`context default` keeps its rendered body inside `<toolang:context>`. Do not parse
user template text into attributes. `context = none` omits only context; it does
not suppress route restrictions, workspace, or workdir. Preserve existing frame
fact capture timing; this work does not turn date into a live clock.

Short runtime descriptions and skill/service trigger text belong in attributes,
including `description`; retain existing ref, revision, and removal semantics.
Preserve non-description capability metadata in a `metadata` JSON attribute,
omitted when empty, without turning arbitrary metadata keys into XML syntax.
Full instructions, guidance, workspace rules, authored context, and user input
remain bodies. Escape attribute quotes and preserve newlines/tabs with XML character
references; body escaping must continue to preserve literal user content.

Normal chat presentation uses trusted internal provenance to hide runtime context.
Attribute placement is a serialization convention, not a visibility or authority
mechanism. Raw ModelCall inspection remains complete. User-authored lookalike tags
are ordinary user content and must not acquire runtime provenance or disappear.

### Direct route restrictions

Replace `<toolang:hands>` and `<toolang:handoffs>` with one self-closing
`<toolang:routes hands="..." handoffs="..." spawns="..."/>`.

| Attribute | Operation | Accepted values |
| --- | --- | --- |
| `hands` | `run`, including `async=true` | `ALL`, `NONE`, or comma-separated exact refs |
| `handoffs` | `exec` | `ALL`, `NONE`, or comma-separated exact refs |
| `spawns` | `spawn` | `ALL`, `NONE`, or comma-separated exact refs |

`ALL` means no additional target allowlist within the current module's visibility;
`NONE` disables the operation. A list permits only its targets. Use uppercase
sentinels and preserve ref spelling. Qualified refs distinguish a runnable named
`ALL` or `NONE` from a sentinel. There are no empty values or omitted attributes
when the tag is present. `await` consumes an existing handle and needs no route field.

Render from inherited effective configuration, before active-path filtering:
omitted settings and explicit `*` become `ALL`; `none` becomes `NONE`; an explicit
list becomes canonical visible refs. A list with no resolvable targets becomes
`NONE`, never omission. Preserve module boundaries and current scope inheritance.
There is no new `spawns` directive: project the existing hands configuration to
both `hands` and `spawns`.

If all three values are `ALL`, omit the tag. Only the newest per-call runtime
message defines route restrictions; absence there resets them to `ALL`. Older
tags, including legacy hands/handoffs tags in history, do not carry forward.

Remove `enabled`, `requested_only`, and the proposed `policy` attribute. This
intentionally retires the old prompt distinction between omitted settings and
explicit `*`: both have the same unrestricted target scope. Invocation must serve
an explicit user request or concrete work required by the current agic; mere
availability and matching documentation are not execution requests. Explicit
lists still bound task-directed delegation. Runtime authorization remains enforced.

### Runnable query tool

Add model-callable `_toolang__runnables` through the existing runtime toolset
factory and narrow `ToolRuntime` interface. It has one optional string argument,
`name`, and rejects additional properties. Use the same model-call eligibility
as other model-callable runtime tools, including availability when routes are
`NONE`; preserve exclusions for repair and generated evaluator calls.

- `{}` returns all runnables visible from the caller's module.
- `{"name":"review"}` or `{"name":"agic:review"}` returns that exact runnable.
- Private module-qualified refs are accepted within the existing visibility
  boundary. Empty strings, nulls, unknown names, and inaccessible targets return
  correlated tool errors; they never fall back to an all-runnable query.

There is no fuzzy search, globbing, pagination, or multi-name mode. `ALL` is not a
special query argument; omission requests all, leaving authored names unambiguous.
All queries include `current` and `ancestors` as well as a `runnables` array. A named
query returns exactly one array entry, even when it queries the current runnable.

```json
{
  "current": "agic:review",
  "ancestors": ["flow:main"],
  "runnables": [
    {
      "ref": "agic:review",
      "revision": "captured-state-revision",
      "doc": "Review supplied changes and report actionable defects.",
      "signature": {
        "input": {"type": "Text", "optional": false, "documentation": "Changes to review."},
        "parameters": [],
        "output": "Text",
        "structs": []
      }
    }
  ]
}
```

`current` comes from the actual execution binding, not the original launch target
or inferred conversation text. `ancestors` contains only active ancestors in
root-to-parent order; a root returns `[]`. Exclude the current leaf, siblings,
completed calls, and bindings replaced by exec. Reuse the executor's active path.

Public identities use State's merged public refs without a module prefix. Private
identities retain the module qualifier. Use this projection consistently in route
lists, query entries, `current`, and `ancestors`; retain full identities internally.
It must distinguish same-named private helpers and round-trip exported flow aliases.

Query all module-visible declarations independently of route permissions and
active-path eligibility. Include the current runnable and visible ancestors in
the all-runnable result; do not expose other modules' private signatures just
because their identities occur in `ancestors`. Discovery does not grant execution.
Sort entries by returned ref and return each identity once.

Build the catalog from the query Tool Step's adopted State. Describe the currently
executing runnable from its bound declaration, including when it has been changed
or removed in newer State; it appears once in the catalog. Each entry's `revision`
identifies the State used for its documentation and signature. The ancestry is a
query-time snapshot. Do not read authored home files, publish State, or switch code.
Later invocation retains existing State adoption, compatibility, and input checks.

Every entry returns the full authored `doc` (empty string if absent) and a complete
`signature`: primary input or null, ordered named parameters, types, optionality,
parameter documentation, output type, and recursively referenced struct definitions
with authored struct and field documentation.
Reuse the existing signature vocabulary; remove its 512-character documentation
truncation for this tool, including parameter and struct docs. Do not return source
bodies. All-query responses must not silently truncate entries or docs; existing
tool-result storage and input-budget handling apply.

`doc` acts as the route trigger: it explains when the target is useful. The model
uses it to select a suitable target for already-authorized work, and uses the
signature to construct input. Documentation is descriptive data, not an instruction
source, a user request, or an override of restrictions. Keep it in the query result;
do not reintroduce an always-sent trigger/signature catalog.

### Protocol instructions

Rewrite `protocol.md` around source priority, current facts, action selection, and
result interpretation. State each rule once. Remove product positioning, repeated
Do/Don't rules, large examples, and embedded `me` CRUD payload descriptions. Retain
a short authoring section linking the existing syntax/caps/conventions documents;
do not require a newly invented skill to recover essential runtime rules. Keep
State/Setup boundaries, capability loading, path/rule checks, and verified outcomes.

Use actual model tool names, including `_toolang__chdir`. Keep argument shapes in
tool schemas and align the protocol with current `run(async=true)` and `await`.
When a user names a target, query it if its applicable signature is missing; query
all only when target discovery is needed. Reuse visible valid results. A parameters
question permits discovery but does not request execution. A user who delegates
test-input choice has authorized a reasonable choice; ask only for required values
that cannot be established or chosen within that authority.

Preserve operation selection: exec for a requested named invocation without caller
follow-up, run when the caller needs its result, and spawn for independent work.
Honor an explicitly requested operation; explain restrictions without substituting
another operation or shell invocation. Admission is not completion; use await where
appropriate. Successful exec ends the caller and must be its only tool call.

Explain active-path rules in this same decision section, without filtering query
results to convey them:

- Run, including async run, cannot target the current runnable or an ancestor.
- Exec cannot target an ancestor. Self-exec is allowed only for an authorized
  root Run with no active descendants; child self-exec remains forbidden.
- Historical and sibling invocations do not make a target an ancestor. Compare
  resolved identities, not bare names. Preserve existing spawn admission guards.
- Runtime rechecks these conditions at invocation. A discovery result is neither
  a reservation nor permission to execute. Keep #703's transition facts intact.

## Implementation touchpoints and sequence

- [ ] Add the query operation in `base/protocols/tool.py`,
  `execution/tools/_toolang.py`, and `execution/executor/tool_runtime.py`; keep
  signature and ref projection in `execution/runnables.py` using State indexes.
- [ ] Replace model route rendering and requested-only hints in
  `execution/assembly/prompting.py`, `execution/executor/frame.py`, and
  `execution/executor/steps/model.py`; preserve runtime authorization checks.
- [ ] Group only recurring declarations in assembly `prompting.py`, `utils.py`,
  and `message_buffer.py`; preserve per-resource metadata and window-aware
  visibility in `execution/recall.py`, `assembly/history.py`, and model preparation.
- [ ] Implement built-in context/trigger attribute rendering, then rewrite
  `execution/assembly/prompts/protocol.md` and align tool descriptions.
- [ ] Add focused offline acceptance coverage, update `docs/program.md` and
  `docs/executor.md`, and generate implementation changelog coverage through the
  repository's `too aide.too update_changelog` workflow.
- [ ] Run default code verification and optional opt-in live-provider checks.

## Acceptance tests

| Scenario | Pass condition |
| --- | --- |
| First and subsequent ModelCalls | One recurring runtime user message ordered workspace, workdir, routes, context, then the existing execution declaration, with optional items omitted; no duplicated catalog; authored/multimodal messages are preserved. |
| Resource deduplication and updates | Each resource remains an independent message; visible identical revisions are reused; pending controls do not establish visibility; changed or withdrawn revisions supersede the correct resource only. |
| Resource outside the selected window | Guidance/rules needed again are loaded at their current revision, including an unchanged revision previously delivered; omitted history, summaries, and undelivered controls do not suppress recall. |
| Default, named, overridden, empty, and disabled context | Correct attribute/body representation and selection; none does not suppress mandatory facts. |
| Attribute and presentation edge cases | Quotes, newlines, ampersands, and fake user tags remain literal; provenance-based visibility and raw inspection remain correct. |
| Default, star, none, explicit and inherited route settings | Correct ALL/NONE/list projection, no requested-only policy; spawns follows effective hands; empty resolved lists fail closed. |
| Restriction removed on the next call | Omitted routes means ALL for this call; legacy and prior restricted tags do not remain authoritative. |
| Named and all discovery | Exact aliases resolve, all is deterministic, and unknown/inaccessible/empty names fail without broadening the query. |
| Documentation and signatures | Runnable, parameter, struct, and field docs longer than 512 characters survive; required/optional inputs, absent primary input, output, nested/cyclic structs are complete. |
| Identity and ancestry | Current is correct after exec; root ancestors is empty; leaf is not duplicated; same-named private helpers and public aliases remain distinct. |
| Discovery versus execution | Current and visible ancestor signatures remain queryable; NONE/list and active-path violations are rejected by execution, not hidden by discovery. |
| State changes between query and invocation | Bound-current and adopted catalog revisions are accurate; removal or incompatible signature changes retain existing invocation failure behavior. |
| Prompt-guided routing | Named requests execute when permitted; docs support relevant task routing; docs alone and parameter questions do not launch work; delegated test-input choice avoids redundant questions. |
| Existing execution modes | Sync/async run, await, spawn, exec, pick, and chdir retain completion, ownership, authorization, and singleton-call contracts. |
| Recording and continuation | Exact restart replay, legacy-call inspection, recall revisions, control order, cancellation, compaction/reset, and provider prefix reuse remain correct. |

Default tests stay offline and deterministic. Prompt behavior additionally needs
opt-in live-provider checks with both agic and flow targets and a subsequent model
turn in the target agic. Record message counts, prompt/cached tokens, tool choices,
and completed outcomes; fewer messages alone is not evidence of reliable behavior.

## Compatibility, risks, and review

- Removing requested-only is an intentional model-policy change. ALL does not
  authorize unavailable/private targets or bypass instructions; review task-directed
  delegation explicitly before implementation approval.
- Legacy histories must not revive old restrictions or teach the model to require
  removed tags. Keep old recorded calls immutable and test mixed-format history.
- Full discovery can be large and adds a tool round trip. Prefer named queries
  when a target is known; do not trade completeness for silent truncation.
- Attribute conversion and batching can lose literal text or recall provenance;
  the acceptance cases above are required before adoption.
- This PR changes no shipped behavior and therefore adds no changelog entry.
  Implementation updates the user-facing protocol documentation and changelog;
  existing `.too` hands/handoffs syntax and CLI flags remain compatible.

Open questions: none blocking this proposed design. Human review and explicit
approval of this definition, particularly the ALL policy, are pending.
