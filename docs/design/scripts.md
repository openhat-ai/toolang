# Scripts

`too init DIR` creates an executable `aid.too` and a comment-only `toolang.toml`.
Both belong in version control. Initialization refuses to overwrite either file,
including directories and symlinks. A failure during creation may leave partial
output; remove or complete it before retrying. Add `.toolang/` to Git ignore rules.

The client selects an authored runnable, resolves its inputs, and captures a
Run request. [CLI](cli.md#script-selection-and-output) owns selector routing;
this guide owns project inputs and the generated invocation boundary.

## Directories and configuration

- **procdir** is the directory from which the client was invoked. Explicit CLI
  paths and Chat/script `@file` attachments resolve from here.
- **srcdir** is the real `.too` file's directory, after resolving symlinks.
- **workdir** is a location within an authorized workspace, such as `repo://src`.

Toolang searches for `toolang.toml` and `toolang.catalog.json` from srcdir through
the nearest Git working-tree root. Nested repositories, submodules, and worktrees
have their own boundaries. Outside Git, only srcdir is searched. Configuration
paths resolve relative to the file that declares them.

Shared settings merge from outer to inner configurations using the existing
field-specific rules. Workspace grants come only from source-local TOML; ancestor
workspace declarations are ignored before resolving their paths. For example:

```toml
# scripts/toolang.toml
[workspaces]
repo = ".."
```

Catalogs never merge. Selection order is an explicit CLI override, the
`TOOLANG_MODEL_CATALOG` environment variable, then the nearest authored layer,
then the bundled catalog. Within a layer, `plugin.model_catalog.models_dev.path`
precedes `toolang.catalog.json`. A project file named `catalog.json` is ignored.
Resident agents retain their root/home `catalog.json` convention.

Each invocation owns `<srcdir>/.toolang/<agent>@<instance_id>/`; agent home is
`<agent_root>/agents/<agent>`. Status, stores, State, logs, and tool rooms are
isolated. Published roots survive exit/failure; runtime cleanup never deletes them.
Roaming scripts use process/container environment variables; they do not load
project `.env` files or resident-root dotenv files. Source-owned edit guards remain shared.

## Runnable input and help

The selected language signature generates the CLI's named inputs, primary input,
requiredness, and help. File help lists declaration metadata without building every runnable signature;
only the selected runnable gets a complete native parameter set. Runnable help
explains that declaration and derives a Flow outline from its statements/docs.
Explicit `run` disambiguates runnable names that collide with CLI commands.
[CLI](cli.md#routing) owns routing and entry selection.

Header parsing happens before reading stdin or preparing State. Named
assignments and options precede primary input. The first ordinary input operand
or `--` starts line input; all remaining words are Content, including assignment-
looking and option-looking text. Unknown/duplicate assignments before that
boundary fail. Shell quoting cannot turn a standalone marker into ordinary text.

| Primary input form | Capture boundary |
| --- | --- |
| `TEXT...` or `-- TEXT...` | Remaining shell words, joined with spaces; explicit line input must be nonempty. |
| Final standalone `-` | Read stdin through EOF; an empty stream supplies empty input. |
| Omitted | Read redirected/piped stdin; an empty stream leaves input absent. Never read an interactive terminal. |

A standalone `---` header is rejected: prompt fences belong inside Content,
while Script stdin is unbounded to EOF. Prompt calls in the captured input keep
all [Content](content.md) forms. Native input values and signature coercion
follow [Call Input](call-input.md).

Common options can appear before or after the runnable, before input starts.
An explicitly supplied runnable-level scalar wins over its file-level value;
repeatable workspace/allow/limit options accumulate in command-line order.
Workdir occurs once across both levels. Script mode rejects global root overrides.

Explicit help does not read stdin or prepare State and exits 0. Missing required
input/arguments show runnable help and exit 2. Neither accepts a Run. Complete
request validation still precedes admission. Progress uses
stderr; durable results are emitted to stdout or a file only when `--out` selects
that destination. Failed/canceled Runs do not write it. [Execution presentation](execution-presentation.md)
owns progress semantics.

## Temporary workspaces

Script execution adds srcdir automatically when `-w` is absent. Explicit `-w`
grants replace that automatic addition; `-d` selects workdir and may add a grant.
`serve` and `chat` accept temporary grants without automatic srcdir, while
`start` uses configured grants. [Workspaces](workspaces.md) owns authority,
selection precedence, naming, capture, inspection, and guest mounts.

The following examples assume the `aid.too` generated by `too init`, existing
local directories, and a configured `repo` grant with a `src` subdirectory:

```sh
too aid.too whats_for -w another_dir -w .
too aid.too whats_for --workdir project=../project
too aid.too whats_for --workdir repo://src
```

An existing server must already have compatible grants. Temporary grants last
for that server generation and do not update configuration. Chat server lifetime
depends on placement; [Hosting](hosting.md#server-lifetime-and-client-leases)
owns retention and cleanup.

## File inputs

Chat and script `@file` inputs use procdir even when workdir changes. Chore file
attachments resolve relative to their Markdown file; inline chore includes use
the agent home. Attachments supply content,
not workspace access. Hosted calls send client-read content; they never reopen
the same filename on the server. Prompt expansion, escaping, and fenced text keep
their existing [Content syntax](content.md).
