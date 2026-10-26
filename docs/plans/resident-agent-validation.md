# Resident agent validation and runtime reuse

Status: Proposed definition. The reuse policy was confirmed in discussion on
2026-10-26; this document does not authorize implementation.

Related: [startup validation #975](https://github.com/openhat-ai/toolang/issues/975),
[chore validation #976](https://github.com/openhat-ai/toolang/issues/976), and
[Chat entry check #974](https://github.com/openhat-ai/toolang/pull/974).

## Goal and scope

Reject undefined resident targets before creating directories, locks, prepared
state, or processes. Ordinary `too NAME chat/start` must still reuse a legitimate
running executor when `agent.too` has been removed. Missing source prevents a
new executor generation, including a restart.

Include resident Chat/start/serve validation, chore authoring, and regression
coverage for source queries and existing-home operations. Preserve public
commands, argument orders, flags, visiting/roaming placement rules, runtime
lifetimes, and compatibility requirements. Source watcher behavior, automatic
cleanup of orphan homes, new binding frameworks, and persisted formats are out
of scope. Keep residual files and normal `new/clone` conflict protection.

## Command requirements

Use the existing `source`, `home`, and `execute` command contracts described in
[CLI recognition](../design/cli-command-recognition.md) and the ownership rules
in [agent addresses](../design/agent-addresses.md#binding-and-execution).

| Operation | Required target |
| --- | --- |
| Resident source queries and authoring | A resident home with an `agent.too` file; no running executor required. |
| History and process controls | The selected existing home and required records or owner; source presence is not a prerequisite. |
| Chat/start reuse, including ordinary names | A verified, compatible live owner in the selected home; local source presence is not a prerequisite. |
| Fresh Chat/start or any restart | The selected home's required program and startup inputs. |
| Foreground serve | Source is required for a new launch; an active owner retains the existing rejection behavior. |

A home directory or status file alone proves neither an authored agent nor a
reusable executor. Source availability checks require the program file, not a
default Chat handler or successful parsing of the whole program. Preserve each
operation's subsequent source validation and runnable selection behavior.

For ordinary missing resident sources with no reusable owner, retain
`Agent NAME not found`. Bound-home startup keeps its existing missing-program
diagnostic. Ownership, compatibility, permission, and parsing failures retain
their specific diagnostics rather than becoming missing-agent errors.

## Validation and execution order

1. Complete native argument parsing before target lookup. Preserve eager help,
   completion, missing-argument errors, and unknown-option precedence.
2. Resolve the exact layout and any explicit instance/Thread/Run binding using
   existing resolvers. Do not allocate, fetch replacement source, or switch homes
   for a bound target. A populated `CliContext.layout` is not proof of validation.
3. Perform a read-only resident preflight before the first management or authoring
   operation that can write. Use a shared CLI-owned source check for source
   operations. For execution without source, use the existing read-only
   `RuntimeProbe` and identity/liveness checks to establish an eligible owner;
   do not use observation that repairs runtime metadata at this stage.
4. Reuse the existing management guards for authoritative owner selection.
   Recheck identity and generation, health/API compatibility, drivers, explicit
   requirements, and retention before returning a server or promoting its
   lifetime. Source absence does not relax those checks. Preserve bounded waits
   for legitimate owners preparing/starting/closing; if no reusable owner results,
   missing source forbids launching another generation. Unknown or replaced
   ownership never permits takeover or fallback.
5. When choosing a fresh launch, check the required resident source again under
   the existing guarded startup path before source/State preparation and workload
   creation. Follow normal preparation validation; never substitute an empty
   program for a missing required source. Attachment performs no local source
   preparation, fetch, or fresh-launch default resolution. Start retains its
   existing idempotence, promotion, and foreground-owner restrictions.
6. For `chore new/edit/delete`, validate the resident source before editor use or
   acquiring a catalog write lock. Recheck the source and existing editable-target
   rules under the write lock before saving/deleting. Reject a source removed
   while editing without changing the chore. Preserve malformed Markdown repair
   and inline-definition protections.

Targets already invalid at preflight leave home files and directories unchanged.
Once a valid source or legitimate owner passes preflight, normal coordination,
lease, or promotion writes are permitted. If it disappears during the operation,
final checks must prevent fresh startup or authored-content mutation; this does
not promise rollback of coordination metadata already written for a valid target.

## Implementation touchpoints

| Likely files | Responsibility |
| --- | --- |
| [CLI context](../../src/toolang/cli/common/context.py) and [resident catalog](../../src/toolang/catalog/agent.py) | Reuse the catalog's resident-file predicate through an explicit read-only CLI check. Keep home/layout lookup independent of universal source validation. |
| [Server acquisition](../../src/toolang/cli/common/agent_server.py) and [runtime commands](../../src/toolang/cli/toolang/commands/runtime.py) | Apply preflight before management state creation and distinguish attachment from fresh startup. Replace directory-only startup validation. |
| [Chat entry](../../src/toolang/cli/toolang/commands/chat/main.py) | Let acquisition decide whether source is required. If #974 lands first, replace its eager source check while retaining its missing-target regression coverage. |
| [Chore commands](../../src/toolang/cli/toolang/commands/chore.py) | Apply the shared source check consistently at authoring boundaries. |
| CLI tests listed below; [hosting](../design/hosting.md), [Chat](../design/chat.md), and [CLI recognition](../design/cli-command-recognition.md) docs | Cover the policy and update current documentation alongside implementation. |

Reuse `up.observation`, `up.identity`, and `up.workload` for observation, fencing,
and coordination. Call sites supply concrete layouts and operation intent; do not
infer policy from business parameter names, add source checks to generic State
loaders, or change schemas. Generate implementation changelog entries through
`too aid.too update_changelog`, with maintainer verification before merge.

## Acceptance checks

Use deterministic offline tests with fake runtime/API responses and editor/process
spies. Cover both command orders where supported and `agent:NAME` selectors.

| Scenario | Pass condition |
| --- | --- |
| Missing home or orphan home, no legitimate owner | Chat/start/serve reject without new directories, status, lock files, prepared state, editor calls, or processes; existing files are unchanged. |
| Ordinary name, compatible running owner, missing source | Chat attaches; start reuses/promotes according to existing lifetime rules. Same home/generation is retained and no source preparation or fresh launch occurs. |
| Missing source, incompatible or unverifiable owner | Existing specific error; no fallback, new generation, or lifetime promotion. |
| Legitimate transitional owner, missing source | Existing bounded wait applies; only a compatible resulting live owner can be reused. |
| Owner exits or is replaced after preflight | Guarded checks reject the lost selection when source is absent; no silent restart or attachment to a replacement generation. |
| Source disappears between preflight and fresh-launch selection | Source recheck rejects startup; preparation cannot accept a missing required program. |
| Valid resident source and no live owner | Existing Chat/start/serve startup, drivers, and lifetimes are preserved. |
| Explicit instance or Thread-bound Chat, missing source | Live reuse stays in the bound home; stopped-owner restart fails without fetching replacement source or allocating another home. |
| Existing history/process controls after source removal | Inspection and applicable stop/cancel operations still work using their normal record/owner prerequisites. |
| Missing source for chore authoring | New/edit/delete reject before editor or write-lock effects; pre-existing chore files remain unchanged. |
| Source removed while chore editor is open | Save fails and the original chore file remains unchanged. |
| Native diagnostics, other placements, and named-only runnables | Help/error precedence, visiting/roaming restrictions, and explicit runnable selection retain existing behavior. |

Extend `tests/unit/cli/test_cli_routing.py`, `test_chat_command.py`, and
`test_agent_server.py`; use `tests/integration/cli/test_runtime_commands.py`,
`test_local_core_commands.py`, and `test_roaming_instances.py` for command-level
coverage and existing-home compatibility. Run focused tests, then
`uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`, and
`uv run pytest -n auto`.

## Risks and open questions

The main risks are introducing writes during preflight, trusting stale runtime
metadata, or applying source requirements to home-only operations. Read-only
observation and guarded revalidation address those risks without a new lock or
binding protocol. Keep permission/parsing errors distinct from source absence.

No open product questions remain. Human review of this complete definition is
required before implementation.
