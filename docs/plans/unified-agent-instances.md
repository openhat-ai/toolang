# Unified agent instances

Status: feature definition for human review. Do not implement before approval.
This proposal replaces the separate embedded-observation approach in
[draft #882](https://github.com/openhat-ai/toolang/pull/882). The mechanics below
complete the placement, identity, and reuse decisions discussed with the human;
they are proposed behavior, not a description of the current implementation.

## Goal and scope

Give every executing agent one instance identity, one agent root, and one
executor owning its activity. `too top` must discover all instances from the
selected Toolang root and display active runs through the common Agent API,
including concurrent executions of the same `.too` file on host and guest.

In scope: instance names, root calculation, uniform storage, executor reuse and
startup compatibility, port allocation, global discovery, common API hosting,
source-scoped instance selection for existing inspection/control commands, and
regression coverage for activity reporting.

Out of scope: automatic collection of abandoned registrations or agent roots,
history import, a new transport or discovery service, source-language changes,
removal/renaming of public commands or flags, and changes to API paths or response
schemas.

## Identity and roots

Use these terms explicitly throughout the design and implementation:

| Term | Meaning |
| --- | --- |
| `toolang_root` | The one selected global root, resolved through the existing CLI/environment contract; owns Hub data and global discovery |
| `agent_root` | The root of an agent instance's materialized layout; calculated by placement |
| `agent_home` | `<agent_root>/agents/<agent_name>`; contains the program, agent-local configuration, state, and runtime files |
| `source_dir` | The original local `.too` file's parent directory; remains the origin of roaming project inputs |

Only resident agents use `toolang_root` as `agent_root`. Global discovery belongs
to the Toolang-root role even when these two paths coincide. Avoid the ambiguous
term "agent directory"; specify agent root or agent home.

| Placement | Agent name | Instance name | Agent root | Executor policy |
| --- | --- | --- | --- | --- |
| Resident | `gus` | `gus` | `<toolang_root>` | Reuse the compatible active instance |
| Visiting | `lex` | `lex@v<source_id>` | `/tmp/lex@v<source_id>` | Reuse the compatible active instance |
| Roaming | `aid` | `aid@r<launch_id>` | `<source_dir>/.toolang/aid@r<launch_id>` | Create a new instance for every launch |

Keep existing agent-name derivation. Visiting `source_id` retains the current
16-character lowercase SHA-256 prefix over canonical Toolang root, NUL, and
canonical Source Ref target, including existing alias normalization. Ports,
sandbox selection, and fetched content do not change this identity.

Generate one path-bound `launch_id` before materializing a new workload:

```text
launch_id = SHA256(canonical_source_path_utf8 + NUL + fresh_uuid4_bytes).hexdigest()[:32]
```

Roaming uses the original canonical absolute `.too` source path, before its
per-launch agent root exists. Resident and visiting use their stable canonical
host program path. Generate the nonce afresh for every launch. Compute the ID
once at the host control boundary; never rehash guest-mapped paths. Resident
and visiting restarts retain their instance name/root but get a new launch ID.
Roaming uses the same 32-character lowercase hex launch ID in its instance name,
root, status, and bootstrap, with no second execution identity. Its stable source
hash may remain provenance metadata but is not a path key. Attachment,
inspection, and cancellation use saved identity and never allocate a new root.

Record the canonical original source path and immutable UTC launch-request time
before preparation/execution. Retain these through startup failure and shutdown.

Resolve identity and policy at CLI/control boundaries and pass concrete values
through `AgentLayout`, `LaunchSpec`, and `ServeSpec`. Guest path translation
must preserve the host-resolved name, launch ID, and discovery bindings.

## Uniform storage

Placement changes root calculation and policy inputs. Every placement uses the
same paths below `agent_root`:

```text
<agent_root>/
  .state/root/
  .sandbox/<agent_name>/launches/<launch_id>/
  agents/<agent_name>/
    agent.too
    config.toml
    psyches/  skills/  services/  prompts/  chores/
    .state/
    .runtime/
      status.json
      agent.log
      runs.db
      stats.db
      chat-input.jsonl
      logs/
      tools/
```

Paths describe storage locations; existing source/configuration policies decide
which inputs are present. Preserve roaming project discovery, dotenv precedence,
source links, authorized workspaces, and watchers. Do not replace project inputs
with global resident configuration. Prepared state is local to the new root;
cross-launch roaming state reuse is outside this proposal.

Project-authored files remain shared inputs. Preserve their existing edit
coordination at `<source_dir>/.toolang/.state/root/edit.lock`; independent launch
locks cannot serialize edits to the same project file. Pass this source-owned
guard explicitly to editing/projection callers. The parent `.toolang` is project
storage, not another Toolang root or an executing agent root.

Remove the extra `.runtime/instances/<execution_id>/` and sandbox instance layer.
Each agent home's `<agent_home>/.runtime/status.json` has one current lifecycle
entry, using the existing default entry in the status document. Retain local
status and history after exit for all placements. Resident and visiting retain
successful settings there for restart.

For example, with Toolang root `/Users/bryan/.toolang` and script
`/work/demo/aid.too`, the status paths are:

```text
/Users/bryan/.toolang/agents/gus/.runtime/status.json
/tmp/lex@v<source_id>/agents/lex/.runtime/status.json
/work/demo/.toolang/aid@r<launch_a>/agents/aid/.runtime/status.json
/work/demo/.toolang/aid@r<launch_b>/agents/aid/.runtime/status.json
```

The two roaming executions do not share a status file, database, prepared state,
or tool working directory. Host and guest controllers for the *same* instance
still cooperate on its status file using the existing control/workload ownership,
short claims, atomic replacement, and launch fencing. Docker continues to mount
the target agent home and required inputs, not the global discovery directory.

## Executor reuse and startup compatibility

Resident and visiting have one active executor per instance name. Resolve the
complete effective startup bindings before deciding whether to attach, including
values inferred from environment, configuration, remembered settings, and
command defaults. Compare normalized values, not flag spelling or whether a
flag was explicitly supplied.

The startup contract covers source/root and input-policy bindings; sandbox
selector and resolved plugin configuration; bind/advertised address and port;
drivers and lifetime policy; workspace additions and initial working directory;
model catalog and development artifact; ceiling/default/limit/compact overrides;
logging settings; and environment/dotenv source bindings and effective Toolang
startup options read from them. Record this normalized contract with the launch.
Do not compare the entire inherited shell environment: unrelated process/session
variables are not startup options. Compare sensitive sandbox/bootstrap values
through fingerprints and keep their values out of discovery and diagnostics.

Equal bindings attach to the existing executor. A difference rejects the request
with the mismatched field names and instructions to stop/restart; it must not
silently reuse, mutate, restart, or create another executor. Apply this to both
explicit and implicit differences. Equivalent explicit values are accepted.
Existing live source/config watchers remain active; compatibility compares boot
bindings and overrides rather than freezing every watched source's contents.
Explicit lifecycle promotion retains its existing API and updates the published
effective lifetime binding.

Run arguments, messages, thread selection, and request-scoped model options are
execution inputs and may differ between requests on one executor. Progress/UI
preferences, lease IDs, process IDs, and launch IDs are not compatibility inputs.

Serialize competing starts with existing management ownership. Compatible callers
wait for and share the same starting generation; incompatible callers fail.
Recheck the generation before attachment, and retain existing leases/idle
shutdown behavior. An old or incomplete startup contract cannot prove
compatibility and requires a restart.

Roaming never attaches to another invocation. Each launch owns an executor and
may independently select host or guest and different valid startup parameters.
Concurrent launches must not overwrite each other's bindings or stop each
other's workload. Isolation does not grant exclusive ownership of explicitly
shared external workspaces or allow two listeners to bind an occupied port.

## Runtime and ports

Use the existing AgentCore, Agent API application factory, and server lifecycle
for every executor. A host foreground script runs its invocation and API in the
same workload process and event loop; both use the same executor and RunStore.
A guest uses the existing sandbox launch and API execution path. There is one
listener per executing instance, with no Top-only application, second executor,
or separate observation endpoint. Preserve script output, exit status, error,
signal/cancellation behavior, and driver gating; ordinary scripts enable no
background drivers. Startup or bind failures fail the invocation and finalize
its registration rather than executing without observable activity.

| Placement | Automatic port policy |
| --- | --- |
| Resident | Reuse the last successful available port, otherwise allocate from `7002–7999` |
| Visiting | Reuse the last successful available port, otherwise request an OS-assigned port |
| Roaming | Request a fresh OS-assigned port for each launch |

Preserve existing explicit port inputs and their precedence; a specified port
must bind or fail. Only resident automatic allocation uses the fixed range;
exclude Hub port `7001` and active reservations. For attachment, an automatic
port request resolves to the active instance's port before compatibility checks;
do not probe it as if starting another listener. Current explicit/configured
port inputs still take precedence and can cause a mismatch. Persist successful
endpoints only after readiness. Allocation must handle competing binds without
overwriting another registration; OS assignment does not guarantee different
port numbers across sequential, non-overlapping launches.

## Global discovery and activity

Use only `<toolang_root>/.runtime/instances/` to enumerate instances. File names
are exactly `<instance_name>.json`:

```text
/Users/bryan/.toolang/.runtime/instances/
  gus.json
  lex@v<source_id>.json
  aid@r<launch_a>.json
  aid@r<launch_b>.json
  register.lock
```

Keep the existing lightweight reference contract: version, absolute local
`status.json` path, and launch ID. Local status remains the lifecycle authority;
following a reference is not an additional directory scan. Validate the saved
instance name, root, and launch before accepting it. Share this discovery reader
between Top, Hub/runtime enumeration, and port reservations. Do not supplement
it by scanning installed residents, temporary roots, or project directories.
`too list` retains its separate installed-agent purpose.

Publish when control claims a new active launch, including preparing/starting,
and remove after normal stop/release or finalized startup failure. Keep stopping
instances while their workload/resources remain active. Publication and removal
retain the existing short registry lock and launch-ID checks; an old generation
cannot remove its replacement. Stopped local status and remembered ports do not
keep a global reference alive.

Abnormal exits may leave files. Readers validate liveness using existing
PID/creation-time or sandbox identity probes: a file alone is not proof of a
running instance. Preserve existing targeted recovery, bounded probes, and
unknown-state diagnostics; add no background stale-file or root sweeper.

Top membership/counts come from active registrations, rather than installed
agents. Instance lifecycle state is distinct from the number of active root
runs. A fresh Top session does not scan stopped roots for historical membership;
an existing session retains already observed history for its current windows.
Preserve snapshot consistency, completion pagination, source-generation fencing,
and independence from the optional archival stats service. Unavailable snapshots
remain explicitly unknown; healthy running scripts must supply live activity
and actual run rows instead of `- active` caused by a missing listener.

## Instance selection and offline history

Retain the common resident-name, visiting-URL, and local-source target forms.
Add `--instance`, with alias `-i`, to commands addressing an existing instance or
its history. The selector belongs to that target and must match its placement:

| Target | Default selection | Explicit selector |
| --- | --- | --- |
| `gus` | Its stable resident instance | `gus` |
| `https://toolang.ai/lex.too` | Its stable URL-derived visiting instance | `v<source_id>` |
| `/work/demo/aid.too` | Its most recently requested roaming launch | `r<launch_id>` |

```sh
too gus info
too https://toolang.ai/lex.too info
too /work/demo/aid.too info
too /work/demo/aid.too info --instance r<launch_id>
too /work/demo/aid.too inspect -i r<launch_id> runs
```

Roaming selectors include the `r`; raw launch IDs are invalid. Visiting
selectors include `v` and must match the target's source ID. A mismatched kind or
source is an error. Resident/visiting selection does not identify an old process
generation: their instance names and homes persist across restarts.

`r<launch_id>` resolves directly to
`<source_dir>/.toolang/<agent_name>@r<launch_id>`; validate its saved source and
launch bindings before use. Without the option, choose the last launch requested
for that canonical source, including preparing, running, stopped, and failed
launches. "Latest" means the greatest immutable launch-request timestamp saved
before preparation, using UTC nanosecond resolution and launch ID to break ties.
It is not readiness, completion, active status, or file modification time.
If A starts, B starts, then A finishes, B remains the default.

Find the default by reading matching instance metadata only under that source's
local `.toolang` directory. Check saved provenance, since different source names
can normalize to the same agent name. Missing/corrupt records are diagnostic;
do not silently choose an older valid launch when a candidate's ordering or
identity cannot be established. If no instance exists, report that without
materializing one. An explicit unknown selector must not fall back to the latest.
Offline selection does not require the original source file to remain readable
when its canonical location and retained metadata still establish the binding.

`info` and `inspect` can read exited instances. Operations requiring a live
executor, such as `cancel` or `steer`, report an exited selected instance instead
of targeting an older running one or implicitly starting a replacement. Retain
existing source-command input policies and stable resident/visiting targeting.
Runtime/history selection publishes no active registration. This scoped history
lookup is separate from Top's global-only runtime discovery; it adds no permanent
global history index.

Ordinary script execution, `chat`, and `serve` still allocate a fresh roaming
instance. Instance selection is for existing-instance commands and cannot turn
a new source execution into executor reuse. Keep source targets unchanged; no
decorated source-path or agent-root-directory target syntax is added.

## API grouping

Rename the OpenAPI `top` group to `activity` and use that vocabulary internally.
Keep the existing method, URL, query, response, and version contracts, including
`GET /api/v0/top/snapshot`. Existing groups `agent`, `stats`, `runs`, `threads`,
`caps`, and `chores` remain. `agent` already exists as a group without an
`/agent` path prefix; this change introduces no `/activity` or `/agent` prefix.
All placements use the same application contracts and existing driver checks.

## Implementation touchpoints and order

1. Update [layout](../../src/toolang/common/layout.py),
   [source materialization](../../src/toolang/up/source.py), and script/Source Ref
   call sites to allocate one explicit launch identity and uniform paths;
   preserve source-owned configuration and edit guards in projection/catalog
   callers rather than deriving shared-input ownership from the new agent root.
2. Resolve and compare startup bindings in
   [agent acquisition](../../src/toolang/cli/common/agent_server.py),
   [sandbox orchestration](../../src/toolang/up/sandbox.py), and
   [server assembly](../../src/toolang/up/server.py); preserve plugin factories.
3. Adapt [local records](../../src/toolang/up/instance.py),
   [process control](../../src/toolang/up/process.py), and
   [discovery](../../src/toolang/up/observation.py) for per-root ownership,
   active-only membership, and placement-specific port allocation.
4. Route foreground script execution through the shared core/application
   lifecycle, preserving CLI output/control behavior. Update
   [Top](../../src/toolang/up/top.py) and its shared inventory consumers. Extend
   [CLI routing](../../src/toolang/cli/toolang/routing.py) and execution-resource
   selection for `--instance`/`-i`, default latest selection, and offline reads.
5. Rename the API router module/group to `activity`, preserving `/top` URLs;
   update [API assembly](../../src/toolang/api/app.py) and router imports.
6. Add the acceptance coverage below, update current design docs only with the
   implementation, and generate its changelog through `too aid.too update_changelog`.

## Acceptance and verification

Use deterministic executor/model/tool gates, temporary roots, and fake sandbox
plugins in the default offline suite. Test observable behavior and saved state.

| Scenario | Pass condition |
| --- | --- |
| All placements | Exact names/root paths and identical relative storage paths; no nested execution directory |
| Launch identity | Canonical source aliases agree for a fixed nonce; changed path or nonce changes the ID; guest translation preserves the host ID |
| Repeated resident/visiting requests | Compatible explicit and inferred bindings retain the same executor, PID, launch ID, and history |
| Startup differences | Each startup-binding category, including implicit changes, rejects without altering the active instance; secrets stay redacted |
| Competing starts/restarts | One compatible executor wins; mismatches fail; stale readiness, attachment, writes, and removal cannot affect a newer launch |
| Parallel roaming invocations | Same source runs with differing parameters on host and fake guest; distinct roots, stores, registrations, and cancellation ownership |
| Shared project inputs | Both launches retain the original configuration/workspace policy and coordinate concurrent authored-file edits with the same guard |
| Foreground script lifecycle | One host workload process/executor; output, exit/errors, cancellation, startup failure, and listener teardown remain correct |
| Ports | Stable restart preferences, busy-port fallback, explicit conflicts, Hub exclusion, automatic active attachment, and concurrent allocation behave as specified |
| Global-only discovery | Valid nonresident records appear without installed agents; unregistered/stopped homes do not appear; missing/malformed/stale/unknown records stay bounded and diagnostic |
| Instance selection | All three target forms and both option spellings select exactly; `r`/`v` markers are validated; latest requested includes stopped/failed launches and survives global registration removal |
| Ordering and selection failures | A finishing after a newer B cannot replace B; touching files cannot reorder launches; unknown selectors never fall back; ambiguous/corrupt metadata fails without guessing; history queries create no executor/root/registration |
| Active execution | Paused model/tool work appears with correct active count and rows; completion/cancellation transitions are consistent and require no archival stats service |
| API compatibility | Existing routes/schemas/cursors/errors and driver checks persist; OpenAPI uses `activity` instead of `top` |
| Finalization and retention | Normal exit removes only its global reference and retains local records/data; sibling instances and remembered successful settings survive |

Likely test locations: `tests/unit/common/test_layout.py`, the existing
`tests/unit/cli/test_agent_server.py` and `test_script_command.py`,
`tests/unit/up/test_{runtime_observation,records,server,sandbox,top_observer}.py`,
`tests/unit/api/test_top_api.py`, and sandbox lifecycle integration coverage.
Guest mount/status replacement behavior also requires the existing opt-in Docker
checks before claiming real Docker validation.

Before committing implementation, run the repository's default verification:
`uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`, and
`uv run pytest -n auto`. This definition alone requires source/link/example
verification and `git diff --check`.

## Upgrade, risks, and approval

Stop old workloads with the old version before upgrading. Do not attach to
unverifiable legacy startup bindings. Resident roots and remembered settings
remain; visiting selects the new directory spelling and may change port on its
first new launch. Leave old `/tmp/toolang-<name>-<source_id>` roots, shared
roaming roots, and nested execution histories untouched for explicit migration.
New roaming launches do not import old history/state. Legacy registrations must
not be adopted as new-format identities or overwritten while still active.
Rollback also requires stopping the new workloads first.

Strict startup comparison can reject requests previously attached despite
different defaults. Fresh roaming roots increase preparation cost and retained
disk usage; later cleanup needs a separate approved design. Visiting state and
port memory remain subject to OS temporary-directory cleanup. Existing
cooperative guest-status trust and configured bind/access policies remain.

Human review and explicit approval of this complete definition are required
before implementation; the earlier approval for draft #882 does not approve
this replacement. Open questions: none pending; the complete proposal still
requires human confirmation.
