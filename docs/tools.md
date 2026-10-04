# Tool Runtime

Toolang exposes tools through the toolset plugin family.

Tools execute inside normal runs and are recorded as `tool` Steps.
`Tool.invoke()` is asynchronous. Function-tool wrappers await native
async callables and isolate synchronous Python callables in a worker thread,
so blocking tool implementations do not stall the run event loop.


## Inspection

Tool inspection uses the root or selected-agent Setup without parsing its program
or requiring a running server. Setup lazily publishes an allow-filtered tool view;
Run directives may narrow it. Tools have no separate readiness protocol: only
leaves from successfully loaded toolsets exist. Internal `_toolang/*` tools are
hidden by default; `me/*` follows normal allow policy.

See [query projections](queries.md) for records and `--all`, and [CLI inventories](cli.md)
for the distinction between effective tools and installed toolset entry points.

## Built-In Tool Families

Current built-in tools are:

- `fs`
- `shell`
- `web`
- `service`
- `me`
- `history`
- `_toolang`

All use the same plugin registration and invocation path. `_toolang` is a runtime
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
where output is `{local: {type, value, dim}, binding}` or null, including partial
output. This is the protocol projection described in [records](run-step-records.md).
Missing targets and unresolved values fail as ordinary tool errors.


## Current Agent

`me` manages the current agent's latest home files and compares saved file
versions with the State loaded by the calling Run, subject to normal tool
permissions. The executor supplies that home and captured State. File operations
read current disk content; `loaded` reads only the captured State's file list.

```text
me__list()
me__get(key)
me__create(key, content, encoding="utf-8")
me__update(key, content, if_digest, encoding="utf-8")
me__delete(key, if_digest)
me__loaded(receipts)
```

Keys are canonical paths relative to the current home:

| Files | Operations |
| --- | --- |
| `agent.too`, `config.toml` | Full-file CRUD |
| `flows/<name>.too` | Full-file CRUD |
| `psyches/<name>.md`, `services/<name>.md`, `prompts/<name>.md` | Full-file CRUD |
| `skills/<name>/SKILL.md`, `skills/<name>/assets/**` | Full-file CRUD |
| `tasks/<name>.md`, `chores/<name>.md` | Full-file CRUD on ready job files |

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
| `loaded` | `{loaded, revision, mismatches: [{key, digest}]}` |

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

Me does not parse or validate file content. A successful save only confirms that
the bytes were written. Existing loaders/watchers handle syntax, metadata, and
composition errors identically for me writes and direct filesystem edits. The
State watcher retains its last valid publication and reports diagnostics for
rejected candidates; repairing the files allows a later refresh to publish.
Reads never allocate job ids. Failures return `{error, message, key?}`, where
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
a task/chore does not archive it or cancel an existing Run. Draft/archive job
storage, root resources, external cap content, arbitrary paths, and directory
operations are excluded. Symlinks are rejected except the canonical roaming
`agent.too` link to its own original script; that link is preserved when editing.
Linked config files are not writable through me.

Main operations use `.agent.too.lock`; flows use `.flows.lock`. Config writers,
including configured caps, workspaces, and roaming projection, share
`.config.toml.lock`; caps/assets use `.caps.lock`, and jobs use `.jobs.lock`.
Locks coordinate participating writers; filesystem edits that bypass these locks
can still race a save.

`loaded` accepts an array of `{key, digest}` receipts with unique home-relative
keys. It compares all receipts with the same State bound to the current call and
returns that State's `revision`. Only mismatches are returned, in input order;
their `digest` is the loaded value, not the expected value from the input.
Empty input matches. Missing or untracked keys have digest `null`, including
independent task/chore files. A null receipt matches a missing entry even if a
same-named root file exists. For example:

```json
{
  "loaded": false,
  "revision": "<loaded-state-sha256>",
  "mismatches": [{"key": "tasks/example.md", "digest": null}]
}
```

