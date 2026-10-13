# Tool Runtime

Toolang exposes tools through the toolset plugin family.

Tools execute inside normal runs and are recorded as `tool` Steps.
`Tool.invoke()` is asynchronous. Function-tool wrappers await native
async callables and isolate synchronous Python callables in a worker thread,
so blocking tool implementations do not stall the run event loop.


## Tool identity

A tool has one toolset and leaf name. Queries and configuration use `toolset/tool`,
provider calls use `toolset__tool`, and human labels use `toolset.tool`. For example:
`convo/write`, `convo__write`, and `convo.write`. Components can contain single
underscores; `__` is reserved as the provider separator. `ToolRef.display_name`
derives the display form without storing another identity.

A mistaken dot/slash model call fails and suggests only its exact advertised
provider name. It never executes or retries the call. Runtime-only, unavailable,
and old tool names get no executable aliases.

## Inspection

`too [AGENT] tools [--all] [--query QUERY]` lists the effective tools published
by setup. With no agent it uses root configuration only, never the implicit
`default` agent. With an agent it overlays that agent's plugin configuration
and allow settings on root inputs. The agent need not be running, and its
program is not parsed for this command. This describes setup-level availability;
individual run resource declarations may narrow it further.

The default list hides internal tools such as `runtime/*`. `--all` reveals
those tools and all allow-excluded tools from the same setup version, matching
the diagnostic meaning of model/provider `--all`. `setup.tools()` remains the
allow-filtered runtime collection; `setup.tools(all=True)` exposes the complete
pre-allow set for inspection without changing run grants. Tool and toolset data
are loaded on first accessor use and memoized within that setup revision.
`state/*` follows normal allow policy in the default view and appears with `--all`.
Queries and footer counts use the displayed dataset. Tools have no separate
readiness protocol: the full view includes leaves supplied by loaded toolsets,
not guessed tools from an unloadable plugin. Both views show `REF`, `DESCRIPTION`, and `TAGS`, taken directly from the
record fields `ref`, `description`, and `tags`. Tags are `ready` or `not_allowed`; internal tools retain
copyable `runtime/name` refs. `--json` emits the same public records used for
native TQ matching. `--human` explicitly selects the default table; the flags
cannot combine. See [Resource Queries](queries.md) for fields and syntax.
Human summaries use `N tools, M toolsets`, omitting the toolset count for zero
or one tool. Empty human results print `0 tools`; empty JSON is `[]`.
`-a` is an alias for `--all`.
`too toolsets [--all]` instead lists locally installed toolset plugins, without
agent configuration, policy, or factory loading.


## Built-In Tool Families

Current built-in tools are:

- `fs`
- `shell`
- `web`
- `service`
- `state`
- `convo`
- `history`
- `runtime`

All use the same plugin registration and invocation path. `runtime` is a runtime
toolset; user resource selectors apply only to user tools.


## Filesystem

`fs` operates inside the workspace list available to the Run. It provides
`read`, `write`, `append`, `list`, `glob`, `stat`, `mkdir`, and `remove`.
Each operation takes a `path` (default `"."` for `list` and `glob`), with no
`workspace` or `cwd` argument. Paths are absolute within an authorized workspace,
relative to the current workdir, or workspace-qualified as `name://path`, where `name` is an available workspace;
`name://` denotes that workspace's root. Relative paths cannot leave their
workspace.

The runtime sends `<toolang:workspace list="lab,repo1"/>` and the current
`<toolang:workdir path="repo1://src"/>` on every Model Call. The list contains
currently usable workspaces, with implicit `lab` first and configured workspaces
in configuration order. `lab` is rooted at `<agent home>/lab` and is not a config
grant. The last listed name is the runtime default.

## Shell

`shell.execute(command, timeout_sec=..., max_output_chars=...)` starts in the
current workdir; `cwd` and `workspace` arguments are not accepted. The result
includes `cwd` (the workdir path), `stdout`, `stderr`, and `exit_code`. An
in-command `cd` affects only that subprocess.
The command text is interpreted by the shell and OS, not Toolang's path resolver;
without an OS sandbox, a command can access paths outside the workspaces.
Guest sandboxes mount available workspaces at startup; a State change granting a
new workspace or remapping an existing one cannot use a missing mount until the
sandbox is restarted with that binding.


