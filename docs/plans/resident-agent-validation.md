# Resident agent discovery, validation, and runtime reuse

Status: Proposed definition. The reuse policy was confirmed in discussion on
2026-10-26; this document does not authorize implementation.

Related: [startup validation #975](https://github.com/openhat-ai/toolang/issues/975),
[chore validation #976](https://github.com/openhat-ai/toolang/issues/976), and
[Chat entry check #974](https://github.com/openhat-ai/toolang/pull/974).

## Goal and scope

Use one flow: parse and locate the target, discover its owner without writes,
validate the intended operation, then act under existing guards. Ordinary
`too NAME chat/start` may reuse a legitimate running executor after `agent.too`
is removed; missing source prevents a new generation, including a restart.
Undefined targets must not create directories, locks, state, or processes.

Include resident runtime discovery, Chat/start/serve validation, and chore
authoring. Preserve commands, flags, visiting/roaming selection, lifetimes,
compatibility checks, and source-independent history/process controls. New
registries, persistent caches, binding frameworks, source watcher changes, and
automatic orphan cleanup are out of scope. Preserve residual data and normal
`new/clone` conflict protection.

## Command requirements

Follow the existing `source`, `home`, and `execute` contracts in
[CLI recognition](../design/cli-command-recognition.md) and
[agent addresses](../design/agent-addresses.md#binding-and-execution).

| Operation | Required target |
| --- | --- |
| Resident source queries and authoring | An `agent.too` file in the selected resident home; no executor required. |
| History and process controls | The existing home and required records or owner; no source prerequisite. |
| Ordinary or bound Chat/start reuse | A verified compatible live owner in that home; no source prerequisite. |
| Fresh Chat/start or any restart | The selected home's required program and startup inputs. |
| Foreground serve | Source for a new launch; preserve active-owner rejection. |

Source availability means a program file, not successful full-program parsing
or a default Chat handler. Keep operation-specific parsing and explicit runnable
selection. Use `Agent NAME not found` for an ordinary missing source with no
owner to reuse; preserve bound missing-program and specific ownership,
compatibility, permission, and parsing diagnostics.

## Target and runtime discovery

Resolve one exact layout after native parsing and explicit Thread/Run binding.
A supplied `CliContext.layout` is location data, not proof of validity.

- `NAME` and `agent:NAME` select `ROOT/agents/NAME`. Observe that home directly;
  do not require `LocalAgents.get()`/`agent.too`, scan all runtimes, search other
  roots, or adopt a visiting/roaming instance with the same name.
- Explicit instance and record selectors retain their existing resolvers and
  generation bindings. Where a short address needs the active index, use its
  exact-address lookup; do not add alternative-home fallback.
- HOME `.runtime/status.json` is authoritative. The active reference at
  `ROOT/.runtime/active/<address>/info.json` is an index, not liveness evidence.
  Probe known-home status first; if it supplies no owner, consult only that
  address's reference to detect an unresolved ownership claim. A verifiable
  current owner needs no index corroboration. Missing/stale references cannot
  hide it or redirect lookup; a claimed owner with missing/corrupt authority
  remains unknown, not absent.
- Reuse the read-only `up.discovery`/`RuntimeProbe` readers and existing
  `AgentStatus`/`RuntimeSource` information. Validate layout identity, generation,
  and host PID/creation time or sandbox-owned resource identity. A cached
  `running` label, endpoint response, or registry entry alone is insufficient.
  Discovery does not repair status, retire references, acquire mutation locks,
  prepare source, or run `RuntimeWatcher.refresh()`.

Share this scoped observation between command dispatch and acquisition, including
`start`'s retention path. Keep API compatibility/health, driver, explicit-option,
and lease checks in the existing attachment path. Do not add a second registry,
parallel state vocabulary, or independent probe pipeline per command.

## Decision and action

Preserve native help/completion and argument/option error precedence. For execution,
apply this decision to the discovered owner before the first management write:

| Observation | Action |
| --- | --- |
| Running candidate | Verify attachment compatibility; Chat attaches and start reuses/promotes under existing lifetime rules. Failure never falls back to another executor. |
| Preparing/starting, or the existing closing condition | Use existing bounded waiting and generation checks, then reevaluate. Missing source never permits a replacement launch. |
| Absent or conclusively stopped/failed | Start only with the required source. An orphan directory alone does not satisfy that check. |
| Unknown, unreadable, or conflicting ownership | Report the existing diagnostic; do not treat uncertainty as permission to start, overwrite, or take over. |

Admitted actions use the existing management guard. Reread authoritative state
and verify the selected generation before attachment/promotion; for fresh startup,
recheck required source before preparation/workload creation. Repeat observation
only for guarded revalidation, a detected change, or existing bounded waits.
Do not let normal stale-index retirement become a prerequisite for lookup.

If a selected owner disappears or is replaced, retain existing fencing/refusal
rules; missing source must not turn that race into a fresh launch. Attachment
performs no source fetch/preparation or fresh-launch default resolution. Preserve
start's idempotence/promotion and its foreground-owner restriction.

For resident source operations, use the declared `source` contract at the existing
leaf invocation boundary to run one shared read-only check. This covers chore
new/edit/delete before editor or write-lock effects, without callback-specific
opt-ins. Recheck source and editable-target rules under the write lock before
saving/deleting. Preserve malformed Markdown repair and inline protection.

An invalid target at initial lookup leaves files/directories unchanged. After a
valid source or legitimate owner is admitted, normal coordination/lease/promotion
writes are allowed. Later disappearance prevents fresh startup or chore mutation;
coordination metadata already written is not subject to a new rollback protocol.
Keep source requirements out of universal layout getters and generic State loaders.

## Implementation touchpoints

| Likely files | Change |
| --- | --- |
| [Discovery](../../src/toolang/up/discovery.py), [observation](../../src/toolang/up/observation.py), and [registry readers](../../src/toolang/up/registry.py) | Reuse/factor a small read-only known-target lookup with existing result types and authority/index rules. Leave inventory rendering and repair policy with the watcher. |
| [CLI invocation](../../src/toolang/cli/common/help.py), [context](../../src/toolang/cli/common/context.py), and [resident catalog](../../src/toolang/catalog/agent.py) | Apply the existing leaf source contract through a shared resident-file check, separate from home lookup and runtime acquisition. |
| [Acquisition](../../src/toolang/cli/common/agent_server.py) and [runtime commands](../../src/toolang/cli/toolang/commands/runtime.py) | Share observation and guarded execution decisions; validate before management state creation. Replace directory-only startup checks. |
| [Chat](../../src/toolang/cli/toolang/commands/chat/main.py) and [chores](../../src/toolang/cli/toolang/commands/chore.py) | Delegate Chat's reuse/start decision to acquisition; replace #974's eager source gate if it lands first. Retain guarded chore rechecks after common invocation validation. |

Reuse identity/workload guards and schemas. Callers pass concrete layout and
operation intent; do not infer policy from business parameter names. Update
[hosting](../design/hosting.md), [Chat](../design/chat.md), and CLI recognition docs
with implementation. Generate its changelog via `too aid.too update_changelog`
and require maintainer verification before merge.

## Acceptance checks

Use offline fake runtime/API responses and editor/process spies. Exercise supported
argument orders, `agent:NAME`, host/guest identity, and explicit home bindings.

| Scenario | Pass condition |
| --- | --- |
| Missing/orphan home, no legitimate owner | Chat/start/serve reject without new directories, status, locks, state, or processes; existing files are unchanged. |
| Ordinary name, live owner, missing source | Chat attaches; start reuses/promotes the same generation without source preparation or fresh launch. |
| Missing/stale active index, verifiable known-home owner | Targeted lookup still succeeds; no full inventory scan or index repair is required. |
| Reference to wrong root/home/generation, malformed authority, or inconclusive liveness | No wrong-owner adoption or duplicate launch; unresolved authority is not classified as absence. |
| Incompatible live owner | Specific error; no promotion, fallback, or new generation. |
| Legitimate transitional owner | Bounded waiting and generation checks apply; absent source cannot lead to replacement startup. |
| Owner replacement/exit between discovery and action | Guarded checks detect the lost selection; no silent attachment to a replacement or restart without source. |
| Source removed before fresh-launch selection | Source recheck rejects; no missing required program is accepted as empty. |
| Valid source, no live owner | Existing fresh startup, drivers, and lifetimes are preserved. |
| Bound Chat and history/process controls after source removal | Live reuse/control and applicable history operations work; stopped-owner restart fails in the same home without replacement fetch/allocation. |
| Chore authoring with source missing initially or removed in the editor | Initial rejection invokes no editor/write lock; later save/delete preserves the original chore. |
| Discovery work and unrelated behavior | One shared initial observation, with guarded revalidation/waits only; native diagnostics, other placements, and named-only runnable selection are preserved. |

Extend `tests/unit/up/test_discovery.py` and `test_runtime_observation.py`,
`tests/unit/cli/test_cli_routing.py`, `test_chat_command.py`, and
`test_agent_server.py`. Cover end-to-end command boundaries in
`tests/integration/cli/test_runtime_commands.py`, `test_local_core_commands.py`,
and `test_roaming_instances.py`. Run focused tests, then `uv run ruff check .`,
`uv run ruff format --check .`, `uv run ty check`, and `uv run pytest -n auto`.

## Risks and open questions

Primary risks: discovery that mutates state, stale evidence mistaken for a live
owner, duplicated lookup decisions, and source checks leaking into home controls.
Use shared read-only observation followed by existing guarded revalidation;
preserve specific errors and existing coordination protocols.

No open product questions remain. Human review of this complete definition is
required before implementation.
