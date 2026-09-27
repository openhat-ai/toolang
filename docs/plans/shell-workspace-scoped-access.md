# Run-Level Workspace and Working Directory

Status: Approved on 2026-09-27. Update public docs with the implementation.

## Goal and current behavior

Give each Run a logical working location shared by model calls and path-aware tools. Today workspace grants are versioned Agent State, `fs` requires `workspace://` or a `workspace` argument, `shell.execute` accepts `workspace`/`cwd`, and Run execution has no persistent cwd. Sandbox startup does not automatically mount external workspaces. Enabled `<toolang:context>` is sent on the first Model Call and repeated on each later call; older copies remain in history.

## Location and path contract

- The effective location is `(workspace: str | None, workdir: str)`. `workdir` is a normalized, decoded path relative to the workspace root, with no leading `/`; `""` denotes the root. `(None, "")` is unselected, never agent home. Durable controls store this location as one canonical `cwd: str`: `:repo://` for the root, `:repo://a/b/c` for a subdirectory, and `""` for unselected. Percent-encode UTF-8 path components, including literal `%`, exactly once; `/` separates components. Do not store shorthand `repo://`, `.`/`..`, host paths, or duplicate workspace/workdir fields in controls. A root Run selects `:name://` only if exactly one workspace is configured **and available**; otherwise it starts unselected. Do not infer a root from config ordering or process cwd.
- For tools that accept paths: plain `a/b`, `./a`, `../b`, and `file://a/b` resolve from current cwd and fail if unselected or if they escape its workspace; `/os/path` and `file:///os/path` are OS-absolute and match the deepest configured workspace root containing the resolved path. An absolute path outside every available root fails. Relative paths do not silently switch workspaces.
- `repo://a/b/c` resolves from workspace `repo`'s root when `repo` is not a known scheme; `:repo://a/b/c` **always** selects workspace `repo`, even if it is named `file` or `https`. `repo://` and `:repo://` denote its root. All path-aware tools use the same syntax; a reference does not change Run cwd unless passed to `_toolang.cd`.
- Parse leading `:name://` before unprefixed schemes. The initial known-scheme table is `file`, `workspace`, `runspace`, `http`, `https`, `ftp`, `ftps`, `ws`, `wss`, `ssh`, `git`, `data`, `mailto`, `urn`, `about`. Known but unsupported schemes fail rather than becoming workspace paths; unknown schemes require an exact configured workspace name. Keep the existing lowercase kebab-case name syntax, but do **not** forbid names that match schemes. `file://a` is Toolang-specific cwd-relative syntax, not a URL host. Reject malformed/ambiguous references, queries/fragments, invalid escapes, and path-class changes after decoding once; do not use generic URL-host parsing for workspace references.
- Remove `workspace://` completely: neither old file paths nor `fs.list("workspace://")` remain valid. Return migration errors, not compatibility aliases. `fs` emits only stable forced references (for example `:repo://a/b/c`); `_toolang.workspaces()` lists names, availability, and root references without revealing OS paths.
- Reject duplicate canonical workspace roots; permit nested roots. OS-absolute paths choose the most-specific root; relative and named-workspace paths retain their selected workspace identity. Check applicable workspace rules against that identity and normalized root-relative path. A configured `/` explicitly grants path-aware tools the entire executing filesystem.

## State and Setup

- **Agent State:** owns versioned workspace names, source-root bindings, and grants. **Agent Setup/hosting:** provides the effective host/guest root mapping and mount availability; it cannot grant a root absent from the captured State. Mount matching external roots at sandbox startup; a workspace added later without a matching mount remains unavailable until restart/reconfiguration. Never interpret a host-only path as a guest root.

## Executor

The Run/executor projects the canonical control `cwd` into `(workspace, workdir)` and caches it per Run. Child Runs copy the parent's committed `cwd` string at acceptance; parent and siblings are unaffected by child changes. Flow and Agic steps share their owning Run's location; same-Run execute retains it; rerun initializes a new root location.

## Tool context

Each Tool Step receives immutable snapshots of its Run cwd, captured State grants, and Setup root mapping. Path preflight and invocation use the same resolved target. After State adoption removes/remaps a selected root or makes it unavailable, durably set `cwd=""` before the next Step; never silently retarget an old workdir.

## Control records and recovery

