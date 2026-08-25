# Sandbox Development Wheel

Status: Draft for human approval.

## Work Type

Feature definition for deterministic `--dev` wheel selection and actionable
feedback when a development Toolang process starts a non-host sandbox without a
development wheel. The implementation also restores the historical directory
selection behavior that was lost during runtime refactoring.

## Verified Current Behavior

- `too run`, `too start`, and roaming file-inbox runtime accept `--dev PATH` and
  pass the resolved path through `LaunchSpec` and `SandboxRequest`.
- Docker uses a file as its `uv tool run --from` source, but passes a directory
  directly to `uv`; `uv` then treats that directory as a Python project.
- A directory inside the Toolang root can be translated to a guest path that is
  not mounted into Docker.
- The host sandbox ignores an existing `--dev` path, although launch resolution
  still rejects a missing path.
- Without `--dev`, Docker runs `uv tool run --from toolang`, so a CLI executing
  from editable source can start a different published Toolang version without
  any warning.
- Current CLI tests do not pass `--dev`, and Docker tests do not assert the
  selected wheel in the generated `uv` command.

## Goal And Success Criteria

Make `--dev` one explicit wheel-selection option. A caller may name one Toolang
wheel or any directory containing Toolang wheels; launch resolution selects one
concrete wheel before sandbox preparation. Host launches reject the irrelevant
option, while development CLIs warn before a non-host sandbox falls back to the
package index.

The change succeeds when:

- a wheel file remains the selected artifact;
- a directory is searched recursively and resolves to its most recently
  modified Toolang wheel;
- directory contents such as `pyproject.toml` do not change wheel-search
  behavior;
- invalid paths and directories without a Toolang wheel fail before sandbox
  preparation;
- Docker always stages one concrete wheel and never receives a development
  directory;
- host selection plus `--dev` fails with an actionable error;
- an editable/source Toolang CLI without `--dev` warns exactly once before a
  non-host launch and does not warn for host or explicit-wheel launches;
- the default offline verification passes.

## Scope

In scope:

- wheel-file and recursive wheel-directory resolution;
- deterministic latest-wheel and tie-breaking rules;
- Docker staging of the selected concrete wheel;
- host rejection of `--dev`;
- installed-package provenance detection for the controlling CLI;
- one non-host package-version-skew warning;
- CLI help, runtime documentation, and deterministic tests.

Out of scope:

- treating a directory as a Python source project;
- automatically building a wheel, editable guest installs, hot reload, or
  rebuilding after launch;
- querying a package index to discover the guest version;
- pinning the default guest package version;
- adding Toolang version data to `/healthz`, startup handshakes, or runtime
  state;
- adding `--dev` to direct runnable, Chat, control, inspection, or internal
  `serve` commands;
- changing sandbox lifecycle or plugin selection beyond this package-source
  input.

## Wheel Resolution

Resolve `--dev` after sandbox selection and before constructing `LaunchSpec`:

1. Expand `~` and resolve the input to an absolute path.
2. If the selected sandbox is `host`, reject the option before inspecting the
   path because host already uses the current Toolang Python environment.
3. If the path is a file, require a `.whl` suffix and a case-insensitive
   `toolang-` wheel filename prefix, then select it.
4. If the path is a directory, recursively enumerate regular `.whl` files with
   a case-insensitive `toolang-` filename prefix. Ignore all other files and
   directory metadata, including `pyproject.toml`.
5. Select the candidate with the greatest `st_mtime_ns`. When candidates have
   the same timestamp, select the lexicographically smallest absolute path.
6. Report distinct actionable errors for a missing path, a non-wheel file, a
   non-Toolang wheel, and a directory containing no Toolang wheels.

The selected `LaunchSpec.dev_artifact` is therefore always `None` or one
concrete Toolang wheel. Directory paths do not cross the sandbox plugin
boundary.

## Sandbox Behavior

Docker copies the selected wheel into its existing per-agent staging directory
and uses the staged guest path as `uv tool run --from`. Remove the current
directory translation and extra-mount branches because launch resolution no
longer supplies a directory. Preserve shell-safe quoting, staging cleanup, and
the behavior of an omitted `--dev`, which continues to use `toolang` from the
configured package index.

The host rejection applies whether `host` was explicit, selected by root or
agent configuration, or chosen as the default. Its diagnostic is:

```text
--dev does not apply to the host sandbox; it already uses the current Toolang environment
```

## Development Source Warning

Detect an editable installation from the installed `toolang` distribution's
`direct_url.json` when `dir_info.editable` is `true`, retaining the decoded
local source path when the URL is a local `file:` URL. If distribution metadata
is unavailable, a directly imported Toolang source tree containing the nearest
project `pyproject.toml` is the fallback development signal. Do not use an
ancestor Git repository alone as the installed-package signal because a
non-editable virtual environment can live inside a repository.

After launch resolution has selected a non-host sandbox, the CLI emits one
stderr warning when this development signal is present and `--dev` was omitted:

```text
Warning: the current Toolang process is running from development source at PATH,
but sandbox SANDBOX will install Toolang from the package index and may run a
different version. Build a wheel with `uv build --wheel` and pass `--dev dist`.
```

Use the sandbox name and omit `at PATH` when no safe local source path is
available. The warning is informational in TTY and non-TTY execution, performs
no network lookup, does not block launch, and is absent for host or an explicit
development wheel.

## Touchpoints

- `src/toolang/up/sandbox.py`
- `src/toolang/plugin/sandboxes/docker.py`
- `src/toolang/cli/common/version.py`
- `src/toolang/cli/toolang/commands/runtime.py`
- `tests/unit/up/test_sandbox.py`
- `tests/unit/plugin/test_sandboxes.py`
- `tests/unit/cli/test_cli_version.py`
- `tests/integration/cli/test_runtime_commands.py`
- `docs/api.md`

No base sandbox protocol or persisted-state schema change is expected.

## Acceptance Tests

1. A direct Toolang wheel path resolves unchanged after absolute-path
   normalization.
2. A directory containing root and nested Toolang wheels selects the greatest
   `st_mtime_ns`; equal timestamps select the lexicographically smallest path.
3. A directory containing `pyproject.toml`, unrelated files, and Toolang wheels
   still uses the same recursive wheel selection.
4. Missing paths, non-wheel files, non-Toolang wheels, empty directories, and
   directories containing only unrelated wheels produce distinct pre-launch
   errors.
5. Docker stages the selected wheel and quotes its guest path in `uv --from`;
   no directory artifact mount or translation remains.
6. Explicit, configured, and default host selection reject `--dev` before path
   validation.
7. `run`, `start`, visiting, roaming, and roaming file-inbox routes pass their
   selected wheel through the shared launch path.
8. Editable metadata with non-host selection and no `--dev` emits one warning;
   released metadata, host selection, and explicit `--dev` emit none.
9. Help and runtime documentation describe a Toolang wheel file or the newest
   recursively discovered wheel in a directory and show `uv build --wheel`
   followed by `--dev dist`.
10. Ruff, formatting, type checking, and the complete default offline test suite
    pass.

## Risks And Open Questions

Modification time represents the most recently built artifact, not the highest
package version. This intentionally preserves the historical development
workflow and permits rebuilding the same version. A copied or touched older
wheel can therefore become the selected artifact; users who require an exact
choice should pass its file path.

Recursive discovery can inspect a large directory tree, so callers should
normally pass `dist` or another build-output directory. The operation is local,
synchronous, and performed once per launch. Package-index version pinning and a
guest version handshake remain future features. There are no open questions.
