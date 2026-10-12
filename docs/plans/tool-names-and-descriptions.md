# Tool names and descriptions

Status: proposed; awaiting human approval. Definition only.

## Goal and scope

Make each built-in tool recognizable from a short progress sentence, including
when several toolsets perform similar operations. Rename `me` to `state`,
`_toolang` to `runtime`, and `msg` to `convo`; align model descriptions with that
vocabulary and shorten conversation-management leaf names.
Separate team and conversation discovery, and expose conversation reads and
writes from the current agent's perspective.

Success means all 49 proposed built-in leaves have explicit lifecycle summaries;
the action and object identify the operation without a repeated tool-name prefix.
Generic fallback retains the qualified tool display name. Chat and Script show the
same wording, and otherwise-supported saved history remains readable.

This definition supersedes the corresponding identity and wording decisions in
[internal toolsets](internal-toolsets.md),
[plugin layout](plugin-listing-visibility.md), and
[tool progress](tool-progress.md), plus the tool surface in [teaming](teaming.md).
Keep their other contracts.
Changes to capabilities, arguments, and results are limited to conversation
discovery, message inspection, and write as defined below. Resource access, runtime execution
semantics, tool-name grammar, CLI commands, and underlying teaming service
contracts remain unchanged. Message reception remains owned by the talk handler.

## Verified baseline

- `pyproject.toml` registers eight toolsets: `me`, `_toolang`, `fs`, `shell`,
  `web`, `history`, `service`, and `msg`.
- `ToolDefinition.description` guides models; `Tool.summary()` supplies progress
  text. They are separate contracts.
- `me`, `service`, and `msg` have no custom summaries. Runtime supplies summaries
  for only spawn, pick, honor, and compact. Executor fallback is
  `Executing/Executed <leaf> <first argument>`, omitting the toolset.
- Fs summaries such as `Listed repo://src` omit the object category. Runtime
  run/async run/exec/spawn/await rows use CLI-owned wording instead of saved
  summaries, so changing a tool hook alone does not update those rows.
- `me` edits supported home files, including Markdown chores outside State;
  only sync checks and publishes State. It does not switch active Run code.
- Model tool definitions use `__`, but workspace/shell errors instruct models to
  use `_toolang.chdir`. Human documentation also uses dotted call notation.
- `msg.targets` returns participant and accessible-conversation directories.
  `msg.resolve` returns a single Resolution, supports explicit conversation-name
  lookup, and rejects duplicate names. `msg.send` accepts canonical targets and
  bare-agent shorthand, but does not look up conversation names.

## Names and compatibility

| Current | Canonical | Meaning | Implementation module |
| --- | --- | --- | --- |
| `me` | `state` | Current agent source files and State publication | `execution/tools/state/` |
| `_toolang` | `runtime` | Run execution, workdir, guidance, and history compaction | `execution/tools/runtime.py` |
| `msg` | `convo` | Conversation discovery, messaging, and membership | `plugin/toolsets/convo.py` |

Under `convo`, shorten `create_conversation`, `rename_conversation`,
`join_conversation`, and `leave_conversation` to `create`, `rename`, `join`, and
`leave`. Replace `targets` with `team` and `list`, rename `send` to `write`, remove
`resolve`, and add `messages`. The eight-leaf surface below replaces seven messaging
leaves, increasing the built-in catalog from 48 to 49. No separate `query` or
`recv` tool is exposed.

Update entry points, plugin/toolset identities, owned class/context names,
selectors (`state/get`, `runtime/run`), model names (`state__get`,
`runtime__run`), configuration keys, internal dispatch, errors, and bundled
instructions together. Retain `<toolset>/<leaf>` and `<toolset>__<leaf>` encoding.
Keep `fs`, `shell`, `web`, `history`, and `service` unchanged. Rename the messaging
toolset class/context to `ConvoToolset`/`ConvoToolContext`, including setup config
injection and executor dispatch. The underlying teaming API, `/msg` HTTP routes,
client method names, conversation IDs/storage, and message receipts do not change.
The unrelated provider/model metadata field `_toolang` is unchanged.

