# Tool Runtime

Toolang exposes tools through the toolset plugin family.

Tools execute inside normal runs and are recorded as `tool` Steps.
`AgentTool.invoke()` is asynchronous. Function-tool wrappers await native
async callables and isolate synchronous Python callables in a worker thread,
so blocking tool implementations do not stall the run event loop.


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

`fs` is scoped to the current agent home.

It provides structured file operations such as:

- `read`
- `write`
- `append`
- `list`
- `glob`
- `stat`
- `mkdir`
- `remove`

`fs` paths and `shell` cwd accept an optional `workspace` name from the published
State. With that anchor, `/src` means `src` under the workspace root. Without it,
paths resolve from the tool working directory; overlapping workspace matches
require an explicit name. Workspace configuration does not expand home access.


## Shell

`shell` runs one non-interactive command inside the current agent home.

It returns structured:

- `stdout`
- `stderr`
- `exit_code`


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
physical ownership. Step pages contain their `run` and control `dependencies`,
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
where output is `{type, value, name, dim}` or null, including partial output.
Missing targets and unresolved values fail as ordinary tool errors.


## Current Agent

`me` exposes structured operations for the current agent's authored data. It
follows normal resource selection and can be denied by policy.

The executor injects the current agent layout through `ToolContext`. `me`
tools do not accept an agent name, home directory, root directory, or arbitrary
path for choosing another target. They expose no layer selector and operate
only on the current agent's home layer; `me` does not read or modify root-layer
caps.

It exposes five leaves for all supported resource kinds:

```text
me__list(kind)
me__get(kind, key)
me__create(kind, key?, content)
me__update(kind, key, content, if_digest?)
me__delete(kind, key, if_digest?)
```

`kind` is one of `task`, `chore`, `psyche`, `skill`, `service`, `prompt`, or
`flow`. `key` is a task/chore id or an authored cap/flow name. Task and chore
create allocates the key and addresses ready documents only. Their lifecycle
does not support `me__delete`, and delete is never interpreted as archive.

`content` is selected and validated from the operation and kind. Job writes
reuse the Markdown document models, id allocation, and RRULE validation used
by the CLI and jobs API. Cap writes reuse the authored cap file layout and
validation used by the CLI and cap API. Flow writes manage only direct
`flows/<key>.too` modules and validate the complete home program before an
atomic write. Invalid create and update requests do not change existing
authored files.

Get and list return home-relative paths and SHA-256 digests. Update and delete
accept an optional `if_digest` precondition. Expected failures remain failed
tool calls and include a structured `output.error` with a stable code,
operation, kind, optional key, and bounded field diagnostics. Source mutation
does not publish State directly; normal watcher and `_toolang__reload` behavior
remain authoritative.


## Runtime Rule

Tools do not own the model loop.

For every ordinary tool-capable Agic Model Call, the executor selects the registered
`_toolang__run`, `_toolang__execute`, `_toolang__reload`, and `_toolang__pick` tools. `hands` and
`handoffs` authorize runnable targets but do not select these definitions. An
executor without State refresh still exposes reload and returns a correlated
error if it is called. Statement-generated Flow evaluators, output-repair
calls, and tool-disabled models receive no runtime tools.

`AgentSetup.tools` retains registered runtime tools independently of user tool
ceilings. Each invocation has an ordinary Tool Step. Trusted runtime tools receive
per-call operations through `ToolContext.runtime`, not the Store or executor.
Run creates a child owned by its Tool Step and returns `{run_id, output_type, output}`.
Reload and execute return `{controls: [ControlRef]}`; the controls retain their
payloads. Execute finishes its Tool Step before transferring execution.

`ToolStepGiven.trigger` records `model` or `runtime`. Both have durable results and
progress events; only model-triggered calls contribute ToolResult messages.
Failures use `ToolResultPart.error`, with additional diagnostics in the output.

Toolang runtime owns:

- when tools are available
- when a tool is executed
- how tool output re-enters the run
- how tool calls are recorded and exposed
- the default human-readable summary for each tool-call lifecycle state

Summary generation receives the tool family, leaf name, and supplied arguments
in the tool definition's parameter order. The default summary combines only
the leaf name and first supplied argument; it does not display the family. Its
running form is `Executing NAME ARG ...`; its succeeded and failed forms are
`Executed NAME ARG` and `Failed NAME ARG`. The canceled form is
`Canceled NAME ARG`. Toolang normalizes and bounds argument previews and
redacts sensitive parameter names or schemas before the summary enters
execution events. The running summary is stored in `ToolStepGiven.summary`;
the terminal summary uses the same key in `ToolStepNoted`.

Plugin-defined summary templates are not part of the current tool contract.

### Pick guidance

`_toolang/pick({kind: "skill" | "service", ref: "<catalog ref>"})` recalls one
allowed resource's body. Use the exact ref from its separate skill or service
catalog, not a name, path, or selector. Pick neither grants tools nor connects,
authenticates, or discovers MCP services.

The Tool Step returns `{controls: [ControlRef]}`. An applied recall control holds
the target, SHA-256 revision, and original recalled text; the next Model Call
adopts it as a separate `<skill>` or `<service>` user message. Failures create no
recall. Revision zero (`"0"`) denotes removal; a present empty body retains its
nonzero hash. Other revisions use 64 lowercase hexadecimal digits.

Repeated picks reuse the latest matching unadopted recall in the same Run. If
nothing is pending and the last visible revision matches, the receipt is empty.
Visibility comes from recall references in the committed call's selected near/now
templates, never from XML matching, far summaries, or raw control existence.
Content that leaves the view must be picked again; assembly does not restore it.
Live prefixes cache these revisions, and history recovers them from the same
saved deltas without reading historical State. Existing record encodings are
unchanged.

### Honor workspace rules

For model calls to path-aware tools, runtime checks applicable `AGENTS.md` files
from the workspace root to the target's scope. Missing or changed rules cause a
runtime-only `_toolang/honor` Step, followed by the original tool's result:
`error="operation not executed; retry required"`, `output={}`. The next Model Call
receives separate `<rules>` user messages after the complete tool exchange.
Only a subsequent retry may execute the operation; current visible rules need
no honor Step. Honor shares pick's revision, pending-reuse, and visibility rules.
Confirmed deletion retracts earlier rules; failed reads block the operation.

Honor is registered normally but is neither advertised to nor callable by the
model. Its results are durable, not orphan ToolResult messages. Preparation and
execution use the same authorized paths. Initial coverage is explicit fs paths
and shell cwd, including reads; paths hidden in shell commands are not inspected.
