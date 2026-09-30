# Script Projects

`too init DIR` creates an executable `aide.too` and a comment-only `toolang.toml`.
Both belong in version control. Initialization refuses to overwrite either file,
including directories and symlinks. A failure during creation may leave partial
output; remove or complete it before retrying. Add `.toolang/` to Git ignore rules.

For a script with an unnamed entry, omit the runnable or select `_` explicitly:
`too file.too` and `too file.too _` select the same entry. `agic:_` and `flow:_`
also check its kind. `<entry:5>` in help identifies the source line. Input `-` reads stdin,
so `too file.too _ -` selects the entry and reads its input from stdin.

Use `too run FILE [RUNNABLE] [ARGUMENTS]` when a runnable name matches a Toolang
command. Otherwise, `run` can be omitted: `too aide.too whats_for`.
Add `--help` after the file or runnable for details. File help lists the unnamed
entry first, then named agics and flows in their source order within each kind.

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
configuration does not grant filesystem access. For example:

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

Generated state stays in `srcdir/.toolang/`. It can be deleted while the runtime
is stopped; this removes local history, caches, and `lab` output. Roaming scripts
use process/container environment variables and do not load project `.env` files
or settings from the resident `~/.toolang` root.

## Temporary workspaces

Script calls automatically add srcdir as a workspace and select it as workdir.
Other execution modes do not add it automatically. `-w` and `-d` are available for
resident, roaming, and visiting agents:

```sh
./aide.too whats_for
./aide.too whats_for -w another_dir -w .
./aide.too whats_for --workdir project=../project
./aide.too whats_for --workdir repo://src
./aide.too whats_for --no-auto-workspace
```

`-w` / `--workspace [NAME=]<DIR>` adds a temporary grant and can be repeated.
`-d / --workdir [NAME=]<DIR>` adds a grant and selects its root. `--workdir NAME://[SUBDIR]`
selects an existing grant without adding access. Only one `--workdir` is allowed.
Any `-w` or `--workdir` suppresses automatic srcdir inclusion; configured grants and
`lab` remain. `--no-auto-workspace` disables automatic inclusion for script calls
and their inspection commands. Without an explicit selection, the last usable
workspace wins.

Names are inferred from the directory basename and normalized to kebab case:
`project.v2` becomes `project-v2`. For `.` and `..`, the resolved directory supplies
the name. Duplicate names fail; use `NAME=PATH` to choose another name. Split only
at the first `=`; `=./foo=bar` infers a name for the path `./foo=bar`.

Grants are fixed for each accepted Run and inherited by its children. Guest startup
resolves relative configuration paths on the host and mounts the resulting
snapshots. Restart the guest to refresh those captured configuration files. An existing
server cannot be rebound to different local directories; stop it before changing
its grants. Use `--workdir NAME://SUBDIR` to select an existing server workspace.

`too ./aide.too info` and `too ./aide.too workspace list` show configuration and
workspace information. Workspace listing reports the current invocation's workdir
and shows a running server's temporary grants separately.
Persistent workspace edits update source-local TOML;
visiting agents accept temporary grants only.

## File inputs

Chat and script `@file` inputs use procdir even when workdir changes. Task/chore
attachments remain relative to their authored files. Attachments supply content,
not workspace access. Hosted calls send client-read content; they never reopen
the same filename on the server. Prompt expansion, escaping, and fenced text keep
their existing [Content syntax](input-syntax.md).
