# Unified agent instances

Historical definition: identity, layout, selectors, and continuation below were
superseded by [agent addresses](../design/agent-addresses.md).

- Approved in [#883](https://github.com/openhat-ai/toolang/pull/883). Replaces [draft #882](https://github.com/openhat-ai/toolang/pull/882).
- Goal: one global registry, uniform layouts, isolated roaming execution, correct Top activity, retained offline history.

## Identity and layout

- `toolang_root`: global discovery/Hub root. `agent_root`: placement root. `agent_home`: `<agent_root>/agents/<agent_name>`.
- Agent names stay unchanged. `source_dir` means the original canonical `.too` parent.

| Placement | Instance ID | Instance name | Agent root | Automatic port |
| --- | --- | --- | --- | --- |
| Resident | None | `gus` | `<toolang_root>` | Last successful available; else `7002–7999` |
| Visiting | `v<source_id>` | `lex@v<source_id>` | `/tmp/lex@v<source_id>` | Last successful available; else OS-assigned |
| Roaming | `r` + 32 lowercase hex digits | `aid@<instance_id>` | `<source_dir>/.toolang/aid@<instance_id>` | Fresh OS-assigned |

- Visiting preserves existing source hashing/alias normalization: canonical Toolang root + NUL + canonical Source Ref, SHA-256 first 16 hex digits.
- Roaming computes once on the host:

```text
instance_id = "r" + hex16(request_order_ns) + SHA256(canonical_source_path_utf8 + NUL + fresh_uuid4_bytes).hexdigest()[:16]
```

- Under `<source_dir>/.toolang/.state/launch.lock`, assign `max(time_ns(), highest_retained_order_ns + 1)` and atomically publish initial identity/status; prepare afterward. Hidden staging is unselectable.
- Preserve `launch_id`: private UUID4 workload-generation token for every placement; existing headers, bootstrap, writes, and cleanup remain fenced by it.
- All relative paths follow [Layout](../design/layout.md); remove execution-instance layers, use status `default`. Keep generation bootstrap directories and status/reference v3; persist host identity/order/startup bindings through guest merges.
- Retain published roots/home/history after every exit or failure. Preserve project inputs and shared `<source_dir>/.toolang/.state/root/edit.lock`.

## Runtime and discovery

- Resident/visiting reuse executors only when all effective startup parameters match, including inferred environment/configuration/defaults. Compare before preparation; redact secrets; reject mismatches without mutation.
- Run/request parameters may differ. Attachment preserves lifetime; retain explicit `start` promotion. Controls attach using saved bindings; serialize starts and offline maintenance through existing management ownership.
- Roaming always gets a fresh root/executor; concurrent host/guest launches may use different parameters. One host workload/event loop/executor; shared AgentCore/app, existing guest hosting; caller owns shutdown.
- Preserve explicit port precedence/Hub exclusion. Reserve listener sockets or publish actual sandbox endpoints; at most three automatic retries, cleaning resources and renewing `launch_id`. Explicit conflicts fail; attachment uses the active endpoint.
- Discovery reads only `<toolang_root>/.runtime/instances/<instance_name>.json`: version, local status path, `launch_id`. Keep preparing/active/stopping references; remove matching generations after resource release, including targeted cleanup after guest exit.
- Global references are disposable; absence proves no process death. History never depends on them. Bounded unknown diagnostics remain; lost live references need host republication. No background cleanup/root deletion.
- Top ownership uses full instance names, never source hashes. Preserve windows/cursors/Stats independence; fresh Top scans no stopped roots. Scoped resident Top and installed-agent listing keep their scope.

## Commands and history

- Targets remain `gus`, URLs, local `.too` paths. `--instance` / `-i` accepts complete `v…`/`r…` IDs; resident needs none. Invalid/unknown selections fail; no decorated-source syntax.
- `info`/`inspect` calculate roots locally without global references, refetch, preparation, or runtime creation; retained inspection requires no readable source contents.
- Default roaming selection: maximum validated local ID for that canonical source, including failed/exited instances. Latest means startup request; completion/mtime never reorder. Corrupt/ambiguous metadata fails.
- Pin root/identity/generation once per command. `steer`/`cancel` require that live owner; never launch a replacement.
- `fork`/`rewind`: existing live API; offline `ThreadManager` transactions only after guarded native/sandbox death checks. Preserve idle/head/anchor rules; unknown/starting/stopping owners reject writes.
- New roaming script/Chat always creates a fresh instance; `-i` cannot revive retained roots. Roaming has no `serve`/`start`/`stop`; the caller owns shutdown.

```sh
too /work/demo/aid.too inspect -i r18e0b5f817d00000e2c7a0f23d51a9c6 runs
```

## Replay

- Roaming `rerun` selects source via `-i`, executes in a fresh destination; resident/visiting retain stable-home semantics.
- Use full SQLite backup; resolve terminal Run/control head in that snapshot. Preserve references/views/allocators/immutable State and existing rerun semantics; prepare current inputs and revalidate grants/workdir.
- Copy no mutable workspaces/tools, bootstrap credentials, lifecycle status, or `stats.db`. Imported receipts grant no live leases/subscriptions.
- Persist imported Run/child ownership and atomic replay acceptance/provenance across repeated replay. Imports stay inspectable; exclude them from steer/cancel, recovery/drivers/new events, and destination activity/completion/usage/archive counts.
- RunStore 63: additive atomic migration from 62; read-only support for both; check guest compatibility before opening stores. Execute only complete seeds; failure retains source and destination roots.
- `fork --chat` / `rewind --chat`: commit edit, snapshot into fresh roaming Chat, report instance ID; Chat failure never undoes the edit.

## Delivery and acceptance

- API: rename internal/OpenAPI `top` group to `activity`; keep `/api/v0/top/snapshot` and all HTTP contracts/other groups, including existing `agent`.
- Touchpoints: [layout](../../src/toolang/common/layout.py), [source](../../src/toolang/up/source.py), [acquisition](../../src/toolang/cli/common/agent_server.py), [hosting](../../src/toolang/up/server.py), [discovery](../../src/toolang/up/observation.py), [routing](../../src/toolang/cli/toolang/routing.py), [threads](../../src/toolang/execution/threads.py), [store](../../src/toolang/execution/store.py), Stats readers.
- Acceptance: exact layouts/path hashing; sortable IDs/clock rollback; parallel host/guest Top rows; strict startup reuse; generation/selection races; ports, output/signals/drivers, active/completed transitions.
- Acceptance: registry-independent inspection; retained failures; guarded offline edits; snapshot reference closure; repeated imports count once; failed seeds never execute; migration/API compatibility; shared-edit serialization.
- Checks: deterministic offline tests/fake sandboxes; opt-in Docker validation. Implementation runs default repository verification; docs-only runs source/link/example checks and `git diff --check`.
- Upgrade/rollback: stop old workloads; reject unverifiable owners; retain old roots for explicit migration. Old writers cannot open schema 63.
- Risks: stricter defaults, preparation/snapshot disk cost, retained roots, OS visiting-root cleanup, existing guest trust. No general history merge or cleanup in scope.
- Update current designs/changelog with implementation. Open questions: none pending.
