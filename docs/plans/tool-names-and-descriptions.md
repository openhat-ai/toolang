# Tool names and descriptions

Status: proposed; awaiting human approval. Definition only.

## Goal and scope

Give all 49 proposed built-in tools concise progress text, separate tool identity
for UI labels/icons, and consistent model instructions. Rename three toolsets and
simplify conversation discovery, inspection, and writing. Keep resource access,
runtime semantics, CLI commands, tool-name grammar, and teaming service contracts unchanged.

This replaces the relevant naming/progress decisions in
[internal toolsets](internal-toolsets.md), [plugin layout](plugin-listing-visibility.md),
[tool progress](tool-progress.md), and the tool surface in [teaming](teaming.md).
Other contracts remain in force.

## Names and representations

| Current | New | Implementation module |
| --- | --- | --- |
| `me` | `state` | `execution/tools/state/` |
| `_toolang` | `runtime` | `execution/tools/runtime.py` |
| `msg` | `convo` | `plugin/toolsets/convo.py` |

Keep `fs`, `shell`, `web`, `history`, and `service`. Rename owned classes/contexts,
including `ConvoToolset`/`ConvoToolContext`; update registration, configuration,
selectors, dispatch, errors, and instructions together. Provider metadata named
`_toolang` is unrelated and stays unchanged.

| Boundary | Example |
| --- | --- |
| Ref, query, configuration, CLI REF/JSON | `state/get` |
| Adapter/provider tool definition, call, and result | `state__get` |
| Full human identity, prose, tooltips | `state.get` |

Derive all forms from one ToolRef: keep `identity` and `model_name`, add derived
`display_name`, and share formatters where no loaded ref exists. Adapters preserve
supplied names. Add no duplicate registrations or persisted display fields.
Model instructions must use exact advertised call names; ref/query examples use
slash syntax. Never rewrite arbitrary prose, code, or saved exchanges.

Dot/slash calls remain errors. Suggest the exact currently advertised counterpart,
e.g. `Unknown tool name 'state.get'; call 'state__get' instead.` Do not execute,
retry, fuzzy-match, or suggest unavailable, runtime-only, or old names. Preserve
original call names and IDs in failed exchanges.

## Conversation tools

Replace `targets` with `team` and `list`, and `send` with `write`; remove `resolve`
and add `peek`. Shorten the four `*_conversation` names. Eight convo leaves
replace seven messaging leaves; the complete built-in catalog grows from 48 to 49.

| Display signature | Result |
| --- | --- |
| `convo.team()` | `[{id, name, online}]`: agents and humans |
| `convo.list(name: string? = null, members: string[]? = null, exact: boolean = false)` | `[{id, name, members}]` |
| `convo.peek(target: string, limit: integer = 50)` | `{conversation: Conversation, messages: Message[]}` |
| `convo.write(target: string, message: string, reply_to: string? = null)` | `{conversation: string, message: Message}` |
| `convo.create(name = null, participants = null)` | Existing create/reuse result |
| `convo.rename(conversation, name, revision)` | Existing rename result plus nullable `previous_name` |
| `convo.join(conversation)` | Existing join result |
| `convo.leave(conversation)` | Existing leave result |

- **Team:** expose only `id` (canonical participant ID), `name` (its unprefixed
  identifier), and `online` (existing agent presence; null for humans). Agent names
  are valid write targets; human names require a conversation ID. Omit owner,
  display-name metadata, creation time, and lease deadline.
- **List:** expose only `id`, nullable `name`, and `members` (canonical participant
  IDs), mapped from conversation ID/name/participants. Both directories retain
  visibility rules. Omitted/null filters return all accessible conversations;
  combine supplied filters with AND. Name uses existing validation and exact,
  case-sensitive matching. Members accepts bare agent names or canonical
  `agent:`/`human:` IDs; normalize and deduplicate before matching all supplied
  members. Extra members are allowed unless `exact=true`, which compares the full
  set, including the caller; never add the caller implicitly. Order is irrelevant.
  Reject empty member arrays, malformed members, and exact without members;
  unmatched names or valid unknown members return an empty list. No separate
  query or pagination.
- **Target:** peek/write accept a canonical `dm_...`/`gc_...` ID or bare agent
  name. `alice` means the symmetric DM between the context's agent and
  `agent:alice`, regardless of conversation labels. Canonical-ID syntax takes
  precedence; an ID failure never falls back to an agent. ID-shaped agent names
  require their DM ID from list/create. Reject unknown/self agents, typed
  `agent:`/`human:` targets, and participant-pair shorthand. Never resolve a
  conversation name or accept a caller identity parameter.
