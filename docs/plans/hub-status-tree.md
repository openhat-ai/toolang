# Hub instance snapshot

Status: Approved for implementation on 2026-10-16 after the user explicitly
requested the revised output and state styling. Implement after
[#823](https://github.com/openhat-ai/toolang/pull/823), which replaces
`too hub status` with bare `too hub`.

## Goal and scope

Show Hub state and agent instances in one `too hub` snapshot, even without Hub.
Include installed residents plus discoverable preparing/starting/running or
unresolved visiting, roaming, and temporary instances under the selected Toolang
root. Exclude finished nonresident instances and history. `too` aliases `toolang`.

Preserve #823's routing, lifecycle commands, help, and exit behavior. Live refresh,
interaction, new flags, presence queries, protocol changes, and startup are outside scope.

## Layout

Illustrative output (the Hub line is one of the two alternatives):

```text
hub v0.3.4
├── gus                    running       enabled
├── merkle                 running       disabled
├── hak                    not running   -
├── alice                  not running   -
└── bob                    not running   -

5 agents, 2 running, 1 talk enabled
```

When the Hub is not online, show `hub not running` instead of the version line.
Do not repeat the Hub line after the summary. The three agent columns have no
header row: a wider agent/tree column, status, and Talk. Keep the agent names
left-aligned after the tree branch; right-align the final Talk column. Size
columns to their content and wrap only when the terminal is narrower than the
required layout.

- Print the Hub line first, then every agent once, then a blank line and a plain
  summary with total listed agents, confirmed running agents, and running agents
  with Talk enabled. Incomplete discovery adds `partial` and concise diagnostics
  after the summary. An empty roster shows normal `no agents` without a summary;
  diagnostics remain visible if discovery fails.
- Keep the entire Hub line normal, whether it displays a version or `not running`.
  Agent names, tree branches, summary, and unclassified values are normal.
  `running` and `enabled` are normal positive states; `disabled`, `not running`,
  and `-` are dim negative states. Do not dim an entire row because one field is
  negative.
- Use `├──` for non-final branches and `└──` for the final branch. The tree is
  visual grouping only and does not indicate connectivity or delivery.
- Preserve distinguishing instance/placement labels and complete values. Large
  lists use scrollback or caller pagination. Respect `NO_COLOR`, `TERM=dumb`,
  pipes, and encoding; plain/ASCII output retains readable values and has no
  ANSI/cursor controls.

## State rules

| Observation | Agent `STATUS` | Agent `TALK` | Style |
| --- | --- | --- | --- |
| Confirmed running launch with Talk | `running` | `enabled` from that launch's recorded drivers. | Both normal. |
| Confirmed running launch without Talk | `running` | `disabled` from that launch's recorded drivers. | `running` normal; `disabled` dim. |
| Confirmed stopped/failed workload, or resident never started | `not running` | `-`, regardless of saved drivers. | Both dim. |
| Preparing/starting, unknown liveness, or inspection failure | `-` | `-`; report the observed state/reason separately. | Negative markers dim; other text normal. |

Missing/invalid drivers yield `-`, never `disabled`; do not read edited config or
guess defaults. Enabled describes driver selection, not Hub/polling health.
Unknowns never count as running/enabled. Incomplete discovery/lifecycle/driver
inspection adds `, partial` and concise diagnostics below the list.

Retain [Hub state/version discovery](hub-status-version.md) and its readiness
classification. A ready Hub displays `hub <version>` (or `hub running` if version
is unknown); any non-ready/stopped/inspection-failure state displays `hub not
running` in normal style. Keep concise diagnostics without hiding local
agents. Supersede that plan's layout/endpoint display only.

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
| Layout | The version/Hub line precedes a headerless three-column tree; the summary follows one blank line after nonempty rows, with no duplicate Hub footer. An empty roster has no count summary. Narrow terminals wrap all three columns without dropping values. |
| State matrix | All table cases, including missing drivers and inspection failures, produce correct words, counts, and field-level positive/negative styling. |
| Hub independence | Stopped/starting/unavailable/replaced/malformed Hubs retain correct readiness/version semantics and never hide or disable agents. |
| Identity | Duplicate references count once; same-name instances remain distinct; stale launches cannot supply current drivers/status. |
| Terminal/scale | At 120/80/40/20 columns and 100/300/1,000 instances, labels stay complete, rows/counts/order are deterministic, plain output has no controls, and Hub request count stays constant. |

Implementation tests use fake probes/offline transports and pass the repository's
default verification. Generate the user-facing Unreleased entry through the
specified changelog runnable; no machine-readable output format is added.

## Risks and open questions

Processes may change after inspection; bounded sandbox probes still add latency.
The new human-readable output and hidden endpoint affect parsers; no machine
format is added. Open questions: none. Reconcile routing with #823's merged form.