`runtime` remains Toolang-owned and selected independently of user tool ceilings;
`state` and `convo` remain ordinary selectable toolsets. Reserve `runtime` against
external registration, both as a plugin name and an explicit toolset key, using
existing distribution provenance. A public-looking name must not grant runtime authority.
Honor and compact remain runtime-only, excluded from model definitions/calls.
Existing underscore-name restrictions remain available for other internal names.

No callable aliases for `me`, `_toolang`, `msg`, or replaced messaging leaf names
are added. Migration replaces toolset and leaf names in authored tool
selections, plugin configuration, and direct tool calls; restart hosts and prepare
fresh Setup before starting new Runs. Split targets consumers between team and
list; use list's name filter for discovery and messages for conversation information
instead of resolve. Send becomes write: rename `body` to `message` and
`in_reply_to` to `reply_to`, preserve `target`, strip `agent:` from agent targets,
and keep canonical IDs. Human
targets and explicit participant pairs must use a conversation ID obtained from
list or create. Other leaf arguments and write receipts need no conversion.
None of the renamed toolsets stores durable data in its plugin room; leave old
rooms alone.

Do not rewrite saved records, model exchanges, or summaries. Readers that classify
old `_toolang` operations must continue recognizing them, including the existing
`_toolang__execute` spelling, for history assembly and progress reconstruction.
This is read compatibility, not permission to invoke old names. Resuming an
in-flight pre-upgrade execution is outside scope. Document the rename as breaking
in the implementation changelog using the repository's runnable workflow.

## Conversation tools

| Display name | Arguments | Result |
| --- | --- | --- |
| `convo.team` | None | Existing `TeamMember[]`, including agents and humans |
| `convo.list` | `name: string \| null = null` | Existing `ConversationSummary[]` for accessible conversations |
| `convo.messages` | `target: string`, `limit: integer = 50` | `{conversation: Conversation, messages: [{stream_id, message: Message}]}` |
| `convo.write` | `target: string`, `message: string`, `reply_to: string \| null = null` | Existing receipt: `{conversation: string, stream_id: string, message: Message}` |

Team retains registered member IDs, display names, owners, and presence. List
returns only conversations, retaining IDs, names, kinds, members, revisions, and
latest message IDs, without message previews. Omitted/null `name` lists all;
otherwise filter by exact, case-sensitive conversation name using existing name
validation. Return all duplicates or an empty list. This one optional filter
covers name lookup without a separate query method or mixed member/conversation
result. No substring, pattern, pagination, or other filters are added.

Messages and write share one `target` parameter: a canonical `dm_...`/`gc_...` ID
selects that exact conversation; otherwise a validated bare agent name such as
`alice` selects the DM between the current agent and `agent:alice`. Use the
existing symmetric DM-ID derivation. The caller comes exclusively from tool
context, never a parameter. Reject unknown agents, self-DMs, typed participant
targets (`agent:`/`human:`), and participant-pair shorthand. Canonical-ID syntax
takes precedence even if an agent has that name; use that agent's DM ID from
list/create in that rare case. An ID lookup/access failure never falls back to
an agent. Never match a conversation name; if a conversation and an agent are
both named `alice`, the bare target still means the agent's DM.

Write appends one message as the current agent. For an agent-name target, create
or reuse the implicit DM through the existing delivery path; an absent DM is
created atomically with its first message. A conversation-ID target must already
exist and permit the caller to write. Preserve body/reply validation, membership
and live-lease checks, run/thread provenance, receipt identity, and unconfirmed
delivery behavior. Never auto-join, retry, or choose another target on failure.
The required `message` argument is plain text mapped to the existing message
record's `body`; stored records and receipts keep their fields. Optional `reply_to`
maps to the existing `in_reply_to` field and is described as: "ID of the message
being replied to. Omit for a new message." Its value is the original Message's
UUID `id`, not a conversation ID or `stream_id`. It records a reply reference;
`target` alone selects the destination. Talk replies retain their source-message
reference using `reply_to`.

Messages returns conversation metadata and at most the latest `limit` retained
messages, ordered oldest to newest. Accept integers `0..1000`; `0` fetches only
metadata and returns an empty messages array. Decode messages using the existing
Message contract and report invalid stored entries rather than silently dropping
them. An unknown/inaccessible conversation or not-yet-created implicit DM is an
error; reading never creates a conversation. Messages is an immediate history lookup:
it does not wait for new messages, advance a cursor, acknowledge delivery, mark
messages consumed, or invoke the talk handler. No recv/subscription or read
pagination is introduced, and talk delivery remains unchanged after a read.

