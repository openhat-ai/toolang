# Tool names and descriptions

Status: proposed; awaiting human approval. Definition only.

## Goal and scope

Give all 49 proposed built-in tools concise, distinguishable progress text and
consistent model instructions. Rename three toolsets and simplify conversation
discovery, inspection, and writing. Keep resource access, runtime semantics,
CLI commands, tool-name grammar, and teaming service contracts unchanged.

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
| Human label, prose, progress fallback | `state.get` |

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
| `convo.team()` | Existing `TeamMember[]`: agents and humans, including presence |
| `convo.list(name: string? = null)` | `[{id, name, members}]` |
| `convo.peek(target: string, limit: integer = 50)` | `{conversation: Conversation, messages: Message[]}` |
| `convo.write(target: string, message: string, reply_to: string? = null)` | `{conversation: string, message: Message}` |
| `convo.create(name = null, participants = null)` | Existing create/reuse result |
| `convo.rename(conversation, name, revision)` | Existing rename result |
| `convo.join(conversation)` | Existing join result |
| `convo.leave(conversation)` | Existing leave result |

- **Discovery:** team preserves existing member metadata; both directories retain
  visibility rules. List returns only `id`, nullable `name`, and `members`
  (canonical participant IDs), mapped from conversation ID/name/participants. Its
  omitted/null name returns all accessible conversations; a supplied name uses
  existing validation and exact, case-sensitive matching, returning all matches
  or an empty list. No separate query, extra filters, or pagination.
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
- **Ownership:** implement target interpretation, list filtering, and result
  projection in convo after AgentClient validation. Keep `/msg` routes,
  client APIs, storage, and talk delivery unchanged.

Model-call examples: `convo__list` with `{"name":"Development"}`;
`convo__peek` with `{"target":"alice","limit":20}`;
`convo__write` with `{"target":"alice","message":"Ready"}` or
`{"target":"gc_00000001","message":"Ready"}`.

## Descriptions and progress

Model descriptions start with the action, object, and access scope; retain the
input/result constraints needed for correct use. State edits authored agent files:
writes do not publish State, chores are outside State, and sync does not switch
active Run code. Keep digest, sync, service-session/auth, and runtime restrictions.

Use the existing optional `summary()` hook. Every built-in supplies lifecycle
summaries; plugins need no new interface or registration metadata. Normal summaries
use action plus explicit object, without a tool-name prefix. `{...}` is display
data; omit the colon with an absent target.

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
| `convo/peek` | `Reading / Read conversation messages: {target}` (`conversation: {target}` when `limit=0`) |
| `convo/write` | `Writing message: {target}` / `Wrote message: {conversation}` |
| `convo/create` | `Creating conversation: {name}` / `Opened conversation: {id}` |
| `convo/rename` | `Renaming / Renamed conversation: {conversation}` |
| `convo/join` | `Joining / Joined conversation: {conversation}` |
| `convo/leave` | `Leaving / Left conversation: {conversation}` |

Special cases:

- Create may reuse a DM: success uses its returned ID, then supplied name, then
  no target. Write success uses the receipt's conversation ID, then input target;
  peek/write running and failure text uses input target. List may return zero
  matches. Exec success means the same-Run switch committed; the caller never resumes.
- History cursor pages say `Reading/Read more threads/runs/steps`, never the cursor.
  Stop_bridge covers both stdio shutdown and HTTP session/auth cleanup.
- Failure uses `Failed to <base action> <object/target>`; cancellation keeps
  `Canceled: <running summary>`. Keep the ToolResult error boundary, including
  nested service responses and shell exit handling. Failed sync never claims success.
- Preserve URI/path labels, command/query quoting, and guidance/rules formatting.
  Summaries use isolated, masked inputs/results and perform no I/O. Omit contents,
  message bodies, nested service inputs, auth URLs/tokens, digests, result dumps,
  and counts. Malformed/missing fields retain the object label or fall back;
  never display `None` or raise into execution.

### Plugin fallback and rendering

Default/missing hooks, None, blank/non-text output, and formatter exceptions use
fallback without affecting execution. Use ToolRef's dotted display name, else
format a valid encoded call name, else retain the original name. Do not infer
sentences from tool names/descriptions or prefix valid custom summaries.

Every lifecycle includes the first supplied argument: use schema order, falling
back to call input order if no schema property matches. Omit it only for calls
without arguments; never insert defaults. Preserve masking (`<redacted>`),
single-line formatting, and the 80-character preview limit, including previews
for empty strings, null, false, and zero.

| Running | Succeeded | Failed | Canceled |
| --- | --- | --- | --- |
| `Executing acme.list projects` | `Executed acme.list projects` | `Failed acme.list projects` | `Canceled acme.list projects` |

Chat/Script runtime tool rows must use saved summaries, including run/async run,
spawn, await, and exec. Preserve child/handle rows, handoff behavior, markers,
timing, and errors. Native Flow and legacy rendering stay unchanged. Truncate
after the leading action/object; add no panels, events, or persistence fields.
Replay never calls live formatters.

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
| Rendering and docs | `cli/common/execution_progress/{operations,step_projection}.py`; `execution/assembly/prompts/protocol.md`; active docs/examples/config fixtures; generated references via their existing workflow |

| Acceptance scenario | Pass condition |
| --- | --- |
| Registration/authority | Exactly 49 mapped leaves; old names fail, reserved runtime cannot be registered externally, ceilings and runtime-only visibility hold |
| Identity | `/`, `__`, and `.` remain boundary-specific, including single-underscore components; mistaken calls suggest only an exact advertised counterpart and invoke nothing |
| Summaries | Catalog coverage for running/success/failure; table and special cases match; masked isolated data, no I/O or payload leaks; malformed fields/cancellation remain safe |
| External plugins | Class/function plugins with absent or broken hooks still run; all fallback states retain identity and argument preview across ordering, missing-ref, masking, truncation, and empty-value cases |
| Rendering/history | Chat/Script, TTY/non-TTY, parallel/narrow layouts preserve hierarchy and saved wording; old runtime records replay correctly without formatters or record rewrites |
| Discovery/setup | Fresh/refreshed Setup and disabled-teaming behavior work; team metadata and directory access hold; list exposes exactly id/name/members; name filtering covers zero/duplicate/case/invalid inputs; management behavior is unchanged |
| Message targeting | Caller-based DM derivation works in either participant order; labels never route messages; ID-shaped/unknown/self/typed/pair targets follow the contract; first write creates once, absent-DM read creates nothing |
| Inspection/delivery | Metadata-only/default/bounded histories preserve order; bad limits/records and denied access fail; reads leave talk checkpoints/delivery untouched; writes preserve parameter mapping, replies, internal provenance, receipt checks, and failure behavior; public results expose only the specified fields and stable logical message IDs |

Main risks are missed identity-dependent dispatch/history paths, accidental runtime
authority, confusing source edits with State publication, and coupling history
inspection to talk delivery. Reuse existing registry, summary, progress, history,
and messaging suites; run all default checks for implementation. For this document,
verify source claims, links/examples, catalog coverage, and `git diff --check`.

Open question: final human approval of this definition before implementation.
