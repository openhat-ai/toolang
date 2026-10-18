# Contract Version Boundaries

Maintain eight independent contracts: two file formats, three database schemas
and three API versions. Approved for implementation on 2026-10-18.

## Contracts

| Name | Constant | Boundary |
| --- | --- | --- |
| agent state format | `AGENT_STATE_FORMAT` | Root/home layers and agent composition, including source inventories and captured AST/cap/chore values. |
| agent instance format | `AGENT_INSTANCE_FORMAT` | Agent status files and instance discovery references. |
| run store schema | `RUN_STORE_SCHEMA` | All `runs.db` data, including chore checkpoints, model calls and compaction payloads. |
| stats store schema | `STATS_STORE_SCHEMA` | `stats.db` buckets, archive receipts and initialization state. |
| hub store schema | `HUB_STORE_SCHEMA` | Valkey team/conversation/message data and stored publication/recovery data. |
| agent API version | `AGENT_API_VERSION` | Execution, capabilities, workspaces, lifecycle, activity/Top, pagination and direct execution streams. |
| stats API version | `STATS_API_VERSION` | Read-only `summary`, `models`, `daily` and `recent` queries, quantities, snapshots and errors. |
| hub API version | `HUB_API_VERSION` | Messaging, team/presence, subscriptions, publication, staging and recovery. |

Keep each counter with its owning package. Reserve `Record`/`records.py` for
database persistence types or rows; use format/discovery terminology for files.
CST JSON and build info are internal conventions outside this inventory. Content
revisions, digests, epochs and software versions retain their existing meanings.

## Removal and merging

- Merge layer/composition versions into agent state format. Remove
  `SOURCE_SCHEMA`, the manifest's version field and unused standalone manifest
  serialization; retain source identity types, file layout and content addressing.
- Merge status/reference versions into agent instance format; preserve separate
  files and control/workload ownership.
- Absorb durable model-call and compaction policy versions into run store schema;
  absorb conversation/event-storage versions into hub store schema. Chores share
  run store schema; stats store remains independent.
- Absorb lifecycle and pagination/cursor versions into agent API, and team-event,
  publication/staging and wire projection versions into hub API. Retain distinct
  payload types, cursor kinds, bindings and validation. Stats API is independently
  versioned even when hosted by the agent; preserve existing
  [statistics semantics](../execution-statistics.md) and
  [`recent` behavior](../top.md).
- Delete the unused common cache envelope/version and Docker `start.json` writer;
  retain used digest helpers. Update affected producers, consumers and tests together.

## Version and compatibility policy

Continue existing format/schema lineages without resetting them for release.
Increment affected formats/schemas for incompatible changes; new domains start at 1.
Merged domains continue from the highest existing counter. Do not preassign final
version values before implementation.

The three APIs use v0 during prerelease and retain independent version owners.

Support no historical formats: remove legacy decoders, retired-field aliases,
migrations and compatibility fallbacks. Reject incompatible stored data without
rewriting it; derived State may rebuild from authored inputs. Existing history
and cursors may become unusable. Preserve data integrity and process ownership.
API route layout, cursor encoding, deployment and unrelated behavior redesigns
are outside this consolidation.

## Touchpoints and acceptance

Likely files: `state/{source,state,cache,prepare}.py`, `up/{records,observation}.py`,
`execution/{store,records,legacy,compaction,schemas}.py`, `execution/inspection/`,
`driver/chore/store.py`, `stats/{records,schemas}.py`, `api/{router,schemas}.py`,
`api/routers/stats.py`, `teaming/` persistence/protocol code, CLI clients,
`common/cache.py`, Docker staging, and their existing tests/documentation.

- Exactly eight version owners use the names above; nested structures introduce
  no independent counters, and stats API can advance independently of agent API.
- Current data round-trips preserve State integrity, model-call replay,
  compaction recovery, chore persistence, Hub publication and all four stats queries.
- Non-current versions and retired payloads are rejected without data mutation;
  State preparation can regenerate derived data. Existing ownership checks pass.
- Implementation passes the repository's default verification. This documentation
  change requires source/link validation and `git diff --check` only.

Open questions: none within this scope.