Implement target interpretation once in the convo toolset. Reuse AgentClient's
team, contacts, resolve, conversation, history, and send methods; no `/msg` route,
client API, or backend schema changes are needed. List filtering and read result
assembly belong to the toolset. For example, model calls use `convo__list` with
`{"name":"Development"}`, `convo__messages` with `{"target":"alice","limit":20}`,
or `convo__write` with either `{"target":"alice","message":"Ready"}` or
`{"target":"gc_00000001","message":"Ready"}`.

## Identity at each boundary

One structured `ToolRef` owns plugin provenance, toolset, and leaf identity.
Derive the three spellings from its components; do not maintain parallel
registrations or use a parser that accepts every spelling everywhere.

| Context | Spelling | Rule |
| --- | --- | --- |
| Resource ref, query, configuration, CLI `REF`/JSON | `state/get` | Canonical ref; preserve copyable selector syntax |
| Model tool definition/call/result name | `state__get` | Exact declared protocol name; shared across adapters |
| Human tool label, explanatory prose, progress fallback | `state.get` | Display only; not a selector or callable alias |

Keep `ToolRef.identity` and `model_name`; add a derived `display_name` using the
same components. Share formatting helpers for boundaries without a loaded
ToolRef. Do not persist an additional display-name field. Existing event/history
fields that contain model names retain their contract; this is not a storage
migration. Query examples and tables explicitly headed `REF` retain slash names,
even in human documentation. Dotted call notation in prose is explanatory, not
executable Toolang syntax.

Model tool names are already encoded by LoadedTool before adapters serialize
them. Adapters preserve those names and never independently choose a separator.
Tool definitions, bundled model instructions, and actionable error hints use
the exact model name when directing a call, for example `runtime__chdir`.
Resource-ref values and query arguments remain slash-form data. Audit current
cross-tool references such as `use _toolang.chdir`, not just tool registration.

Add one protocol instruction: copy tool-call names exactly from the tools
provided for the current Model Call; selector refs and display labels are not
callable names. Normal narration uses action/object wording. When discussing a
tool by name, prose may use the dotted label; code/query examples use the syntax
required by their destination. Do not rewrite model text, quoted content, code,
or saved tool exchanges with separator replacements.

If an actual call uses `state.get` or `state/get`, retain exact-name rejection;
do not normalize and execute it silently. When that exact display/ref spelling
identifies one tool advertised in the current Model Call, return an actionable
error such as `Unknown tool name 'state.get'; call 'state__get' instead.` No tool
is invoked; the model may submit a corrected call. Build suggestions from that
call's advertised tool set, not the global registry: unavailable/runtime-only
tools receive no suggestion. Do not use fuzzy matching, leaf-only matching,
old namespace aliases, or automatic retries. Preserve original call names and
IDs in the failed exchange. This keeps protocol mistakes visible and avoids
changing execution semantics to accommodate display notation.

## Description rules

Model descriptions start with the operation, concrete object, and access scope.
Follow with only the selection, input, result, and failure details needed to use
the tool correctly. Update cross-tool references to canonical names. Preserve
digest checks, sync stability requirements, service auth/session/schema reuse,
message delivery rules, and runtime route/ownership restrictions.

Use **agent file** for state CRUD, **State** for publication, **file/directory/path**
for workspace operations, **service tool/resource/prompt** for MCP operations,
and **message/conversation** for messaging. State's toolset description must say
that files are authored sources, writes do not publish State, and chores are
outside State. Keep exact protocol details in parameter descriptions when they
describe individual inputs; do not discard operational constraints for brevity.

Progress uses `Action object[: target]`, with the discriminating object before
the potentially long target. Avoid `Executing list`, raw leaf names, repeated
`state.get` prefixes, and implementation names in normal built-in summaries.
The following table fixes the running/success vocabulary; `{...}` denotes data,
not literal text. The optional colon and target disappear together when absent.