- **Write:** append once as the current agent. An agent-name target creates/reuses
  the implicit DM; first creation and append remain atomic. An explicit
  conversation ID must exist. Preserve message validation, membership/live-lease
  checks, internal run/thread provenance, receipt validation, and unconfirmed
  delivery behavior; never auto-join, retry, or redirect on failure.
- **Parameters:** plain-text `message` maps to stored `body`; optional `reply_to`
  maps to `in_reply_to`. Its description is "ID of the message being replied to.
  Omit for a new message." Use the original Message UUID, not a conversation or
  stream ID. Only `target` selects the destination. Stored fields stay unchanged.
- **Public messages:** expose only `{id, sender, message, reply_to}` in peek/write,
  mapping stored `body`/`in_reply_to` to the public names. `id` is the existing
  application-generated UUID, independent of the backend and stable across write,
  peek, and reply references. Never expose backend stream IDs/cursors or internal
  origin data in these results or progress; retain backend receipt checks internally.
- **Peek:** return metadata and the latest `limit` retained messages in
  chronological order. Accept integers `0..1000`; zero returns metadata and an
  empty messages array. Decode with the existing Message contract; invalid stored
  entries are errors. Unknown/inaccessible conversations and absent implicit DMs
  are errors, with no creation. Reads never wait, consume, acknowledge, advance
  checkpoints, or trigger the talk handler; talk-thread processing is unchanged.
  No recv, subscription, or pagination.
- **Rename:** read metadata before mutation, require its revision to match the
  supplied revision, then rename with that same revision. Return the captured
  name as `previous_name` for progress; preserve revision conflicts without retries.
- **Ownership:** implement target interpretation, list filtering, and result
  projection in convo after AgentClient validation. Keep `/msg` routes,
  client APIs, storage, and talk delivery unchanged.

Model-call examples: `convo__list` with `{"name":"Development"}`,
`{"members":["alice","bob"]}`, or
`{"members":["alice","bob","human:bryan"],"exact":true}`;
`convo__peek` with `{"target":"alice","limit":20}`;
`convo__write` with `{"target":"alice","message":"Ready"}` or
`{"target":"gc_00000001","message":"Ready"}`.

## Descriptions and progress

Model descriptions start with the action, object, and access scope; retain the
input/result constraints needed for correct use. State edits authored agent files:
writes do not publish State, chores are outside State, and sync does not switch
active Run code. Keep digest, sync, service-session/auth, and runtime restrictions.

Use the existing optional `summary()` hook for action/object/target text only;
keep tool identity and icons outside it. Every built-in supplies lifecycle
summaries; plugins need no new interface or registration metadata. Objects such
as files, messages, and child runs still distinguish leaves within a toolset.
`{...}` is display data; omit the colon with an absent target. Preserve the same
input-derived target across running, success, failure, and cancellation. Result
IDs or resolved labels may supplement that target, never replace it. Keep the target before optional
result details when truncating; retain existing masking and display normalization.

