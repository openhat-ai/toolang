# Plan

Consolidate compatibility management into eight package-owned contracts: two file
formats, three database schemas, and three API versions. Definition only; awaiting
human review before implementation.

## Scope

- In: ownership, naming, format removal/merging, current-version validation and
  coordinated consumer updates.
- Out: historical compatibility, migration, service separation, new statistics
  behavior, public CLI changes, CST JSON and build-info internal conventions.

### Contract names and boundaries

Each row owns one package-local constant. Nested types retain validation without
independent counters; protocol types remain independent of stores/runtime services.

| Contract | Constant | Boundary | Existing lineage -> consolidated version |
| --- | --- | --- | --- |
| agent state format | `AGENT_STATE_FORMAT_VERSION` | Root/home `layer.json` and agent `layers.json`, including source inventories and captured AST/cap/chore values. | Layer 22 + composition 3 -> 23 |
| agent instance format | `AGENT_INSTANCE_FORMAT_VERSION` | Agent `status.json` and host-owned instance discovery references. | Status 2 + reference 1 -> 3 |
| run store schema | `RUN_STORE_SCHEMA_VERSION` | All `runs.db` data, including chore checkpoints, model calls and compaction payloads. | 59 -> 60 |
| stats store schema | `STATS_STORE_SCHEMA_VERSION` | `stats.db`: buckets, archive receipts and initialization state. | 1 -> 1; no DDL change |
| hub store schema | `HUB_STORE_SCHEMA_VERSION` | Valkey team/conversation/message data and stored execution publication/recovery data. | Conversation 3 + event storage 1 -> 4 |
| agent API version | `AGENT_API_VERSION` | Execution, capabilities, workspaces, lifecycle, activity/Top, pagination and direct execution streams. | URL version 1 -> 2 |
| stats API version | `STATS_API_VERSION` | Read-only `summary`, `models`, `daily` and `recent` requests and responses. | Newly independent -> 1 |
| hub API version | `HUB_API_VERSION` | Messaging, team/presence, subscriptions, publication, staging and recovery requests and responses. | Newly explicit -> 1 |

If an affected lineage advances before implementation, advance from its newer
value. Never reset existing lineages or couple counters to package releases.

Use `format` for files, `schema` for databases, and `Record`/`records.py` for
database persistence types/rows. Move file codecs/types to their format/discovery
owner. Content revisions, digests, epochs, generations and grammar/source/package
versions retain their meanings without becoming additional compatibility domains.

### Compatibility and API boundaries

Accept only current contracts: no legacy decoders, field aliases, migrations,
dual readers/writers or fallbacks. Reject incompatible stores/instance files
without mutation; rebuild derived State from authored inputs. Initialize empty
stores only; incompatible datasets require explicit replacement. Stop previous
workloads before replacing instance files; preserve ownership and fencing.

Keep the existing `/api/vN` route convention: mount agent routes under
`/api/v{AGENT_API_VERSION}`, stats routes independently under
`/api/v{STATS_API_VERSION}/stats`, and Hub application routes under
`/api/v{HUB_API_VERSION}` on the Hub endpoint. Keep readiness `/healthz`
operational and unversioned. Update bundled clients together; serve no previous
route aliases. Stats remains agent-hosted. Its version covers query validation,
scopes, time ranges/timezones, quantities/units, null/zero semantics, snapshot
state and errors. Preserve existing
[statistics semantics](../execution-statistics.md) and
[`recent` contract](../top.md). Statistics shared in agent Top responses belong
to the agent API contract; Hub conversation/message counts belong to the hub API.

## Action items

- [ ] Unify State file markers as `format` under the agent state version; retain
  root/home sharing, composition files, content addressing and integrity checks.
  Remove `SOURCE_SCHEMA`, the manifest's version field and unused standalone
  manifest serialization; keep source identity as an internal value type.
- [ ] Unify status/reference markers as `format` under the agent instance version
  without merging their files or control/workload ownership. Rename file-backed
  `up/records.py` terminology and `teaming.records.HubRecord`; keep Hub process
  discovery an unversioned internal convention.
- [ ] Fold model-call and compaction policy versions into the run store schema;
  remove the old compaction branch, legacy retry decoding and retired field
  aliases. Keep chores in `runs.db`. Fold conversation and event-storage version
  checks into one hub dataset gate; retain the Valkey key namespace as an address,
  not another version counter. Retain independent stats persistence.
- [ ] Route and validate the three APIs independently. Absorb `lifecycle_protocol`
  and history/thread/tool cursor versions into the agent API; absorb team-event,
  publication/staging and wire projection versions into the hub API. Keep cursor
  kinds, scope/store bindings and epoch validation, replacing `h1`/`t1` with
  versionless `h`/`t` kind tags. Storage encodings remain store-owned.
- [ ] Delete the unused common cache envelope/version and Docker `start.json`
  writer, preserving used digest helpers. CST JSON/build info remain internal
  conventions outside this inventory; add no CST JSON readback path.
- [ ] Cover current-format round trips, State integrity, host/guest fencing,
  exact model-call replay and compaction recovery, shared chore persistence, Hub
  publication/recovery, and all four stats endpoints. Non-current versions,
  nonempty unversioned stores and malformed payloads must fail without mutation;
  cache preparation may rebuild. Verify wrong-kind/store cursors fail and a stats
  API version change leaves agent/hub routes unchanged.
- [ ] Update owning documentation, bundled clients and existing offline tests;
  generate implementation changelog entries through `too aid.too update_changelog`
  for maintainer verification. Run the repository's default verification before
  implementation commits; for this definition, validate links and run
  `git diff --check`.

Likely touchpoints: `state/{source,state,cache,prepare}.py`,
`up/{records,observation,process,server,hub}.py`,
`execution/{store,records,legacy,compaction,schemas,remote}.py`,
`execution/inspection/`, `driver/chore/store.py`, `stats/{records,_sql,schemas}.py`,
`api/{router,schemas,routers/stats}.py`, `teaming/` codecs/clients/Valkey backend,
CLI clients, `common/cache.py`, Docker staging, and their existing tests and
focused documentation.

## Open questions

None. Risks are unusable old history/cursors and accidental coupling of storage,
transport or host/guest authority; the gates and acceptance checks cover them.
