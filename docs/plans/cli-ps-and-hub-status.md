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
  (comma-separated) and service ports; missing/stopped values use `-`.
- Count displayed instances and confirmed running instances. `ps` also counts
  each placement and adds `[teaming].hub` availability using existing bounded
  readiness checks, without reading hub process files. No type column or tree.
- Bare `too hub` shows help. `status` inspects this ROOT's managed process via
  `[hub]`/`hub/pid.json`: actual bind address/PID and its `/info` version
  (`unknown` on lookup failure), independently of client settings and agents.
- Preserve lifecycle, unknown/intermediate states, partial diagnostics, and
  terminal conventions. Configuration, protocol, persistence, authentication,
  compatibility, and migration changes are outside scope.

`too ps` footer (always two lines, including zero placement counts):

```text
4 agents: 2 resident, 1 visiting, 1 roaming, 3 running
hub running: http://127.0.0.1:7001
```

Unavailable target: `hub not running: <url>`; this reports client readiness,
not whether the server process exists.

`too ls` footer (resident counts only; no placement breakdown or hub line):

```text
2 agents: 1 running
```

`too hub status` prints one status line:

```text
hub running: version <version>, pid 10402, endpoint 127.0.0.1:7001
hub not running
```

Hub help description and command order:

```text
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
- [ ] Cover both listings: resident-only/mixed/empty inventories, duplicate names,
  stopped/unknown states, drivers/ports, and partial/narrow/plain output.
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