| Tool ref | Running / succeeded sentence | Failed sentence |
| --- | --- | --- |
| `state/list` | `Listing / Listed agent files` | `Failed to list agent files` |
| `state/get` | `Reading / Read agent file: {key}` | `Failed to read agent file: {key}` |
| `state/create` | `Creating / Created agent file: {key}` | `Failed to create agent file: {key}` |
| `state/update` | `Updating / Updated agent file: {key}` | `Failed to update agent file: {key}` |
| `state/delete` | `Deleting / Deleted agent file: {key}` | `Failed to delete agent file: {key}` |
| `state/sync` | `Syncing / Synced agent state` | `Failed to sync agent state` |
| `fs/list` | `Listing / Listed files: {path}` | `Failed to list files: {path}` |
| `fs/read` | `Reading / Read file: {path}` | `Failed to read file: {path}` |
| `fs/write` | `Writing / Wrote file: {path}` | `Failed to write file: {path}` |
| `fs/append` | `Appending to / Appended to file: {path}` | `Failed to append to file: {path}` |
| `fs/glob` | `Matching / Matched files: {pattern} in {path}` | `Failed to match files: {pattern} in {path}` |
| `fs/stat` | `Inspecting / Inspected path: {path}` | `Failed to inspect path: {path}` |
| `fs/mkdir` | `Creating / Created directory: {path}` | `Failed to create directory: {path}` |
| `fs/remove` | `Removing / Removed path: {path}` | `Failed to remove path: {path}` |
| `shell/execute` | `Running / Ran: {command}` | `Failed: {command}` |
| `web/search` | `Searching / Searched: {query}` | `Failed to search: {query}` |
| `history/read_threads` | `Reading / Read threads` | `Failed to read threads` |
| `history/read_runs` | `Reading / Read runs: {thread}` | `Failed to read runs: {thread}` |
| `history/read_steps` | `Reading / Read steps: {run}` | `Failed to read steps: {run}` |
| `history/read_output` | `Reading / Read run output: {run}` | `Failed to read run output: {run}` |
| `runtime/runnables` | `Discovering / Discovered runnables` (or `runnable: {name}`) | `Failed to discover runnables` (or `runnable: {name}`) |
| `runtime/chdir` | `Changing / Changed workdir: {path}` | `Failed to change workdir: {path}` |
| `runtime/run` | `Running / Completed child run: {runnable}` | `Failed to complete child run: {runnable}` |
| `runtime/run` with `async=true` | `Starting / Started child run: {runnable}` | `Failed to start child run: {runnable}` |
| `runtime/spawn` | `Spawning / Spawned new run: {runnable}` | `Failed to spawn new run: {runnable}` |
| `runtime/await` | `Waiting for / Received run result: {target}` | `Failed to receive run result: {target}` |
| `runtime/exec` | `Switching to / Switched to runnable: {runnable}` | `Failed to switch to runnable: {runnable}` |
| `runtime/pick` | `Loading / Loaded guidance: {kind}/{ref}` | `Failed to load guidance: {kind}/{ref}` |
| `runtime/honor` | `Loading / Loaded rules for {paths}` | `Failed to load rules for {paths}` |
| `runtime/compact` | `Compacting / Compacted thread history` | `Failed to compact thread history` |
| `service/start_bridge` | `Starting / Started bridge: {service}` | `Failed to start bridge: {service}` |
| `service/stop_bridge` | `Stopping / Stopped connection: {service}` | `Failed to stop connection: {service}` |
| `service/init` | `Initializing / Initialized session: {service}` | `Failed to initialize session: {service}` |
| `service/start_auth` | `Starting / Started authorization: {service}` | `Failed to start authorization: {service}` |
| `service/complete_auth` | `Completing / Completed authorization: {service}` | `Failed to complete authorization: {service}` |
| `service/list_tools` | `Listing / Listed tools: {service}` | `Failed to list tools: {service}` |
| `service/call_tool` | `Calling / Called tool: {service}/{tool_name}` | `Failed to call tool: {service}/{tool_name}` |
| `service/list_resources` | `Listing / Listed resources: {service}` | `Failed to list resources: {service}` |
| `service/list_resource_templates` | `Listing / Listed resource templates: {service}` | `Failed to list resource templates: {service}` |
| `service/read_resource` | `Reading / Read resource: {service} {resource_uri}` | `Failed to read resource: {service} {resource_uri}` |
| `service/list_prompts` | `Listing / Listed prompts: {service}` | `Failed to list prompts: {service}` |
| `service/get_prompt` | `Getting / Got prompt: {service}/{prompt_name}` | `Failed to get prompt: {service}/{prompt_name}` |
| `convo/team` | `Listing / Listed team members` | `Failed to list team members` |
| `convo/list` | `Listing / Listed conversations: {filter}` | `Failed to list conversations: {filter}` |
| `convo/peek` | `Reading / Read conversation messages: {target}` (`conversation: {target}` when `limit=0`) | `Failed to read conversation messages: {target}` (`conversation: {target}` when `limit=0`) |
| `convo/write` | `Writing / Wrote message: {target}` | `Failed to write message: {target}` |
| `convo/create` | `Creating conversation: {label}` / `Created conversation: {label} ({id})` | `Failed to create conversation: {label}` |
| `convo/rename` | `Renaming conversation: {conversation} → {name}` / `Renamed conversation: {old_name} → {name} ({conversation})` | `Failed to rename conversation: {conversation} → {name}` |
| `convo/join` | `Joining / Joined conversation: {conversation}` | `Failed to join conversation: {conversation}` |
| `convo/leave` | `Leaving / Left conversation: {conversation}` | `Failed to leave conversation: {conversation}` |

Special cases:

- Peek/write always use the input target: writing to `alice` completes as
  `Wrote message: alice`; an explicit conversation ID stays that ID. Receipt IDs
  remain in the result without replacing or repeating the target in progress.
- Create's label combines the supplied name and participants when present, e.g.
  `Development with agent:alice, human:bryan`; keep it on success and append the
  returned ID. Without a label, success uses only the ID; without an ID, retain
  the label; without either, keep the action/object. DM reuse follows the same rule.
