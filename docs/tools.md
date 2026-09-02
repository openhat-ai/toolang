# Tool Runtime

Toolang exposes tools through the toolset plugin family.

Tools execute inside normal runs and are recorded as `tool_call` steps.
`AgentTool.invoke()` is asynchronous. Function-tool wrappers await native
async callables and isolate synchronous Python callables in a worker thread,
so blocking tool implementations do not stall the run event loop.


## Built-In Tool Families

Current built-in tools are:

- `fs`
- `shell`
- `web`
- `service`
- `_me`

`_too` is deliberately absent from plugin loading. It is the executor-owned
inner runtime toolset, not a selectable tool resource.


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


## Current Agent

`_me` exposes structured operations for the current agent's authored data. The
leading underscore marks it as a Toolang-owned internal action toolset; it
still follows normal resource selection and can be denied by policy.

The executor injects the current agent layout through `ToolContext`. `_me`
tools do not accept an agent name, home directory, root directory, or arbitrary
path for choosing another target. They expose no layer selector and operate
only on the current agent's home layer; `_me` does not read or modify root-layer
caps.

It exposes five leaves for all supported resource kinds:

```text
_me__list(kind)
_me__get(kind, key)
_me__create(kind, key?, content)
_me__update(kind, key, content, if_digest?)
_me__delete(kind, key, if_digest?)
```

`kind` is one of `task`, `chore`, `psyche`, `skill`, `service`, `prompt`, or
`flow`. `key` is a task/chore id or an authored cap/flow name. Task and chore
create allocates the key and addresses ready documents only. Their lifecycle
does not support `_me__delete`, and delete is never interpreted as archive.

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
does not publish State directly; normal watcher and `_too__reload` behavior
remain authoritative.


## Runtime Rule

Tools do not own the model loop.

For every ordinary tool-capable Agic Model Call, the executor separately
injects `_too__run`, `_too__execute`, and `_too__reload`. `hands` and
`handoffs` authorize runnable targets but do not select these definitions. An
executor without State refresh still exposes reload and returns a correlated
error if it is called. Statement-generated Flow evaluators, output-repair
calls, and tool-disabled models receive no runtime tools.

The definitions never appear in `AgentSetup.tools`, tool ceilings, public tool
listings, or generic tool invocation. Run creates an ordinary Run Step, reload
creates a reload control without a Step, and successful execute creates an
applied execute control before replacing the active runnable without a Step.

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
