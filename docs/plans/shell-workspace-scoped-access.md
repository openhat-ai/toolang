# Run-Level Workspace and Working Directory

Status: Approved on 2026-09-27. Update public docs with the implementation.

## Goal and current behavior

Give each Run a logical workdir shared by model calls and path-aware tools. Workspace grants are versioned Agent State; this feature makes the Run location durable and uses the same workspace-root URI syntax for `fs`, `chdir`, and runtime context. Guest sandbox mounts are captured at startup. Enabled `<toolang:context>` is sent on the first Model Call and repeated on each later call; older copies remain in history.

## Location and path contract

- The effective location is `(workspace: str | None, workdir: str)`. Internally, `workdir` is a normalized workspace-root-relative path with no leading `/`; `""` denotes the workspace root. `(None, "")` is unselected, never agent home. Durable controls keep one canonical `cwd: str`: `repo://` for the root, `repo://a/b/c` for a subdirectory, and `""` for unselected. Percent-encode UTF-8 path components (including literal `%`) exactly once; `/` separates components. A root Run selects the only configured workspace if it is available; otherwise it starts unselected.
- Every lowercase `name://suffix` path denotes the configured workspace named `name`; there is no generic URL-scheme dispatch or leading-colon escape syntax. `repo://a/b/c` resolves from workspace `repo`'s root; `repo://` denotes that root. This applies equally when a workspace is named `file` or `https`.
- Plain relative paths `a/b`, `./a`, and `../b` resolve from the current workdir and fail when unselected or when traversal escapes the current workspace. A leading `/` is OS-absolute and selects the deepest configured workspace containing the resolved target; paths outside all available workspaces fail. Do not accept `file://` or `file:///` as file-URL forms. If `file` is configured as a workspace, `file://path` is simply a workspace reference to it.
- Reject malformed workspace references, queries/fragments, invalid escapes, encoded separators, and decoded paths whose path class changes. Do not use generic URL-host parsing. Preserve the lowercase kebab-case workspace-name syntax.
- Remove the former `workspace://<name>/<path>` namespace convention and `fs.list("workspace://")` discovery behavior. `workspace://...` now denotes a path in a workspace literally named `workspace` and is never a workspace-listing alias. `fs` emits canonical `repo://a/b/c` references; `_toolang.workspaces()` lists names, availability, and roots without host paths.
- Reject duplicate canonical workspace roots; permit nested roots. Absolute paths choose the most-specific root; relative and named-workspace paths retain their selected workspace identity. Check rules against that identity and normalized root-relative path. A configured `/` explicitly grants path-aware tools the executing filesystem.

## State and Setup

- **Agent State:** owns versioned workspace names, source-root bindings, and grants. **Agent Setup/hosting:** provides the effective host/guest root mapping and mount availability; it cannot grant a root absent from the captured State. Mount matching external roots at sandbox startup; a workspace added later without a matching mount remains unavailable until restart/reconfiguration. Never interpret a host-only path as a guest root.

## Executor

The Run/executor projects the canonical control `cwd` into `(workspace, workdir)` and caches it per Run. Child Runs copy the parent's committed `cwd` string at acceptance; parent and siblings are unaffected by child changes. Flow and Agic steps share their owning Run's location; same-Run execute retains it; rerun initializes a new root location.

## Tool context

Each Tool Step receives immutable snapshots of its Run cwd, captured State grants, and Setup root mapping. Path preflight and invocation use the same resolved target. After State adoption removes/remaps a selected root or makes it unavailable, durably set `cwd=""` before the next Step; never silently retarget an old workdir.

## Control records and recovery

- Root and child Run acceptance controls carry `cwd: str` in the canonical full form above. Add an applied, Run-scoped cwd control with the same `cwd: str`, a cause (`chdir` or invalidation), and its causing Step/State reference. This control is the **sole authority** for post-acceptance changes; tool output only reports them. An unselected location always uses the empty string.
- `_toolang.chdir` validates its target and honors workspace rules before any change. Commit its successful Tool StepEnd and cwd control **atomically in one store transaction**. Failed, canceled, or honor-blocked calls create no cwd control. Only then update the in-memory cache; after delivery interruption, read the committed outcome rather than running cd again. Persist invalidation with the State adoption transition before the next Step.
- On recovery, parse the Run acceptance `cwd` and last applied cwd control before the selected boundary; never infer location from current config, host cwd, or model text. Retry removes cwd controls in its truncated suffix in the same transaction as discarded execution records and reconstructs at its anchor. Legacy Run records without a `cwd` start unselected. Replay/inspection use records without re-executing cd.