| Tool ref | Running / succeeded sentence |
| --- | --- |
| `state/list` | `Listing / Listed agent files` |
| `state/get` | `Reading / Read agent file: {key}` |
| `state/create` | `Creating / Created agent file: {key}` |
| `state/update` | `Updating / Updated agent file: {key}` |
| `state/delete` | `Deleting / Deleted agent file: {key}` |
| `state/sync` | `Syncing / Synced agent state` |
| `fs/list` | `Listing / Listed files: {path}` |
| `fs/read` | `Reading / Read file: {path}` |
| `fs/write` | `Writing / Wrote file: {path}` |
| `fs/append` | `Appending to / Appended to file: {path}` |
| `fs/glob` | `Matching / Matched files: {pattern} in {path}` |
| `fs/stat` | `Inspecting / Inspected path: {path}` |
| `fs/mkdir` | `Creating / Created directory: {path}` |
| `fs/remove` | `Removing / Removed path: {path}` |
| `shell/execute` | `Running / Ran command: {command}` |
| `web/search` | `Searching / Searched web: {query}` |
| `history/read_threads` | `Reading / Read threads` |
| `history/read_runs` | `Reading / Read runs: {thread}` |
| `history/read_steps` | `Reading / Read steps: {run}` |
| `history/read_output` | `Reading / Read run output: {run}` |
| `runtime/runnables` | `Discovering / Discovered runnables` (or `runnable: {name}`) |
| `runtime/chdir` | `Changing / Changed workdir: {path}` |
| `runtime/run` | `Running / Completed child run: {runnable}` |
| `runtime/run` with `async=true` | `Starting / Started child run: {runnable}` |
| `runtime/spawn` | `Spawning / Spawned new run: {runnable}` |
| `runtime/await` | `Waiting for / Received run result: {target}` |
| `runtime/exec` | `Switching to / Switched to runnable: {runnable}` |
| `runtime/pick` | `Loading / Loaded guidance: {kind}/{ref}` |
| `runtime/honor` | `Loading / Loaded rules` (successful result may add `: {files}`) |
| `runtime/compact` | `Compacting / Compacted thread history` |
| `service/start_bridge` | `Starting / Started service bridge: {service}` |
| `service/stop_bridge` | `Stopping / Stopped service connection: {service}` |
| `service/init` | `Initializing / Initialized service session: {service}` |
| `service/start_auth` | `Starting / Started service authorization: {service}` |
| `service/complete_auth` | `Completing / Completed service authorization: {service}` |
| `service/list_tools` | `Listing / Listed service tools: {service}` |
| `service/call_tool` | `Calling / Called service tool: {service}/{tool_name}` |
| `service/list_resources` | `Listing / Listed service resources: {service}` |
| `service/list_resource_templates` | `Listing / Listed service resource templates: {service}` |
| `service/read_resource` | `Reading / Read service resource: {service} {resource_uri}` |
| `service/list_prompts` | `Listing / Listed service prompts: {service}` |
| `service/get_prompt` | `Getting / Got service prompt: {service}/{prompt_name}` |
| `convo/team` | `Listing / Listed team members` |
| `convo/list` | `Listing / Listed conversations: {name}` |
| `convo/messages` | `Reading / Read conversation messages: {target}` (`conversation: {target}` when `limit=0`) |
| `convo/write` | `Writing message: {target}` / `Wrote message: {conversation}` |
| `convo/create` | `Creating conversation: {name}` / `Opened conversation: {id}` |
| `convo/rename` | `Renaming / Renamed conversation: {conversation}` |
| `convo/join` | `Joining / Joined conversation: {conversation}` |
| `convo/leave` | `Leaving / Left conversation: {conversation}` |

For `convo.create`, a DM may be reused; use the returned canonical ID on
success when present, otherwise the supplied name or no target.
Messages/write running and failure summaries use the supplied target. Write success
uses the receipt's canonical conversation ID, falling back to the target if that
display field is absent. List success does not imply a match was found.
`runtime.exec` switches the runnable within the current Run; the caller does not
resume. Its success sentence means the switch was committed, not that the target
completed.
On history cursor pages use `Reading/Read more threads`,
`more runs`, or `more steps`; never display the opaque cursor. Stop_bridge's
broader object covers both stdio bridge shutdown and HTTP session/auth cleanup.

Failure uses `Failed to <base action> <same object and target>`; cancellation
retains the executor's `Canceled: <running summary>` convention. Success follows
the existing ToolResult error boundary; it does not reinterpret nested service
responses or nonzero shell exits. Sync failure must never say State was synced.