- Honor retains input paths as workspace labels in every state; loaded rule files
  stay in the result. Run/spawn retain the runnable, not returned run/thread IDs.
  Exec success means the same-Run switch committed; the caller never resumes.
- List filter text combines supplied name and members, e.g. `Development; with
  alice, bob` or `only alice, bob`; omit it when unfiltered.
- Rename success uses `previous_name` and the requested new name; quote names and
  render null as `unnamed`. Missing/malformed prior-name data falls back to the
  input ID and new name: `Renamed conversation: {conversation} → {name}`. Failure
  uses those same inputs without assuming an old name was retrieved. Keep the ID
  and new name when shortening the optional old name. Formatters perform no reads.
- History cursor pages say `Reading/Read/Failed to read more threads/runs/steps`, never the cursor.
  Stop_bridge covers both stdio shutdown and HTTP session/auth cleanup.
- Failure uses the table's explicit text and input targets, never success-only
  result fields. Keep the ToolResult error boundary, including nested service
  responses and shell exit handling; do not treat arbitrary payload fields as
  execution failure. Error details retain their existing display; do not append
  raw errors to summaries or imply rollback/non-delivery after an uncertain result.
  Cancellation uses `Canceled: <running summary>`, except shell uses
  `Canceled: {command}` through the executor's existing lifecycle formatter.
  Preserve the masked command preview; never use success wording.
- Preserve URI/path labels, command/query quoting, and guidance formatting.
  Summaries use isolated, masked inputs/results and perform no I/O. Omit contents,
  message bodies, nested service inputs, auth URLs/tokens, digests, result dumps,
  and counts. Malformed/missing fields retain the object label or fall back;
  never display `None` or raise into execution.

### Tool identity and rendering

Expose identity, status, and summary separately in presentation: for example,
`{toolset: "shell", tool: "execute", status: "running", summary: "Running: ls"}`.
This is a derived view, not a new event or stored schema. Recover the toolset key
and leaf from the existing encoded `call.name` with shared validated helpers;
use ToolRef where already available. A plugin implementation name is not its
configured toolset key. Preserve unknown/malformed raw names rather than guessing.

Carry optional `tool_identity` (toolset/name) and `tool_status` on progress rows,
including parallel lane/terminal projections. Render the badge separately from
summary text; never parse a summary to recover identity or strip presumed prefixes.
Renderers choose text or icon presentation. CLI defaults to `[shell] Running: ls`;
icon-capable UIs may replace the badge with a mapped icon and retain `shell.execute`
as its accessible label/tooltip. Missing icons and plain-text output use the
toolset text; when identity is unknown, retain the raw call name in the fallback.
Keep status indicators distinct from toolset icons and reserve width for both
identity and target. No new CLI flags or plugin icon requirements in this scope.

### Plugin fallback

Default/missing hooks, None, blank/non-text output, and formatter exceptions use
fallback without affecting execution. Put the leaf name in the fallback summary;
the separately rendered toolset identifies its owner. With no valid identity,
use the original call name. Do not infer sentences from descriptions. Preserve
valid custom summaries verbatim, even when they already mention the toolset.

Every lifecycle includes the first supplied argument: use schema order, falling
back to call input order if no schema property matches. Omit it only for calls
without arguments; never insert defaults. Preserve masking (`<redacted>`),
single-line formatting, and the 80-character preview limit, including previews
for empty strings, null, false, and zero.

| Running | Succeeded | Failed | Canceled |
| --- | --- | --- | --- |
| `Executing list: projects` | `Executed list: projects` | `Failed list: projects` | `Canceled list: projects` |

These summaries render with the separate `acme` badge, e.g.
`[acme] Executed list: projects`. Custom/plugin summaries receive the same UI
identity treatment without requiring summary or icon metadata.

Chat/Script runtime tool rows must use saved summaries, including run/async run,
spawn, await, and exec. Preserve child/handle rows, handoff behavior, markers,
timing, and errors. Native Flow rendering stays unchanged. Preserve legacy saved
summary text and derive identity only from its recorded call; never rewrite old
labels or call live formatters during replay. Add no panels, events, or persistence
fields. Identity decoration belongs to presentation, not saved summary text.

## Authority and migration

`runtime` stays Toolang-owned and selected independently of user tool ceilings.
Reserve both its plugin name and explicit toolset key using distribution provenance;
external plugins cannot acquire runtime authority. Honor/compact stay runtime-only.
State/convo remain ordinary selectable toolsets; existing underscore rules remain.

