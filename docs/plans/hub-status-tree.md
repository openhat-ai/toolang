# Hub instance snapshot

Status: Definition-only PR requested on 2026-10-16. Merge and implement after
[#823](https://github.com/openhat-ai/toolang/pull/823), which replaces
`too hub status` with bare `too hub`. No implementation is included here.

## Goal and scope

Show Hub state and agent instances in one `too hub` snapshot, even without Hub.
Include installed residents plus discoverable preparing/starting/running or
unresolved visiting, roaming, and temporary instances under the selected Toolang
root. Exclude finished nonresident instances and history. `too` aliases `toolang`.

Preserve #823's routing, lifecycle commands, help, and exit behavior. Live refresh,
interaction, new flags, presence queries, protocol changes, and startup are outside scope.

## Layout

Illustrative output after #823; the final two agent rows have dim text:

```text
$ too hub
hub running · 6 agents · 4 running · 3 talk enabled

HUB                        AGENT     STATUS        TALK
                        ┌─ alice     running       enabled
                        ├─ carol     running       enabled
Hub                     ├─ dorion    running       enabled
running · v0.4.0a8      ├╌ bob       running       disabled
                        ├╌ dave      not running   -
                        └╌ erin      not running   -
```

- Summary: one plain line, then one blank line. Counts are listed instances,
  confirmed running instances (including idle ones), and running instances with
  Talk enabled. Distinct instances count separately.
- Dim headings: `HUB`, `AGENT`, `STATUS`, `TALK`. Left-align the Hub block; its
  name is always normal and state/version always dim. Omit endpoint, legend,
  background bar, and horizontal feed from Hub to the agent spine.
- All lines are thin and normal intensity. Spine solid; branches solid for
  `enabled`, dashed otherwise. They do not indicate connectivity or delivery.
  Running agent text is normal; other rows/continuations are dim.
- Sort running rows by Talk enabled, disabled, then unknown; unresolved lifecycle
  rows next; nonrunning rows last. Break ties by name and stable instance ID.
  No status groups or historical Talk selection.
- Center the complete Hub block for at most 10 rows fitting the terminal height;
  otherwise top-align it. Clamp offsets to zero; pipes/unknown height use the same
  row cutoff. Empty lists top-align Hub, show dim `No agents`, and have zero counts.
- List every instance once. Preserve distinguishing instance/placement labels.
  Ordinary rows fit 80 columns; narrow layouts keep Hub left and agents right,
  wrapping within columns without truncation. Large lists use scrollback or caller
  pagination. Respect `NO_COLOR`, `TERM=dumb`, pipes, and encoding; plain/ASCII
  output retains readable values and branch styles without ANSI/cursor controls.

## State rules

| Observation | Agent `STATUS` | Agent `TALK` |
| --- | --- | --- |
| Confirmed running launch | `running` | `enabled` / `disabled` from that launch's recorded drivers. |
| Confirmed stopped/failed workload, or resident never started | `not running` | `-`, regardless of saved drivers. |
| Preparing/starting, unknown liveness, or inspection failure | `-` | `-`; report the observed state/reason separately. |

Missing/invalid drivers yield `-`, never `disabled`; do not read edited config or
guess defaults. Enabled describes driver selection, not Hub/polling health.
Unknowns never count as running/enabled. Incomplete discovery/lifecycle/driver
inspection adds `· partial` and concise diagnostics below the list.

Retain [Hub state/version discovery](hub-status-version.md): `running`, `starting`,
and `unavailable` keep their meanings; display `stopped` as `not running`. Summary
and Hub block use the same state. Live records show state/version; absent/stale
processes omit version. Inspection failure shows `hub -` plus a diagnostic without
hiding local agents. Supersede that plan's layout/endpoint display only.

## Implementation touchpoints

- [Hub CLI](../../src/toolang/cli/toolang/commands/hub.py): extend #823's callback
  using a command-local `commands/hub_status.py` renderer and existing Rich support.
  Reuse [HubProcess](../../src/toolang/up/hub.py) for bounded health/version requests.
- [Resident discovery](../../src/toolang/up/discovery.py),
  [runtime references](../../src/toolang/up/observation.py), and
  [AgentProcess](../../src/toolang/up/process.py): expose required entries without
  changing `running_instances()` callers. Read each document once; deduplicate by
  status path, instance key, and launch ID. Keep safe confirmed-dead correction.
- [Server setup](../../src/toolang/up/server.py) records effective launch drivers.
  Match status/drivers to the same validated launch. Batch Docker probes, retain
  bounded timeouts, cap independent probes at eight, and add no per-agent HTTP.
- Extend `tests/unit/cli/test_hub_commands.py`, add renderer tests, and retain
  `tests/unit/up/test_runtime_observation.py` coverage. Implementation updates
  `docs/messaging.md` and generates Unreleased through `too aid.too update_changelog`.

## Acceptance checks

| Scenario | Pass condition |
| --- | --- |
| Dependency/routing | After #823, snapshot/help/lifecycle routes work; the removed status subcommand stays removed. |
| Layout | Cases with 0/1/6/10/11 rows, wrapped rows, and taller Hub blocks satisfy placement/spacing rules without overlap or negative offsets. |
| State matrix | All table cases, including missing drivers and inspection failures, produce correct words, counts, sorting, and styles. |
| Hub independence | Stopped/starting/unavailable/replaced/malformed Hubs retain correct readiness/version semantics and never hide or disable agents. |
| Identity | Duplicate references count once; same-name instances remain distinct; stale launches cannot supply current drivers/status. |
| Terminal/scale | At 120/80/40/20 columns and 100/300/1,000 instances, labels stay complete, rows/counts/order are deterministic, plain output has no controls, and Hub request count stays constant. |

Implementation tests use fake probes/offline transports and pass the repository's
default verification. This definition changes no code, public guide, or changelog.

## Risks and open questions

Processes may change after inspection; bounded sandbox probes still add latency.
The new human-readable output and hidden endpoint affect parsers; no machine
format is added. Open questions: none. Reconcile routing with #823's merged form.