Retain existing workspace URI/relative-path labels, command/query quoting, and
guidance/rules identity formatting. Summaries use only isolated, masked call data
and existing result fields; no filesystem, network, Store, or State reads.
Built-in summaries omit file contents, message bodies, nested service inputs,
auth URLs, tokens, digests, result dumps, and counts. Missing/malformed display
fields retain the object label or fall back; never display `None` or raise into
execution.

## Progress integration and fallback

Use the existing optional `summary()` hook, not a new description protocol or a
renderer registry of tool names. Add missing built-in hooks at their owners.
Existing plugins and function tools require no new method, metadata, dependency,
or registration changes. Do not require custom summaries during plugin loading.
The catalog-wide coverage requirement applies only to Toolang's built-ins.

Executor fallback uses the ToolRef's dotted display name, or derives that display
name from a valid encoded call name when the ref is unavailable. If no family
can be recovered, retain the original call name. Every lifecycle fallback must
include the first supplied argument's preview after the identity; omit the
preview only when the call has no arguments.
Select the first supplied property in schema order. If the schema is unavailable
or none of its properties is supplied, use the first argument in call input order.
Do not insert omitted defaults. Preserve masking, single-line formatting, and
the existing 80-character preview limit. Sensitive values display `<redacted>`;
empty strings, null, false, and zero still have previews. For example:

| Lifecycle | Plugin with no custom summary |
| --- | --- |
| Running | `Executing acme.list projects` |
| Succeeded | `Executed acme.list projects` |
| Failed | `Failed acme.list projects` |
| Canceled | `Canceled acme.list projects` |

This same fallback applies to inherited/default hooks, None, empty/whitespace
or non-text returns, and formatter exceptions. A missing or broken formatter
must not change tool execution or result delivery. Do not infer an action's tense
from a leaf name or truncate/parse the model-facing description to invent progress
text. Custom summaries replace the fallback without an automatic family prefix;
plugins that want natural action/object wording can opt into the existing hook.

For new runtime Tool Steps, execution progress reads the saved tool summary for
the primary row, including run, async run, spawn, await, and exec. Preserve child
hierarchy, admission ID/thread rows, handoff behavior, markers, timing, and error
rows. Native Flow wording and legacy runtime presentation remain unchanged.
No extra family prefix, panels, status events, or new persistence fields are
needed. Truncation happens after the leading action/object so ordinary narrow
rows retain the distinction. Replays never call live tool formatters.

## Implementation touchpoints

- `pyproject.toml`, `execution/tools/{me,_toolang.py}` and their imports:
  identities, module/class names, model descriptions, and owned summaries.
- `plugin/toolsets/{registry,loading,collections}.py`, setup/resource selection,
  executor dispatch/context injection, runtime admission/control helpers, and
  `execution/assembly/tool_replies.py`: authority, canonical names, and saved
  history classification. Audit exact old-name references, not global `_toolang`
  replacement across unrelated model metadata.
- `base/utils/tools.py`, `base/utils/workspace_paths.py`, model-facing tool
  descriptions/errors, and `execution/assembly/prompting.py`: shared identity
  formatting and consistent call instructions. Check adapter serialization and
  invalid-name responses against the actual advertised tool definitions.
- `plugin/toolsets/{fs,shell,web,history,service,msg}.py`,
  `base/utils/tool_descriptions.py`, and `execution/executor/steps/tool.py`:
  vocabulary, lifecycle summaries, and qualified fallback.
- `plugin/toolsets/msg.py` to `convo.py`, `base/types/tool.py`, and
  `setup/{tools,watcher}.py`: messaging identity, discovery/messages/write contracts,
  shared target interpretation, owned context, and setup configuration
  injection. Verify against `teaming/{client,agent_client,messaging,schemas}.py`
  and the existing unit/integration messaging suites. Update `work/messaging.py`
  call instructions to `convo__team`, `convo__list`, and `convo__write` with
  `message`/`reply_to` arguments; preserve
  polling, checkpoints, and talk-delivery behavior.
- `cli/common/execution_progress/{operations,step_projection}.py`: new runtime
  recognition and saved summary use with existing presentation structure.