## Web Search

`web` returns structured search results for model use.


## Service

`service` exposes visible service caps as callable tools.

It is the bridge between:

- service cap definitions
- runtime tool execution

Service calls return structured input and output and are recorded as normal
tool-call steps.

Its leaf tools are `start_bridge`, `stop_bridge`, `init`, `start_auth`,
`complete_auth`, `list_tools`, `call_tool`, `list_resources`,
`list_resource_templates`, `read_resource`, `list_prompts`, and `get_prompt`.


## History

`history` reads the current agent's durable records through ordinary, selectable
user tools. It creates no controls, recalls, or compaction and does not rebuild
ModelCalls.

```text
history/read_threads(limit=20)
history/read_runs(thread?, begin?, end?, limit=20, from_end=false)
history/read_steps(run, begin?, end?, limit=20, from_end=false)
history/read_output(run)
```

Thread defaults to the caller's; Run must be supplied. Bounds are Run references
for `read_runs` and Step references for `read_steps`, covering `[begin, end)`.
`limit` counts primary records, not tokens or dependencies. Tail reads select
from the end while each page remains naturally ordered. Threads are ordered by
updated time descending, then ID ascending; Steps use numeric order.

List results contain `threads`, `runs`, or `entries` and a nullable `cursor`.
Run pages also identify the logical `thread` and captured `head`; records retain
physical ownership. Only root Run records are read and captured by Run cursors;
child updates alone do not invalidate these pages.
Step pages contain their `run` and control `dependencies`,
which may recur. Unbounded Step reads include unused owned controls; bounded
reads include only selected Steps and their dependencies. Child internals require
an explicit child Run read. Pages may split tool exchanges.

Continue using the same tool with **only** `cursor`. Membership stays fixed
across append, rewind, and restart; changes to captured Run/Step/control facts
invalidate continuation. Thread-list metadata is current as of each page.
Cursors belong to the current agent Store and tool. A captured running Step
finishing also invalidates continuation; bound reads before active Steps when
paging stable history within an active Run.

Step outputs and control inputs are resolved typed values; structural and saved
ModelCall references remain intact. `read_output` returns `{run, status, output}`,
where output is `{type, value, binding}` or null, including partial output.
The type describes the complete value; binding is a local name or null.
Missing targets and unresolved values fail as ordinary tool errors.


## Current Agent

`state` manages the current agent's latest home files and synchronizes tracked
root/home sources with published Agent State, subject to normal tool permissions.
The executor supplies that home and its host's State synchronization service.

```text
state__list()
state__get(key)
state__create(key, content, encoding="utf-8")
state__update(key, content, if_digest, encoding="utf-8")
state__delete(key, if_digest)
state__sync()
```

Keys are canonical paths relative to the current home:

| Files | Operations |
| --- | --- |
| `agent.too`, `config.toml` | Full-file CRUD |
| `runnables/<name>.too` | Full-file CRUD for agic or flow modules |
| `psyches/<name>.md`, `services/<name>.md`, `prompts/<name>.md` | Full-file CRUD |
| `skills/<name>/SKILL.md`, `skills/<name>/assets/**` | Full-file CRUD |
| `chores/<name>.md` | Full-file CRUD on current chore definitions |

Use the exact spelling of existing file and directory names, as returned by
`list`. Aliases with different casing are rejected as `invalid_request` on
case-insensitive filesystems, keeping receipts consistent with State file keys.

Successful results are flat objects:

| Operation | Result |
| --- | --- |
| `list` | `{files: [{key, digest, bytes}]}`, sorted by key |
| `get` | `{key, digest, bytes, content, encoding}` |
| `create`, `update` | `{key, digest}` |
| `delete` | `{key, digest: null}` |
| `sync` | `{revision, files: [{scope, key, observed_digest, accepted_digest, code?, message?, line?}]}` |

Text is UTF-8; non-UTF-8 files are returned as base64. Create/update accept a
complete content string; `encoding="base64"` supplies exact binary bytes.
Each file operation addresses a complete file. The write result itself is a
receipt; identical updates return the same receipt without rewriting the file.

All digests are lowercase SHA-256 of exact file bytes, including comments, front
matter, whitespace, and original line endings. Update/delete require `if_digest`
from a read or successful write and check it under the owning lock. On
`digest_mismatch`, get the latest file and reconcile before retrying. Create fails
if a target already exists. The receipt identifies the resulting bytes; a null
digest identifies the requested absence of a file.