`loaded: false` is a successful comparison, not an error. The operation does not
read disk, select the latest publication, refresh, wait, or switch the Run's
State. Matching proves inclusion in the source file list, including shadowed
inputs; it does not prove that each declaration is effective or that Setup or a
scheduled job adopted a change. Historical manifests without raw config hashes
treat that config as untracked. See [Agent State](agent-state.md).

Reading after a save observes the saved source. Running code remains governed by
Run binding and publication: the accepted flow retains its code and caller contract, while each new named
call binds the latest compatible publication. Resource selectors evaluate current
State at model-call boundaries, within existing authority ceilings; Setup and
workspace grants remain captured. See [program binding](program.md#directives).


## Runtime Rule

Tools do not own the model loop.

For ordinary tool-capable Agic calls, the executor advertises `_toolang/run`,
`_toolang/exec`, `_toolang/pick`, and `_toolang/chdir`. Honor and compact are
registered runtime-only helpers. Statement-generated evaluators, output-repair
calls and tool-disabled models receive no runtime tools.

[Program directives](program.md#directives) own hands/handoffs authorization and
model guidance. Runnable snapshots are bounded to 64 unique targets and 32 KiB;
narrow hands/handoffs if that bound is exceeded.

`AgentSetup.tools()` retains registered runtime tools independently of user tool
ceilings. Each invocation has an ordinary Tool Step. Trusted runtime tools receive
per-call operations through `RuntimeToolContext.runtime`, not the Store or executor.
Run creates a child owned by its Tool Step and returns a scheduling receipt with
`run_id` and `controls`. A separate runtime message delivers its status and, on
success, output type and content before the caller continues.
Execute returns `{controls: [ControlRef]}` and finishes its Tool Step before
transferring execution. Pick, honor, and compact return summaries of durably created or reused
controls; recalled content remains in controls, not the result summaries.
Execute never resumes the caller after commitment, even if the target fails,
and does not change the default runnable for future chat turns.

`ToolStepGiven.trigger` records `model` or `runtime`. Both have durable results and
progress events; only model-triggered calls contribute their own ToolResult messages.
Failures use `ToolResultPart.error`, with additional diagnostics in the output.

The executor owns availability, invocation, history and tool-result delivery.
Leaf [summary hooks](plugins.md#toolset) supply wording; it records running text in
`ToolStepGiven.summary` and terminal text in `ToolStepNoted.summary`, including
safe fallback wording. [Presentation](execution-presentation.md) reads those
saved summaries and owns markers, timing, truncation and hierarchy. It never
calls a plugin while rendering.

Workspace summary paths use `name://path`. Guidance summaries use the effective
cap identity, such as `skill/testing`, rather than its source location.

### Pick guidance

`_toolang/pick({kind: "skill" | "service", ref: "<trigger ref>"})` recalls one
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
runtime-only `_toolang/honor` Step, followed by an error-only response to the
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
`/src/AGENTS.md`, not its directory scope. Progress lists these files. Honor's
`Step.input` references the original ToolCall, allowing history to reconstruct
its response if no later ModelCall delta saved it. Honor's own result is durable
but not a model message. Preparation and
execution use the same authorized paths. Initial coverage is explicit fs paths
and shell cwd, including reads; paths hidden in shell commands are not inspected.

Compact likewise stays out of model messages. Its result is
`{controls: [{ref, horizon}]}`, referencing the compact Run output; the compact
control changes the horizon used by subsequent ModelCalls.

## Implementation and verification

[Built-in toolsets](../src/toolang/plugin/toolsets/),
[runtime tools](../src/toolang/execution/tools/), and
[history binding](../src/toolang/execution/executor/tool_history.py) own execution.
[Plugin tests](../tests/unit/plugin/) and
[execution integration tests](../tests/integration/execution/) cover ordinary
calls, rules/guidance visibility and current-agent writes.
