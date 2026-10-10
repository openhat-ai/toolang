# Workspace CLI options

Approved in the implementation request on 2026-10-10, following the command
scope and `run` fallback discussion.

## Goal and scope

Expose workspace options only on commands that execute with caller-selected
directories. Make configuration inspection independent of runtime state.

| Command | `-w` / `--workspace` | `-d` / `--workdir` | Automatic source directory |
| --- | --- | --- | --- |
| `tools`, `models`, `providers`, `workspace list`, `start` | No | No | No |
| `serve`, `chat` | Yes | Yes | No |
| Script `run`, including shorthand invocation | Yes | Yes | When no `-w` is supplied |

Remove `--no-auto-workspace` everywhere. Script workspace fallback depends only
on `-w`, including when `-d` is present. Preserve configured grants and implicit
`lab`. `-d` keeps its path-grant and URI-selection forms; workdir precedence is
explicit `-d`, last `-w`, then the automatic source directory. Preserve existing
name validation and duplicate-name rejection. Selecting the automatic source
grant again with an identical `-d` path reuses that grant.

`workspace list` reads authored configuration, preserves insertion order, and
shows configured names, resolved paths, and local directory availability. It
does not add `lab`, temporary bindings, or an initial workdir, prepare execution
State, or query a running service. Roaming targets read source-local
`toolang.toml` without loading ancestor configuration or creating project caches;
resident and visiting targets read their layout configuration.

Keep `start`'s existing default-workdir selection from configured workspaces.
Do not introduce a workdir configuration field or change Chat/server lifetimes,
runtime inspection APIs, sandbox mounts, or workspace add/remove behavior.

## Touchpoints

- `src/toolang/cli/common/workspaces.py`: resolution and obsolete inspection helpers.
- `src/toolang/cli/toolang/commands/{script,runtime,workspace,plugin,model_catalog}.py`:
  command options and consumption.
- CLI unit/integration tests, `docs/script-projects.md`, and `CHANGELOG.md`.

## Acceptance checks

- Removed options are absent from help and rejected by affected commands.
- Scripts without `-w` include the real source directory with either form of
  `-d`; repeated `-w` suppresses this fallback and the selected workdir follows
  the precedence above. Options before/after the runnable behave identically.
- `serve` and `chat` accept explicit directories without an automatic source grant.
- Configuration listing is unchanged by server status and works with invalid
  program source, absent directories, and source-local relative paths. Roaming
  listing tolerates invalid ancestor configuration and conflicting project caches.
- Existing workspace management and runtime inspection tests continue to pass;
  default lint, formatting, type checks, and offline tests pass.

## Compatibility

`start -w` users must configure `[workspaces]` or use `workspace add`; its former
`-d` subdirectory override has no configuration equivalent. Inspection commands
reject execution options. Scripts using `--no-auto-workspace` must omit it and
use `-w` to select explicit directories; a config-only/`lab`-only invocation
without any temporary grant is no longer exposed. A lone `-d` now also grants
the source directory. Runtime workspace inspection remains available through
the existing API and agent information command.

Open questions: none within this scope.
