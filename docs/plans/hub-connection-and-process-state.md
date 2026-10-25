# Hub Client and Server Separation

Proposed; implementation requires approval. Clients must use configured Hub HTTP
connections without backend access or Hub files; successful local stop must
remove the stopped process's record. Supersedes discovery/scanning decisions in
[agent Hub transport](agent-hub-transport.md) and [teaming](teaming.md).
Authentication, deployment automation, data migration, and permanent-member
removal APIs are outside scope. No implementation is included.

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
- URL precedence: `TOOLANG_HUB_URL` > `teaming.hub` > default. Accept HTTP/HTTPS
  with path prefixes; reject invalid selected values without fallback.
- Listener precedence: CLI `--host/--port` > `TOOLANG_HUB_HOST/PORT` > `[hub]` >
  defaults. Keep busy-port failure. Changing the bind port requires updating the
  client URL; listener settings never supply client defaults.
- A Docker Hub configures its own `[hub]` and backend. If published at host port
  `17001`, host clients set `teaming.hub = "http://127.0.0.1:17001"` only.

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
4. **Docker agents:** Project selected ROOT client settings, including resolved
   human and effective URL, over PLACE settings; preserve HOME opt-out. Strip
   server/backend settings and remove the Hub directory mount. Apply existing
   gateway translation only to loopback URLs, preserving port/path and explicit
   remote hosts. Transport receives concrete settings from setup/launch boundaries.
5. **Process control:** Keep `hub.json` as local `pid/created/host/port/status`
   state, without human/identity. Verify PID creation time. Clean owned records
   during ASGI teardown and after confirmed exit, including force/startup failure.
   Use the lifetime lock and compare process identity to protect replacements;
   allow five seconds for cleanup contention, then report failure. Stop timeout
   without force fails and retains state. Keep atomic private writes and log/lock
   locations; reconcile verified dead legacy records without trusting their human.
6. **Inspection:** `too hub` shows configured-target availability/version separately
   from local process state and retains local agent listings. Corrupt local state
   cannot block remote inspection. Start/stop always control the local process.

## Changes and acceptance

| Area | Touchpoints | Required checks |
| --- | --- | --- |
| Configuration | `teaming/config.py`, `setup/teaming.py`, `up/config.py` | Defaults, section isolation, enablement, precedence, legacy-key diagnostics. |
| Transport and membership | `teaming/` clients, API, lifecycle, roster/backend; Talk and conversation-tool callers | No HOME/backend access from clients; first registration, offline delivery, owner/lease conflicts, human actors, dataset isolation and reconnect. |
| Process and CLI | `up/{hub,records}.py`, CLI Hub commands | Real SIGTERM regression, forced stop, failed/timed-out startup, stop timeout, replacement race, independent local/remote status. |
| Docker | `up/{mounts,sandbox,server}.py`, Docker adapter | ROOT differs from PLACE and guest username; client-only projection, gateway handling, Docker Hub without shared HOME or Hub files. |

Keep default tests offline and synchronized; container/wire tests remain opt-in.
Implementation updates focused design docs and generates the changelog through
`too aid.too update_changelog`, then runs repository default checks. This plan
change needs source/example/link verification and `git diff --check` only.

## Upgrade and limits

Move `teaming.backend` to `hub.backend`, old `teaming.hub.port` to `hub.port`,
and make `teaming.hub` a URL string; reject legacy shapes with migration diagnostics.
Upgrade Hub/clients together. Preserve backend schema, identity, history, and
namespace guards; legacy roster rows become inert. Local control may decode old
process records only for verified stop/cleanup.

Offline and previously discovered members remain until a future explicit removal
operation. All clients share the endpoint's agent-name namespace. Keep the Hub's
canonical root stable across container restarts to preserve its dataset.
No open design questions remain.
