# Process titles and server discovery

Status: server discovery implemented. Process-title rewriting was withdrawn on
2026-09-21: the CLI now preserves the interpreter's name and argv. The title
design below is historical; sandbox identity, registration, and discovery remain
in effect. Native executable naming is deferred to a future implementation.

## Goal and scope

Expose agent scope and command responsibility in process titles, while making
server discovery independent of argv and display names. Reuse the sandbox
control model for managed host servers, direct host `_serve`, and guest servers.
Do not introduce a second PID registry or a separate server-management service.

## Current behavior

- CLI entry points do not currently set process titles.
- `AgentProcess.pids()` scans command lines. Its root/`--agent` match accepts an
  unrelated program, while a title such as `too:alice _serve` no longer matches.
- `AgentProcess.status()` separately trusts PID existence from runtime status;
  that does not establish process identity after PID reuse.
- `<root>/.sandbox/<agent>/state.json` already stores a `SandboxState` containing
  the sandbox selector and its `SandboxRef`. Host refs record PID and, when
  available, `ps lstart`; guest refs identify adapter-owned workloads.
- Launch holds `state.lock` while saving the reference and waiting for readiness.
  Stop uses the same lock; foreground cleanup conditionally clears the exact ref.
  Direct `_serve` currently bypasses sandbox registration.

## Withdrawn process title contract

Use `too[:agent] <actual-command> [arguments]` for both `too` and `toolang`,
including module invocation. The separate `caps` executable is out of scope.

| Process | Title |
| --- | --- |
| Agent chat | `too:alice chat --thread term_x` |
| Actual server | `too:alice _serve ...` |
| Foreground launcher / output follower | `too:alice serve ...` |
| Start/stop management command | `too:alice start ...` / `too:alice stop ...` |
| Standalone Script | `too run task.too main ...` |
| Global command | `too new psyche ...` |

Use existing routing results for the command and canonical agent. A Script's
implicit `run` uses its existing routed command; introduce no `script` or `server`
display alias. A global creation command's new name stays an argument. Internal
Script layouts and arbitrary argument values do not establish agent scope.

Preserve explicit remaining arguments, shell-quoted for display, after removing
the executable and consumed agent selector. Do not append resolved defaults,
environment values, stdin, or internal launch tokens. Escape whitespace/control
characters and `%` in the agent label as percent-encoded UTF-8 so the first token
remains one label. OS truncation does not affect identity.

Capture argv and import `setproctitle` before environment mutation. Apply titles
at resolved CLI call sites; do not mutate `sys.argv` or parse the title to start
children. Title-setting failures do not block commands. Apply server titles only
after the reference is registered/confirmed and before agent preparation.
Titles apply without a TTY too. Preserve existing TTY-only OSC conversation titles,
exit cleanup, tmux metadata, and `TOOLANG_TMUX` semantics.

## One authoritative runtime reference

```text
<root>/.sandbox/<agent>/
    state.json       # Sole authoritative workload reference
    state.lock       # Existing management-operation lock
    launches/        # Existing adapter staging, when needed
<agent-home>/.runtime/status.json  # Descriptive status, not process ownership
```

Reuse `SandboxState`/`SandboxRef`; add host-specific identity fields inside the
host ref rather than another record. Root and canonical agent are scoped by the
control path. Host refs use PID, OS process creation time, and signal scope;
managed launches additionally carry a per-launch correlation token. Use `psutil`
for new process identity checks. PID existence alone never authorizes a signal.

- Managed host: PID + creation time, signal scope `process_group` because the
  adapter creates a new session. Validate group ownership before signalling it.
- Direct host `_serve`: PID + creation time, signal scope `process`; never send a
  group signal to the invoking shell's group.
- Guest/container: retain the adapter's instance ID and lifecycle operations.
  A guest PID is descriptive only and never becomes a host process reference.

`status.json` retains readiness, endpoint, model and error information. It may
retain a diagnostic PID but must not authorize discovery, stop, or removal.
Correlate host reports using PID plus process creation time and guest reports
using the adapter instance ID; these copied fields remain descriptive. Ignore
mismatching reports. A live ref without matching readiness is starting; probe its
endpoint without using HTTP health as proof of process ownership.

## Registration and launch interruption

| Entry | State writer | Before agent preparation |
| --- | --- | --- |
| `start` / `serve`, host | Parent launcher | Child confirms its saved ref |
| Direct `_serve`, host | `_serve` itself | Save its own host ref under the lock |
| Managed guest | Controller via adapter | Keep existing guest startup contract |
| Chat / Script / management CLI | None for that CLI process | Embedded execution is not a server |

For managed host launches, generate a token in the host adapter, pass it through
an internal environment value, and return it in the host ref. The `_serve` child
consumes that value and waits for the atomic `state.json` snapshot to contain the
same token and its PID/creation time. This acknowledgement reads without taking
`state.lock`: the parent still holds that lock while waiting for readiness.
The token correlates a launch; it is not another identity registry or a public CLI
option. Do not propagate it to unrelated child commands.

A bounded acknowledgement timeout, invalid state, or mismatching replacement ref
ends the child before it prepares files or starts watchers. Thus parent death
before persistence cannot leave an unregistered running server. Parent death
after persistence leaves a discoverable process. Failed launches retain the ref
until the workload is confirmed stopped and adapter cleanup succeeds.