- Root and child Run acceptance controls carry `cwd: str` in the canonical full form above. Add an applied, Run-scoped cwd control with the same `cwd: str`, a cause (`cd` or invalidation), and its causing Step/State reference. This control is the **sole authority** for post-acceptance changes; tool output only reports them. An unselected location always uses the empty string.
- `_toolang.cd` validates its target and honors workspace rules before any change. Commit its successful Tool StepEnd and cwd control **atomically in one store transaction**. Failed, canceled, or honor-blocked calls create no cwd control. Only then update the in-memory cache; after delivery interruption, read the committed outcome rather than running cd again. Persist invalidation with the State adoption transition before the next Step.
- On recovery, parse the Run acceptance `cwd` and last applied cwd control before the selected boundary; never infer location from current config, host cwd, or model text. Retry removes cwd controls in its truncated suffix in the same transaction as discarded execution records and reconstructs at its anchor. Legacy Run records without a `cwd` start unselected. Replay/inspection use records without re-executing cd.

## Tools

- `_toolang.cd(path)` takes **one required path**; the target must exist and be a directory. Its result reports the canonical full `cwd` string. A relative path needs a selected cwd; `:repo://` switches to repo root even when unselected; an authorized OS-absolute path can also select a workspace. It must be the **only tool call in its Model Call**: reject a mixed batch before any call executes. `_toolang.workspaces()` is read-only and usable when unselected.
- `fs` file operations take `path` plus operation-specific options (`text`, `pattern`, etc.), **not** `workspace` or `cwd`. `fs.list()`/`fs.glob()` default path to `"."`. With unselected cwd, relative paths fail but explicit named-workspace or authorized OS-absolute paths work. All `fs` results are forced-workspace references; no `fs` call changes Run cwd. Reject obsolete `workspace` and unexpected `cwd` arguments rather than ignoring them.
- `shell.execute(command, timeout_sec=…, max_output_chars=…)` drops per-call `workspace`/`cwd`; it starts in the Run cwd, honoring that directory's rules, and fails if cwd is unselected/unavailable. An in-command `cd` affects only that subprocess. `command` is shell text: paths inside it are interpreted by the shell/OS, **not** Toolang's path resolver.

## Protocol

The existing enabled `<toolang:context>` appears in the first Model Call and is appended again on each later call; old copies remain in assembled history. Follow the same repetition rule for cwd **independently of `context = none`**: append a new runtime-owned `<toolang:working-location workspace="repo" workdir="a/b/c"/>` user declaration to every Model Call after its current context. Define its meaning in `protocol.md`: among all working-location declarations visible in a Model Call, **only the last one is authoritative**; earlier declarations are historical snapshots. Do not remove historical messages or retroactively change them. Model Step input records the declaration actually sent.

Encode `workspace` as the exact configured lowercase name, or `""` when unselected. Encode `workdir` as the percent-encoded UTF-8 workspace-root-relative suffix of canonical control `cwd` (e.g. `a%20b/c`), with no leading `/`; `""` means the selected workspace root or an unselected location when `workspace=""`. XML-escape attribute values as well. Never expose physical roots or cache the declaration in static `PromptInputs.rendered_input`: derive it from the effective Run control at each Model Call boundary. Model instructions prefer cwd-relative paths, suggest `:repo://` for explicit cross-workspace paths, and warn that shell commands can escape the workspace without an OS sandbox.

## Scope, verification, and risk

Likely touchpoints: `src/toolang/state/config.py`; `src/toolang/setup/` and `src/toolang/up/{mounts,sandbox}.py`; `src/toolang/base/{types/tool.py,utils/workspace_paths.py}`; `src/toolang/plugin/toolsets/{fs,shell}.py`; `src/toolang/execution/{records,store,types}.py`, `execution/tools/_toolang.py`, `execution/executor/`, and `execution/assembly/` (including `prompting.py` and `prompts/protocol.md`). Update `docs/tools.md` and other public docs **after** approval, with implementation. No implementation or other docs belong to this definition change.

Acceptance tests: zero/one/multiple/unavailable workspace initialization; host/guest mounts; nested and `/` roots; path forms, traversal, symlinks, and rule preflight; rejection of old `workspace://`, `workspace`/`cwd` arguments, and mixed cd batches; fs explicit paths without cwd; cd failures/cancellation and atomic StepEnd/control commit; child/sibling, Flow/Agic, reload/invalidation, retry-anchor, replay, and legacy-record recovery; Model Call declarations on consecutive calls, after cd/reload and with `context = none`; context repetition, last-declaration precedence despite historical copies, root/unselected encoding, Unicode/spaces/percent escapes, and shell-escape guidance.

This scopes path-aware tools and the **starting directory** of shell commands, not arbitrary filesystem accesses by commands. OS-absolute paths are environment-specific; relative or named-workspace paths are preferable across sandboxes. No OS-level shell sandbox or host-path portability guarantee is added.