- `execution/assembly/prompts/protocol.md`, active tool/plugin documentation,
  affected examples/configuration fixtures, and generated references through
  their existing workflow. Preserve historical changelog records and completed
  plans; link superseding decisions where needed.

## Acceptance and risks

1. Registration exposes exactly the mapped names and 49 leaves. Old
   names are not callable. External attempts to register `runtime` as a plugin
   or explicit toolset fail; ordinary state/convo selectors still constrain access.
   Runtime-only leaves stay hidden and uncallable by models.
2. A catalog-driven test requires every built-in leaf to provide running,
   success, and failure summaries. Representative snapshots assert the table,
   including state/fs reads, every list operation, service/tool pairs, message
   operations, sync errors, async runs, and reused conversations.
3. Legacy class and function plugins with no custom summaries still load and run.
   Two external `list` tools have distinct qualified fallback text and include
   the first supplied argument in all four lifecycle states. Verify schema-order
   selection, call-order fallback without matching schema properties, omitted
   defaults, empty/null/false/zero values, redaction, truncation, and omission only
   for argument-free calls. Default/None/empty/non-text/throwing hooks,
   explicit toolset keys differing from plugin names, and missing ToolRefs use
   the specified fallback. Valid custom summaries are not prefixed or rewritten.
   Malformed arguments, optional targets, cursor pages, and cancellation cannot
   break execution or erase built-in object labels.
4. Summaries perform no I/O; built-in summaries omit content payloads. Existing
   argument masking, input/result isolation, whitespace normalization, and
   bounded previews continue passing.
5. Chat and Script, TTY/non-TTY, parallel lanes, and narrow widths identify the
   same operation. New runtime rows consume persisted summaries; child/handle
   structure, compact timing, errors, and native Flow rows remain correct.
6. Otherwise-supported saved old-name events and tool exchanges remain readable,
   including honor interception, compact, chdir, handoffs, and async result
   reconstruction. Stored names/summaries remain unchanged and no live tools run
   during replay. Fresh executions use only canonical identities.
7. One ToolRef yields the specified ref, model name, and display name, including
   components containing single underscores. Queries/CLI REF fields retain `/`;
   all adapters retain `__`; fallback labels use `.`. Actionable built-in model
   instructions use exact callable names. Dot/slash calls invoke nothing and
   suggest only an exact currently advertised counterpart. Unknown, unavailable,
   runtime-only, and old namespace/leaf names get no suggestion. Failed exchange
   identities and arbitrary prose/code remain unchanged.
8. Fresh and refreshed Setup injects convo configuration; renamed calls receive
   run/thread provenance and retain disabled-teaming errors. Team/list preserve
   their respective directory metadata and visibility; create/rename/join/leave
   preserve their schemas and behavior. List covers absent, unique, duplicate,
   case-sensitive, and invalid name filters without exposing inaccessible entries.
9. Messages/write interpret an agent name as the caller's DM, regardless of a matching
   conversation label. Canonical IDs select exact conversations; failed ID access
   never redirects. Cover reversed DM participants, ID-shaped agent names,
   unknown/self agents, rejected typed/pair targets, and no conversation-name
   lookup. A first write creates the implicit DM once, while read of an absent
   DM creates nothing. Existing DM/GC writes retain replies and receipts.
10. Messages covers metadata-only, default and bounded history, chronological order,
    malformed stored messages, invalid limits, and inaccessible conversations.
    Reads leave messages, delivery checkpoints, and talk-handler execution
    unchanged; subsequent talk delivery still sees unread messages. Write retains
    membership/lease checks, provenance, text-to-body mapping, optional source
    message-ID replies, and unconfirmed-delivery behavior without
    automatic retries. No recv or subscription operation is registered.

Primary risks are missing an identity-dependent dispatch/reader, granting an
external plugin runtime authority, and implying that writing an agent file
publishes or activates State. Conversation tools must also preserve caller-based
DM selection and keep history inspection separate from talk delivery. The tests
above cover these boundaries. Reuse existing tool-description, registry,
context, progress, and execution-history
suites; run all default repository checks for implementation. For this definition,
verify source claims, relative links, catalog coverage, and `git diff --check`.

Open questions: human approval of this naming, wording, representation boundaries,
and no-alias migration policy. Implementation starts only after approval.