## Tools

- `_toolang.chdir(path)` takes **one required path**; the target must exist and be a directory. Its result reports the canonical full `cwd` string. A relative path needs a selected cwd; `repo://` switches to repo root even when unselected; an authorized OS-absolute path can also select a workspace. It must be the **only tool call in its Model Call**: reject a mixed batch before any call executes. `_toolang.workspaces()` is read-only and usable when unselected.
- `fs` file operations take `path` plus operation-specific options (`text`, `pattern`, etc.), **not** `workspace` or `cwd`. `fs.list()`/`fs.glob()` default path to `"."`. With unselected cwd, relative paths fail but explicit named-workspace or authorized OS-absolute paths work. All `fs` results are canonical workspace references; no `fs` call changes Run cwd. Reject obsolete `workspace` and unexpected `cwd` arguments rather than ignoring them.
- `shell.execute(command, timeout_sec=…, max_output_chars=…)` drops per-call `workspace`/`cwd`; it starts in the Run cwd, honoring that directory's rules, and fails if cwd is unselected/unavailable. An in-command `cd` affects only that subprocess. `command` is shell text: paths inside it are interpreted by the shell/OS, **not** Toolang's path resolver.

## Protocol

The enabled `<toolang:context>` appears in the first Model Call and is appended again on later calls; prior copies remain in history. Append a runtime-owned `<toolang:workdir path="repo://a/b/c"/>` after the current context on every Model Call, independently of `context = none`. The single `path` value is either empty (unselected), `repo://` (the selected workspace root), or a workspace-root reference with a percent-encoded UTF-8 relative suffix. In this runtime-owned declaration every `name://` prefix means a configured workspace, including names that may be URL schemes elsewhere. Among visible declarations, **only the last** is authoritative. Keep earlier declarations as historical snapshots. XML-escape the attribute; never expose physical roots or cache this declaration in static `PromptInputs.rendered_input`. Model Steps record the declaration actually sent. Model instructions prefer relative paths from the latest workdir, and warn that shell commands can escape the workspace without an OS sandbox.

## Scope, verification, and risk

Likely touchpoints: `src/toolang/state/config.py`; `src/toolang/setup/` and `src/toolang/up/{mounts,sandbox}.py`; `src/toolang/base/{types/tool.py,utils/workspace_paths.py}`; `src/toolang/plugin/toolsets/{fs,shell}.py`; `src/toolang/execution/{records,store,types}.py`, `execution/tools/_toolang.py`, `execution/executor/`, and `execution/assembly/` (including `prompting.py` and `prompts/protocol.md`). Update `docs/tools.md` and other public docs **after** approval, with implementation. This approved definition is the implementation contract; public docs are updated with the implementation.

Acceptance tests: zero/one/multiple/unavailable workspace initialization; host/guest mounts; nested and `/` roots; path forms (`name://`, relative `.`, `..`, and absolute `/`), rejection of file-URL forms and `workspace`/`cwd` arguments, and mixed chdir batches; fs explicit paths without cwd; chdir failures/cancellation and atomic StepEnd/control commit; child/sibling, Flow/Agic, reload/invalidation, retry-anchor, replay, and legacy-record recovery; `<toolang:workdir path="name://..."/>` declarations on consecutive calls, after chdir/reload and with `context = none`; context repetition, last-declaration precedence despite historical copies, root/unselected encoding, Unicode/spaces/percent escapes, and shell-escape guidance.

This scopes path-aware tools and the **starting directory** of shell commands, not arbitrary filesystem accesses by commands. OS-absolute paths are environment-specific; relative or named-workspace paths are preferable across sandboxes. No OS-level shell sandbox or host-path portability guarantee is added.