For direct host `_serve`, acquire the existing management lock, validate/release
any stopped ref, reject a live or indeterminate ref, verify that the agent still
exists, and atomically save its own ref. Release the lock before preparation.
Managed children never self-register over the parent's ref. Guest `_serve` never
writes controller state; the sandbox selector determines the execution location.

The server never needs `state.lock` to shut down. It writes descriptive status
and stops its tasks; its ref may remain after exit for the controller or next
management command to validate and release. Cleanup only clears the expected ref,
so an old launcher cannot erase a replacement. Do not use a lifetime-held lock:
validated process identity protects the running interval.

## Discovery, stop, removal, and recovery

- Route discovery through the sandbox adapter and `state.json` for every runtime
  kind. Remove command-line scanning and the bare-PID status fallback. Keep
  `AgentProcess` as a presentation facade rather than a second discovery engine.
- Distinguish workload liveness from API readiness. A live starting ref blocks a
  duplicate start/removal even without `status.json`; no HTTP endpoint alone
  proves ownership. Failure to inspect identity is indeterminate, not stopped.
- Stop uses the validated adapter ref. A dead PID or different creation time means
  the old host workload is gone; never signal the replacement PID. A zombie is
  exited. Missing identity or permission errors require an explicit diagnostic.
- Serialize the final stopped check, resource release, and agent-home deletion
  under `state.lock`. Move this complete removal operation into sandbox
  orchestration. All start paths, including direct `_serve`, recheck the home
  under the same lock. Keep the control lock outside the removed home and do not
  unlink it during ordinary agent removal.
- Normal startup/cleanup must never remove a live ref. Missing `status.json` is
  recoverable from `state.json`. A malformed ref blocks destructive operations;
  an absent ref accompanied by an active status report or unreferenced staging
  requires diagnosis, not title-based adoption or automatic duplicate startup.
- Arbitrary deletion of all control evidence while a workload runs cannot be
  repaired reliably from a process title. Do not promise orphan rediscovery in
  that case. Existing unregistered legacy servers must be stopped before upgrade.
- Preserve guest adapter ownership and namespace boundaries. Reuse valid existing
  host refs with recorded `lstart`; missing legacy identity does not authorize a
  signal. Write creation-time identity and explicit signal scope for new refs.

## Terminal limits and approval boundary

On this macOS host, Python 3.13 with `setproctitle` changed `ps` to
`too:alice chat --thread term_test`, but `proc_name()` and isolated tmux
`pane_current_command` still returned `python3.13`. Python 3.10 behaved similarly.
macOS tmux reads kernel `pbsi_comm`; Linux tmux reads the foreground command line.
iTerm2 has both process-name and argv-derived data; its installed UI is unverified.

The title/discovery design does not promise replacement of macOS native job names.
If that is mandatory, investigate executable/startup architecture before declaring
this feature implementable. Additional OSC/user-variable presentation needs a
separate concrete format and cleanup decision; existing conversation titles stay
unchanged. The user has not yet selected between these terminal approaches.

## Touchpoints and acceptance

Likely files: `cli/toolang/main.py`, existing routing/context hooks,
`cli/toolang/commands/{runtime,script,agent}.py`, a narrow title helper,
`up/{process,server,records,sandbox}.py`, `plugin/sandboxes/host.py`,
`pyproject.toml`, `uv.lock`, and relevant CLI/up/host tests. Preserve current paths,
public CLI flags, and sandbox plugin factory/entry-point patterns.

1. Verify scoped/global titles, both agent argument orders, implicit Script `run`,
   roaming/visiting agents, `_serve` vs launcher titles, TTY/non-TTY, unchanged
   argv/environment, and title-setting failure. Local execution cores do not
   acquire server identity.
2. Use real subprocesses to verify changed titles do not affect status, stop, or
   removal; distinguish roots, agents, roles, PID reuse, zombies, and namespaces.
3. Kill the parent before/after ref persistence; the child respectively exits
   before preparation or remains discoverable. Cover startup failure, timeout,
   forced exit, missing/stale status, malformed/missing identity, concurrent starts,
   and deletion races. Ensure no parent/child or shutdown lock deadlock.
4. Test direct `_serve` registration/stop without affecting its shell group;
   preserve managed group cleanup and guest adapter lifecycle behavior.
5. Check native job names separately from OSC titles on macOS/Linux and manually
   verify iTerm2 before claiming terminal support. Run Ruff lint/format, ty, and
   the offline pytest suite before implementation commits.

Implementation does not assert that macOS native job-name replacement has been
achieved. Changes to executable/startup architecture need a separate design.

## Sources

- [setproctitle](https://github.com/dvarrazzo/py-setproctitle)
- [tmux macOS lookup](https://github.com/tmux/tmux/blob/master/osdep-darwin.c)
- [tmux Linux lookup](https://github.com/tmux/tmux/blob/master/osdep-linux.c)
- [iTerm2 process information](https://github.com/gnachman/iTerm2/blob/master/sources/ProcessInfo/iTermProcessInfo.swift)
