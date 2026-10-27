# Hub API changes for multiple root partitions

Status: proposed API contract for [client-root namespaces](client-root-hub-namespaces.md).

## Shared rules

The current 48 method/path combinations remain unchanged. Today every data route
uses the Hub startup root, `S`; afterward it uses the client-selected host root,
`R`. Business bodies, query parameters, responses, and actor permissions stay the
same, except for the metadata changes below.

- Data routes require `X-Toolang-Root`: the percent-encoded canonical host root.
  Reuse its existing R8 key prefix and full-path ownership checks. Missing or
  invalid roots return 400 before storage access; never fall back to `S`.
- Scoped `/info` returns `root` and that partition's `identity`. Clients verify
  the root echo and carry the identity in the existing `X-Toolang-Backend` header.
  Existing identity checks compare against `R`, returning 409 `hub_changed` on
  mismatch. Unscoped service probes do not validate a partition identity.
- Human routes retain `X-Toolang-Human`; agent routes retain
  `X-Toolang-Agent-Lease`. Owners, leases, membership, and permissions are resolved
  within `R`; identical participant/conversation IDs can exist in different roots.
- Bind backend selection once per request/SSE subscription. Resume context and
  caches include root/identity; switching roots resets that context and never
  replays uncertain sends. Keep existing cursor formats and recovery behavior.
- Startup and service probes create no business keys. A valid scoped business
  request initializes `R` as needed through the existing atomic initialization.
  Metadata alone neither initializes a partition nor registers a human.
- Keep API/event version 0 with explicit root-aware metadata validation; upgrade
  Hub and clients together. Old servers without the root echo and old unscoped
  business clients fail closed. Existing datasets stay under their original root.

## Service and execution-event APIs: 3 operations

| Method | Path | Before | After |
| --- | --- | --- | --- |
| GET | `/info` | Version, API version, and identity of `S`. | Without root: version/API version only. With root: version/API version plus `root=R` and `identity(R)`. Neither form creates keys. |
| GET | `/healthz` | Checks maintenance, backend connection, and initialization of `S`. | Service-only readiness: shared maintenance and backend connection. Preserve `200 {"ok":true}` / 503; no partition validation, initialization, or all-partition scan. |
| GET | `/events/stream` | Execution events for `S`, filtered by agent/thread/run. | Require `R`; stream and filters apply only within `R`. Keep that selection for the entire SSE connection. |

`/healthz` remains global even when a caller supplies partition headers. A bad
partition produces errors on its data routes without marking other partitions
unhealthy. `ps` combines global readiness with scoped `/info` identity checks;
`hub status` uses unscoped version metadata. No partition-health API is added.

## Team APIs: 4 operations

`T` expands to both the empty prefix and `/agents/{agent}`. The first uses a
human actor; the second requires that agent's active lease in `R`.

| Method | Path | Before | After |
| --- | --- | --- | --- |
| GET | `T/team` | Member directory and presence in `S`. | Member directory and presence in `R`. |
| GET | `T/team/events` | Team/membership/presence SSE for `S`. | The same feed restricted to `R`; lease and resume checks use `R`. |

## Messaging APIs: 32 operations

`M` expands to both `/msg` and `/agents/{agent}/msg`. Existing human visibility
and agent membership/active-lease restrictions remain unchanged within `R`.

| Method | Path | Before | After |
| --- | --- | --- | --- |
| GET | `M/targets` | Available targets in `S`. | Available targets in `R`. |
| GET | `M/agents` | Agent/owner directory in `S`. | Agent/owner directory in `R`. |
| GET | `M/conversations` | Conversation directory/previews in `S`. | Directory/previews in `R`; preserve `include_preview`. |
| POST | `M/conversations` | Creates a conversation in `S`. | Creates it in `R`; allocation and membership remain local to `R`. |
| GET | `M/stats` | Dataset statistics for `S`. | Dataset statistics for `R`; no cross-root aggregation. |
| GET | `M/conversations/{conversation}/stats` | Conversation statistics in `S`. | Conversation statistics in `R`. |
| GET | `M/conversations/{conversation}` | Conversation lookup in `S`. | Lookup by `(R, conversation)`. |
| PATCH | `M/conversations/{conversation}` | Rename/revision check in `S`. | Rename/revision check in `R`. |
| POST | `M/resolve` | Resolves IDs, names, pairs, and `all` in `S`. | Resolves them in `R`; `all` is that root's system group. |
| GET | `M/conversations/{conversation}/rules` | Rules/revision from `S`. | Rules/revision from `R`. |
| PUT | `M/conversations/{conversation}/rules` | Updates rules with revision fencing in `S`. | Updates/fences them in `R`; preserve conflict behavior. |
| PUT | `M/conversations/{conversation}/participants` | Joins the current actor in `S`. | Joins the current actor in `R`. |
| DELETE | `M/conversations/{conversation}/participants` | Leaves the conversation in `S`. | Leaves it in `R`. |
| GET | `M/conversations/{conversation}/messages` | History/read range in `S`. | History/range in `R`; preserve `after` and `count`. |
| GET | `M/conversations/{conversation}/cursor` | Checks a message cursor in `S`. | Checks it against the conversation in `R`. |
| POST | `M/messages` | Sends a message using `S`'s membership and receipts. | Uses `R` throughout authorization, append, receipt, and agent rules/run/thread handling. |

## Agent lifecycle and publication APIs: 9 operations

`A` expands to `/agents/{agent}`. Registration and release retain their existing
lease checks; other operations require that agent's active lease in `R`.

| Method | Path | Before | After |
| --- | --- | --- | --- |
| PUT | `A/lease` | Registers agent/owner in `S`; same-name owners collide. | Registers in `R`; other roots' agents do not participate in owner/lease checks. |
| PATCH | `A/lease` | Renews the lease in `S`. | Renews it in `R` only. |
| DELETE | `A/lease` | Releases the matching lease in `S`. | Releases it in `R` only. |
| GET | `A/talk/cursors/{convo_id}` | Reads the agent/conversation cursor in `S`. | Reads it by `(R, agent, conversation)`. |
| PUT | `A/talk/cursors/{convo_id}` | Advances/fences that cursor in `S`. | Advances/fences it within `R`. |
| GET | `A/events/state` | Publication metadata/origin for the agent in `S`. | Metadata/origin for that agent in `R`. |
| PUT | `A/events/staging/{generation}` | Stages publication data in `S`. | Stages it in `R`; equal agent/generation IDs elsewhere stay independent. |
| DELETE | `A/events/staging/{generation}` | Abandons staging data in `S`. | Abandons only the matching data in `R`. |
| POST | `A/events/commits` | Commits events/projections and renews the lease in `S`. | Commit, projection updates, and renewal all use `R`. |

## Acceptance

Verify the inventory covers all 48 operations and every data operation retains
its selected backend throughout execution.
Two roots with identical IDs must retain independent leases, conversations,
messages, rules, statistics, Talk cursors, staging, and both SSE feeds. Global
probes and scoped metadata must leave an empty backend unchanged; a fresh scoped
business request must initialize only its selected namespace. Missing roots,
identity mismatches, and old peers must not fall back to another partition.

`hub start/status/stop` remain local process commands, not HTTP APIs. Namespace
selection supplies data isolation; it adds no authentication or process-control
authority.

Sources: [service routes](../../src/toolang/teaming/api.py),
[team routes](../../src/toolang/teaming/team_api.py),
[messaging routes](../../src/toolang/teaming/messaging_api.py), and
[agent routes](../../src/toolang/teaming/agent_api.py).