State file operations do not parse or validate content. A successful save confirms that
the bytes were written. Existing loaders/watchers handle syntax, metadata, and
composition errors identically for state writes and direct filesystem edits. The
State watcher excludes invalid resource units and publishes valid siblings,
with diagnostics on the affected source records. Fatal shared-config or storage
failures retain the last published State. Repairing a resource restores its
availability. Reads never allocate chore IDs. File-operation failures return `{error, message, key?}`, where
`error` is a stable code and `key` is included when known. Successful results
never contain `error`.

| Error | Meaning |
| --- | --- |
| `invalid_request` | Invalid arguments, path, or content encoding |
| `not_found` | Get/update/delete target does not exist |
| `already_exists` | Create target already exists |
| `digest_mismatch` | Update/delete observed different file bytes |
| `io_error` | A filesystem or safe-path access operation failed |

Digest conflicts also return `expected_digest` and `actual_digest`. No failure
returns a successful receipt. A list failure returns an error rather than a
partial file list.

Delete removes exactly one file. Deleting SKILL.md leaves assets in place; deleting
a chore preserves its checkpoint and does not cancel an existing Run.
Root resources, external cap content, arbitrary paths, and directory
operations are excluded. Symlinks are rejected except the canonical roaming
`agent.too` link to its own original script; that link is preserved when editing.
Linked config files are not writable through state.

Main operations use `.agent.too.lock`; runnable modules use `.runnables.lock`.
Config writers, including configured caps, workspaces, and roaming projection, share
`.config.toml.lock`; caps/assets use `.caps.lock`, and chores use `.chores.lock`.
Locks coordinate participating writers; filesystem edits that bypass these locks
can still race a save.

`sync()` accepts no arguments and waits for one serialized State refresh. Finish
all source writes first and ensure no program, agent, editor, or background writer
modifies tracked sources until sync returns. Concurrent readers and the watcher
may continue. The operation adds no preliminary scan, final verification, or
retry loop; existing preparation snapshot protections remain in place.

Success returns `{revision, files}` with records sorted by `(scope, key)`,
including valid shadowed inputs, raw config, skill assets, and home Markdown
chores. Each record contains `scope`, `key`, `observed_digest`, and
`accepted_digest`. Both digests identify exact raw authored bytes.

An invalid readable file has an observed digest and null accepted digest;
`code`, `message`, and optional `line` appear directly on that record. Unreadable
files/directories have null digests and a read diagnostic. No diagnostic objects,
separate differences lists, or status fields are returned. Deleted/untracked
files are absent. Identical checks return the existing revision. Publication
success does not mean every file was accepted; the tool summary reports the
excluded-source count when nonzero.

Fatal publication failures set the tool error and return:

```json
{"error": "state_rejected", "message": "<preparation error>", "revision": "<last-published-state-sha256>", "files": []}
```

`state_rejected` means a fatal preparation failure; `io_error` means required
root/home or persistence access failed; `sync_unavailable` means the host has
no synchronization service. The revision is null when none has been published.
No candidate acceptance receipt is issued on failure. Malformed arguments use
`invalid_request` without refreshing. Raw file operations can still read,
replace, or delete malformed source without parsing it.

Sync does not replace active code, captured Setup, or the current model-call
snapshot. It confirms input inclusion, not runtime success or effective selection
of every declaration. A child may edit, sync, and return so the root can self-exec
into compatible code. Keep sources stable through exec acceptance if it must use
that publication; the returned revision is not reserved. See [Agent State](agent-state.md).

Use `state.sync()` / `state__sync({})` for publication receipts; handle operational
errors and inspect `revision` and the scoped `files` manifest. There is no loaded
receipt-polling operation. Historical tool-call records remain readable.

Reading after a save observes the saved source. Running code remains governed by
Run binding and publication: static calls in an accepted flow use its bound
program; a later permitted named Run can use the latest published State. Editing
config does not install new tools, refresh captured Setup, or grant authority to
the active Run.


## Runtime Rule

Tools do not own the model loop.