No old namespace/leaf aliases. Update authored selections, configuration, and calls;
restart hosts and prepare fresh Setup. Split targets consumers into team/list;
replace resolve with list/peek. Send becomes write: `body` → `message`,
`in_reply_to` → `reply_to`, and `agent:alice` → `alice`. Human/pair targets require
an explicit conversation ID from list/create. Management arguments stay unchanged.
Result consumers must use the compact directory/message shapes above; backend
receipt fields are no longer part of the tool result.

Preserve saved names, exchanges, summaries, and plugin rooms; the renamed toolsets
have no durable room data to migrate. Readers still recognize old `_toolang`
operations, including `_toolang__execute`. This permits history reading, not old
calls or resuming pre-upgrade runs. Record the breaking migration through the
repository's changelog runnable; preserve historical changelog entries and plans.

## Implementation and verification

Paths below are relative to `src/toolang`, except `pyproject.toml` and documentation.

| Area | Touchpoints |
| --- | --- |
| Rename and authority | `pyproject.toml`; `execution/tools/{me,_toolang.py}`; `plugin/toolsets/{registry,loading,collections}.py`; setup selection; executor dispatch/control; `execution/assembly/tool_replies.py` |
| Identity and descriptions | `base/utils/{tools,workspace_paths,tool_descriptions}.py`; `plugin/toolsets/{fs,shell,web,history,service,msg}.py`; `execution/{assembly/prompting,executor/steps/tool}.py`; adapter serialization |
| Conversation tools | `plugin/toolsets/msg.py` → `plugin/toolsets/convo.py`; `base/types/tool.py`; `setup/{tools,watcher}.py`; existing teaming clients/schemas; `work/messaging.py` instructions use exact new names and `message`/`reply_to` |
| Rendering and docs | `cli/common/execution_progress/{types,operations,step_projection,projector,state,rich_rendering}.py`; `execution/assembly/prompts/protocol.md`; active docs/examples/config fixtures; generated references via their existing workflow |

| Acceptance scenario | Pass condition |
| --- | --- |
| Registration/authority | Exactly 49 mapped leaves; old names fail, reserved runtime cannot be registered externally, ceilings and runtime-only visibility hold |
| Identity | `/`, `__`, and `.` remain boundary-specific, including single-underscore components; mistaken calls suggest only an exact advertised counterpart and invoke nothing |
| Summaries | Every catalog row matches all lifecycle text, including sync/async run and compact shell cancellation; input targets survive result-ID/resolved-label changes, missing result fields, and narrow rendering; cover agent/ID writes, named/unnamed/participant-based create/reuse, rename fallback, honor paths, and runnable results; ToolResult error boundaries, special cases, masking, isolation, no I/O, malformed-field safety, and no raw-error leaks hold |
| External plugins | Class/function plugins with absent or broken hooks still run; separate identity plus fallback retains owner, leaf, and argument preview across ordering, missing-ref, masking, truncation, and empty values; valid custom wording is untouched |
| Rendering/history | Text/icon and missing-icon fallback retain tool identity/status independently of summary; configured aliases never use plugin names as labels; Chat/Script, TTY/non-TTY, parallel/narrow layouts preserve metadata, hierarchy, and saved wording; old runtime records replay correctly without formatters or record rewrites |
| Discovery/setup | Fresh/refreshed Setup and disabled-teaming behavior work; directory access holds; team exposes only id/name/online with agent/human presence semantics; list exposes only id/name/members; name filters cover zero/duplicate/case/invalid inputs; member filters cover contains/exact, name combinations, normalized duplicates/order, explicit self, unknown/invalid/empty members, and exact without members |
| Conversation management | Create/reuse reports Created; rename success shows the verified old and requested new names, including setting/clearing names and no-op renames; read failure or stale/concurrent revisions cannot produce a false success or wrong old name; malformed summary data falls back safely; join/leave behavior is unchanged |
| Message targeting | Caller-based DM derivation works in either participant order; labels never route messages; ID-shaped/unknown/self/typed/pair targets follow the contract; first write creates once, absent-DM read creates nothing |
| Inspection/delivery | Metadata-only/default/bounded histories preserve order; bad limits/records and denied access fail; reads leave talk checkpoints/delivery untouched; writes preserve parameter mapping, replies, internal provenance, receipt checks, and failure behavior; public results expose only the specified fields and stable logical message IDs |

Main risks are missed identity-dependent dispatch/history paths, accidental runtime
authority, confusing source edits with State publication, and coupling history
inspection to talk delivery. Reuse existing registry, summary, progress, history,
and messaging suites; run all default checks for implementation. For this document,
verify source claims, links/examples, catalog coverage, and `git diff --check`.

Open question: final human approval of this definition before implementation.
