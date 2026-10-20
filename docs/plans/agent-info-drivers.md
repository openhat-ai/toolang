# Agent info drivers

Status: approved by the human on 2026-10-20, including omission when not running.
No implementation is included.

## Goal and scope

Show the agent's enabled execution drivers in `too TARGET info` and
`too info TARGET` while running, immediately after `Status`. Success means users
can see the running agent's driver selection without inspecting runtime files.
Use the existing [driver contract](../design/drivers.md); do not change driver
selection, startup, handlers, API schemas, or other commands.

## Display and data

- Emit one `Drivers` row only while running, directly after `Status` and before
  runtime details.
- When status is `running`, read `drivers` from the runtime state already loaded
  by `AgentProcess.state()`. Reuse `up.config.format_drivers` for canonical order
  and formatting: `chat,talk,chore`, subsets, or `none` for an empty set.
- For every other status, omit the row. Do not show previous-launch values or
  predict defaults for a future launch.
- Missing or invalid running driver metadata also displays `-`; valid metadata
  is a list of distinct names from `ALL_DRIVERS`. This preserves the existing
  best-effort runtime metadata behavior without inventing enabled drivers.
- Reuse existing runtime identity checks and resource loading. Add no HTTP
  requests, source reads, runtime launches, or configuration resolution.

```text
Status:  running (up 2h11m16s)
Drivers: chat,talk,chore
```

## Implementation touchpoints

- `src/toolang/cli/toolang/commands/agent.py`: format the existing runtime
  metadata and insert the running-only row immediately after `Status`.
- `tests/unit/cli/test_agent_info.py`: extend existing info coverage.
- `tests/integration/cli/test_local_core_commands.py`: update the existing row
  order checks and cover nonrunning statuses with stale driver metadata.
- `docs/design/cli.md`: describe the running-only row and its unavailable value.

## Acceptance tests

1. Running agents with all drivers, a subset, or an empty set show the canonical
   value immediately after `Status` and before sandbox/API details.
2. Nonrunning agents omit the row, including when metadata contains drivers from
   a previous launch.
3. Missing, malformed, duplicate, or unknown driver values show `-` without
   breaking otherwise successful info output.
4. Both command orders and resident, roaming, visiting, host, and Docker cases
   preserve existing resource behavior, including no local source loading for
   running agents and no additional runtime requests.

## Risks and open questions

Enabled drivers describe runtime admission, not guaranteed handler/resource
availability. Missing metadata remains unavailable rather than looking disabled.
No open questions remain.