For tool-capable agic calls, the executor supplies callable `runtime` operations:
`runnables`, `run`, `spawn`, `await`, `exec`, `pick`, and `chdir`. `honor` and
`compact` are automatic runtime-only preflights. Flow evaluators, output repair,
and tool-disabled models do not receive runtime tools.

Run, spawn, and exec accept `runnable` and optional `input`. A named invocation
without further requested work uses exec; use run when its result is needed
before continuing. Current `routes` declarations govern hands (run), handoffs
(exec), and spawns (spawn), independently. Missing restrictions mean all visible
targets; `none` disables the operation. Invocation guards still apply. See the
[runtime protocol](../src/toolang/execution/assembly/prompts/protocol.md).

`AgentSetup.tools()` retains registered runtime tools independently of user tool
ceilings. Each invocation has an ordinary Tool Step. Trusted runtime tools receive
per-call operations through `RuntimeToolContext.runtime`, not the Store or executor.
Run creates a child owned by its Tool Step and waits for it to finish. The single
tool reply returns `{type, value}` on success or `ToolResultPart.error` on failure
or child-only cancellation.
Exec returns `{controls: [ControlRef]}` and finishes its Tool Step before
transferring execution. Pick, honor, and compact return summaries of durably created or reused
controls; recalled content remains in controls, not the result summaries.
Exec never resumes the caller after commitment, even if the target fails,
and does not change the default runnable for future chat turns.

`ToolStepGiven.trigger` records `model` or `runtime`. Both have durable results and
progress events; only model-triggered calls contribute their own ToolResult messages.
Failures use `ToolResultPart.error`, with additional diagnostics in the output.

Toolang runtime owns:

- when tools are available
- when a tool is executed
- how tool output re-enters the run
- how tool calls are recorded and exposed
- the default human-readable summary for each tool-call lifecycle state

Leaf tools may provide `summary(arguments, result=None) -> str | None`
through the [plugin contract](plugins.md). The executor supplies isolated
call/result data with sensitive arguments masked. Missing, empty, or failed
summaries fall back to generic wording. No result means running; `ToolResult.error`
distinguishes failure from success. Cancellation uses executor wording.
The running summary is stored in
`ToolStepGiven.summary`; the terminal summary uses `ToolStepNoted.summary`.

The fallback retains the leaf and first supplied argument: `Executing list:
projects`, `Executed list: projects`, `Failed list: projects`, or `Canceled list:
projects`. Schema property order takes precedence when any supplied key matches;
otherwise input order applies. Defaults are not invented. Empty, null, false,
and zero values remain visible; previews are masked, single-line, and at most
80 characters. Missing, blank, nontext, or throwing hooks use this fallback.
Valid custom wording is preserved.

Progress carries toolset/leaf identity and status separately from saved wording,
including parallel lanes. Script and Chat add a textual `[toolset]` prefix,
using the configured toolset rather than its implementation plugin name.
No icon configuration is provided. Unknown call names remain raw. Replay reads
saved summaries and call identities without invoking current formatters.

Built-in summaries preserve the input target through success, failure, and
cancellation. Shell uses `Running: command`, `Ran: command`, `Failed: command`,
and `Canceled: command`; other custom summaries cancel as `Canceled: <running>`.
State sync says `Synced agent state`. Honor describes input paths as `Loaded rules
for repo://src/file`; pick says `Loaded guidance: skill/name`. Tool traces retain
an indented error line on failure and no result blocks. Run/exec preserve their
child/handoff hierarchy and their saved action descriptions. Compact retains
its elapsed timer. Flow descriptions are unchanged.

### Spawn independent work

`runtime/spawn({runnable, input?})` starts an independent root in a new empty
`work_` thread under the same agent/executor. It shares run's input decoder and
follows the current spawns route. It accepts no thread,
identity, source-code, or execution-configuration arguments. Multiple spawn calls
may share a tool batch; each commits independently.

