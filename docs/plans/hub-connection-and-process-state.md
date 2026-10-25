# Hub Client and Server Separation

Approved in [#959](https://github.com/openhat-ai/toolang/pull/959). Clients must use configured Hub HTTP
connections without backend access or Hub files; successful local stop must
remove the stopped process's record. Supersedes discovery/scanning decisions in
[agent Hub transport](agent-hub-transport.md) and [teaming](teaming.md).
Authentication, deployment-specific adaptations/tests, data migration, and
permanent-member removal APIs are outside scope.

## Configuration

Both sections are optional in `ROOT/config.toml`; defaults are shown below except
`human`, which defaults to the OS username.

```toml
[teaming]
human = "bryan"
enabled = true
hub = "http://127.0.0.1:7001"

[hub]
host = "127.0.0.1"
port = 7001

[hub.backend]
url = "redis://localhost:6379/0"
```

- Clients validate only `[teaming]`; servers validate only `[hub]`. Neither
  consumer resolves the other's settings. Root disablement gates client activity;
  HOME `teaming.enabled=false` can additionally opt out. Human/URL stay root-owned.
- Clients use `teaming.hub` or its default; reject invalid URLs without fallback.
  No new URL/environment overrides are introduced.
- Servers use `hub.host`; port precedence remains `--port` > `TOOLANG_HUB_PORT`
  > `hub.port` > default. Keep busy-port failure. Changing the bind port requires
  updating the client URL; listener settings never supply client defaults.

## Behavior

1. **Identity:** `/info` returns `{version, api_version, identity}` without human
   or backend details and without registering participants. Validate it before
   application mutations. Preserve the existing opaque dataset identity and
   pin it through `X-Toolang-Backend` on requests/SSE and admitted Talk batches.
   Configuration refresh applies to new connections; old batches/cursors cannot
   cross datasets. Preserve recovery, uncertain-send handling, and local execution
   during outages; never auto-start Hub, use Redis directly, or replay messages.
2. **Actors:** Human requests declare their actor through `X-Toolang-Human`;
   missing/invalid actors fail before storage access. Agent identity comes from
   route/lease. Registration accepts `{owner, endpoint}`, with owner from client
   `teaming.human`; remove `managed`. Keep owner/name conflicts and lease fencing.
   Hub startup uses no configured human; human requests register their own actor.
3. **Membership:** Remove all Hub HOME scanning and scan-based registration or
   removal. First registration makes an agent known; lease expiry/release marks
   it offline while retaining memberships, history, cursors, and message delivery.
   HOME deletion and local removal never imply Hub deletion. Local CLI listings
   may still discover unstarted agents without publishing them to Hub.
4. **Call sites:** Pass selected ROOT client settings through existing setup and
   launch paths, preserving human identity and HOME opt-out. Remove obsolete
   Hub-file mounts and server/backend fields from client inputs. Reuse existing
   networking behavior; all targets use the same HTTP client.
5. **Process control:** Use `hub/pid.json` as local `pid/created/host/port/status`
   state, without human/identity. Verify PID creation time. Clean owned records
   during ASGI teardown and after confirmed exit, including force/startup failure.
   Use the lifetime lock and compare process identity to protect replacements.
   Preserve existing stop timeouts and report cleanup failure. Keep atomic private
   writes and log/lock locations. Use only the new record format.
6. **Inspection:** `too hub` shows configured-target availability/version separately
   from local process state and retains local agent listings. Corrupt local state
   cannot block remote inspection. Start/stop always control the local process.

## Changes and acceptance

| Area | Touchpoints | Required checks |
| --- | --- | --- |
| Configuration | `teaming/config.py`, `setup/teaming.py`, `up/config.py` | Defaults, section isolation, enablement, precedence. |
| Transport and membership | `teaming/` clients, API, lifecycle, roster/backend; existing setup/launch and tool callers | Mock HTTP verifies the configured URL and no HOME/backend dependency; registration, offline delivery, owner/lease conflicts, actors, dataset isolation and reconnect. |
| Process and CLI | `up/{hub,records}.py`, CLI Hub commands | Real SIGTERM regression, forced stop, failed/timed-out startup, stop timeout, replacement race; mocked target availability independent of local process state. |

Use configuration units, mock HTTP, and the process regression; keep tests offline.
Implementation updates focused design docs and generates the changelog through
`too aid.too update_changelog`, then runs repository default checks.

## Limits

Use the new configuration, API, and process-record formats directly; do not add
backward compatibility or migration handling.

Offline and previously discovered members remain until a future explicit removal
operation. All clients share the endpoint's agent-name namespace; changing the
Hub's canonical root selects a different dataset.
No open design questions remain.
