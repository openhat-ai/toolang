# Plan

Define one runtime listing behind `too ps` and resident-only `too ls`, separate
from `too hub` service management. This is a definition; implementation follows
approval.

## Scope

- Remove root `too list`; place `ps` before `top` in **Inspection commands** and
  `ls` in **Agent commands**. No filtering options or aliases; nested `list` stays.
- `ps` uses the existing ordered ROOT inventory: all residents, including stopped
  ones, plus preparing/starting/running or unresolved visiting/roaming instances.
  `ls` is a thin wrapper over the same discovery/projection/renderer, selecting
  residents only and skipping hub configuration/probes and the hub footer.
- Both show only `NAME`, `STATUS`, `DRIVERS`, and right-aligned `PORT`, using
  `top`'s complete `name`/`name@id` addresses. Show observed launch drivers
  and service ports, using the state and metadata rules below.
- Count displayed instances and confirmed running instances. `ps` also counts
  each placement and adds `[teaming].hub` availability using existing bounded
  readiness checks, without reading hub process files. No type column or tree.
- Bare `too hub` shows help. `status` inspects this ROOT's managed process via
  `[hub]`/`hub/pid.json`: actual bind address/PID and its `/info` version
  (`unknown` on lookup failure), independently of client settings and agents.
- Preserve lifecycle, unknown/intermediate states, partial diagnostics, and
  terminal conventions. Configuration, protocol, persistence, authentication,
  compatibility, and migration changes are outside scope.

## State presentation

Keep runtime/API values unchanged. `ls`, `ps`, and agent `info` share this mapping:

| Runtime observation | Display | Meaning |
| --- | --- | --- |
| `preparing` | `preparing` | Preparing source/environment. |
| `starting` | `starting` | Launch underway, not ready. |
| `running` | `running` | Running, including idle instances. |
| `stopped` / never started | `not running` | No active runtime. |
| `failed` | `failed` | Confirmed launch/workload failure. |
| Inspection/ownership error | `unknown` | Runtime state cannot be verified. |

- `ls`/`ps` show labels in STATUS and reasons below the footer; `info` retains
  uptime/reason details. Inspection errors mean `unknown`, never `failed`/stopped.
- `hub status`: `starting`/`running` from verified process state; absent/stale
  process is `not running`; corrupt records/denied inspection are `unknown`.
  Version lookup failure does not change process state.
- `ps` uses `hub <status>: <url>`: `running` after valid metadata/readiness,
  `not running` after
  connection/readiness failure, `unknown` for invalid configuration/response.
  Unknown URL is `-`. These report availability, not server lifecycle.
- `top` retains Run/Step `pending`/`running`/`succeeded`/`failed`/`canceled` and
  operation labels. `unavailable`/`stale` qualify observation quality, not agents.
- DRIVERS reuses `format_drivers`: `chat,chore,talk`, `none` for known empty,
  `-` for missing/inapplicable. PORT uses `-` when missing; version uses `unknown`.
  Metadata failure never downgrades a confirmed `running` instance.
- Count displayed rows; running count is `-` when runtime state or coverage is
  unknown. Incomplete agent inspection adds `, partial` to the first footer line.
  Hub probes do not affect agent states/counts. Preserve `top`'s nullable totals.

## Output

`too ps` footer (always two lines, including zero placement counts):

```text
4 agents: 2 resident, 1 visiting, 1 roaming, 3 running
hub running: http://127.0.0.1:7001
```

`too ls` footer (resident counts only; no placement breakdown or hub line):

```text
2 agents: 1 running
```

`too hub status` prints one status line:

```text
hub running: version <version>, pid 10402, endpoint 127.0.0.1:7001
hub starting: version unknown, pid 10402, endpoint 127.0.0.1:7001
hub not running
hub unknown: <reason>
```

`too hub` help, including usage and command order:

```text
$ too hub
Usage: too hub [COMMAND]

Manage the hub service.

Commands:
  status  Show hub status
  start   Start hub in the background
  stop    Stop hub
  serve   Run hub in the foreground
```

## Action items

- [ ] Update `cli/toolang/main.py` routing/help; replace `commands/agent.py`'s
  listing with thin `ps`/`ls` wrappers and extract `commands/hub_status.py` rendering.
- [ ] Reuse `up/watcher.py`/`up/observation.py` for snapshot data and
  `up/hub.py` for process identity checks; simplify `commands/hub.py` accordingly.
- [ ] Share CLI state/driver formatting with `commands/agent.py` info output;
  leave runtime records, API schemas, and `top` Run/Step semantics unchanged.
- [ ] Cover both listings: resident-only/mixed/empty inventories, duplicate names,
  every state mapping above, empty/unknown drivers, missing ports/version,
  uncertain counts, and partial/narrow/plain output. Assert agreement with `info`.
- [ ] Verify both client hub outcomes leave agent rows intact; server status is
  independent of the client URL; `ps` has the two-line footer above, while `ls`
  has only its resident summary and never probes the hub. Metadata/inspection
  failures remain distinguishable from shutdown.
- [ ] Assert command placement, exact help/status wording, bare help without
  probing, root `list` removal, and unchanged lifecycle routing using offline tests.
- [ ] Update CLI docs/examples, generate the changelog through
  `too aid.too update_changelog`, and run repository default verification.

## Open questions

None.