The response is the admission snapshot `{id, thread, status: "pending"}` stored
in the Tool Step. Use `id` with existing history/output tools to inspect progress
and results. Spawn waits for no result and
injects no completion message. Work survives the source Run, but executor shutdown
cancels it. In a short-lived script host, returning from the script stops unfinished
roots. See [spawn syntax](flow-syntax.md#spawn) and
[record semantics](run-step-records.md#spawn-admission-and-handles).

### Pick guidance

`runtime/pick({kind: "skill" | "service", ref: "<trigger ref>"})` recalls one
allowed resource's body. Use the exact ref from its `toolang:skill-trigger` or
`toolang:service-trigger`, such as `skill/testing`, not a source path or selector.
Pick neither grants tools nor connects, authenticates, or discovers MCP services.

The Tool Step returns `{controls: [{ref, target, revision}]}`, where target is
`{kind: "skill" | "service", ref}`. An applied recall control holds
the target, definition revision, and original recalled text; the next Model Call
adopts it as a separate `toolang:skill-guidance` or `toolang:service-guidance`
user message. The revision hashes the definition and metadata, not just the
guidance body. Failures create no recall. Revision zero (`"0"`) is an internal
tombstone rendered as `removed="true"`; a present empty body retains its nonzero
hash. Other revisions use 64 lowercase hexadecimal digits.

Repeated picks reuse the latest matching unadopted recall in the same Run. If
nothing is pending and the last visible revision matches, the receipt is empty.
Visibility comes from the committed messages' tag and recall ref/revision, never
from XML matching, far summaries, triggers, or raw control existence. Trigger and
guidance share a ref but have separate visibility. Changed definitions retract
old guidance. Content that leaves the view must be picked again; assembly does
not restore it. History recovers visible revisions from saved message deltas
without reading historical State.

### Honor workspace rules

For model calls to path-aware tools, runtime checks applicable `AGENTS.md` files
from the workspace root to the target's scope. Missing or changed rules cause a
runtime-only `runtime/honor` Step, followed by an error-only response to the
original tool call: `Workspace rules were just loaded. This operation was not
executed; please retry if it complies with them.` The operation has no Tool Step
or execution events; it has not run. The next Model Call
receives separate `<rules>` user messages after the complete tool exchange.
Only a subsequent retry may execute the operation; current visible rules need
no honor Step. Honor shares pick's revision, pending-reuse, and visibility rules.
Confirmed deletion retracts earlier rules; failed reads block the operation.

Honor is registered normally but is neither advertised to nor callable by the
model. Its result contains `{controls: [{ref, target, revision}]}`; target is
`{kind: "rules", workspace, path}` with the exact rules file, such as
`/src/AGENTS.md`, not its directory scope. Progress preserves the input paths being checked. Honor's
`Step.input` references the original ToolCall, allowing history to reconstruct
its response if no later ModelCall delta saved it. Honor's own result is durable
but not a model message. Preparation and
execution use the same authorized paths. Initial coverage is explicit fs paths
and shell cwd, including reads; paths hidden in shell commands are not inspected.

Compact likewise stays out of model messages. Its result is
`{controls: [{ref, horizon}]}`, referencing the compact Run output; the compact
control changes the horizon used by subsequent ModelCalls.

## Async Runtime Calls

`runtime/run` accepts an optional boolean `async`, defaulting to false. With true,
it admits an owned child and returns `{id, thread, status}` immediately. Use
`runtime/await` with `{target: "run_id"}` for that Run's complete `{type, value}`
result. The target must have been admitted by the calling Run, including through
spawn. Await creates no new execution, accepts one reference, and reports target
failure/cancellation as a tool error. An absent result produces the empty reply.

Flow and agic share admission, ownership, and waiting services. Python `ToolRuntime`
implementations provide `run(..., asynchronous=False)` and `await_target(target)`.
Background children are canceled/drained when their parent ends or executes a
handoff; spawned roots remain independent. Waiting itself never adopts ownership.

## Conversations

The selectable `convo` toolset provides `team`, `list`, `peek`, `write`, `create`,
`rename`, `join`, and `leave`. See [messaging](messaging.md#agent-messaging)
for parameters, compact results, target resolution, and read/write semantics.

## Migration

Replace `me` with `state`, `_toolang` with `runtime`, and `msg` with `convo` in
selections, plugin configuration, and calls. Split targets into team/list, replace
resolve with list/peek, and rename send to write (`body` → `message`,
`in_reply_to` → `reply_to`, `agent:alice` → `alice`). Management leaves become
create/rename/join/leave. Human and pair destinations require a conversation ID.
Restart hosts and prepare fresh Setup; do not resume pre-upgrade runs. Existing
records, summaries, and plugin rooms remain untouched, and history still recognizes
old runtime names, including `_toolang__execute`.
