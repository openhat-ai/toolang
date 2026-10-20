# ID Rules

Generated suffixes use eight lowercase Base32 characters:
`0123456789abcdefghjkmnpqrstvwxyz`. External/provider IDs remain opaque.

| Identity | Rule |
| --- | --- |
| `chat_`, `work_`, `exec_` | Agent-keyed `local` allocation: hourly tick(20) + sequence(20). |
| `run_` | Agent-keyed `run` allocation with the same bit budget. |
| `talk_` | Permute the conversation suffix with an agent-specific key. |
| `chore_` | Hash the exact name to 40 bits, then permute with an agent-specific key. |
| `dm_` | Conversation codec v3: permute `0 + pair_hash(39)`. |
| `gc_` | Conversation codec v3: permute `1 + hourly_tick(20) + sequence(19)`. |

## Chores

- Every filename stem/declaration name follows the same calculation, regardless
  of its format or creator. Names are case-sensitive and not normalized.
- Automatic filenames are `chore-<local ID>.md`. The filename suffix is allocated
  using epoch/sequence; the chore ID is derived from the complete resulting name
  and agent. Neither `chore-` nor `auto_` has special identity semantics.
- Title/body/schedule/location edits preserve identity. Name changes create a
  new identity. Reject different-name collisions before writes or State acceptance.
  State uses one pure name-to-ID function; it tracks no creation provenance.

## Allocation and conversations

- Epoch: `2026-01-01T00:00:00Z`; ticks are hours. Local/run permit 1,048,576
  IDs/hour per agent; GC permits 524,288 reservations/hour per backend.
- Each agent's `runs.db` keeps separate `local`/`run` sequence rows.
  `BEGIN IMMEDIATE` serializes reservations; commit before returning the ID.
  Keep ticks monotonic through clock rollback. Exhaustion fails without wrapping
  or resetting. Participating runtime processes share the database.
- DM uses the sorted exact typed participant pair; a stored mismatch fails as a
  collision. DM/GC suffix domains are disjoint. GC reservations use atomic backend
  time and sequence state. Agent-keyed IDs reduce cross-agent collisions, without
  a global uniqueness guarantee; references must still identify the agent.
- Talk uses `talk_thread_id` and persists one binding per backend/conversation
  in the agent's `runs.db`. Reuse it across batches/restarts; reject an occupied
  ID with another or no binding. The codec itself creates no thread.
  Hub/clients use hub store schema 4. Add no compatibility codec or migration.

Shared primitives live in `common/id_codec.py`; allocation in `common/ids.py`;
chore rules in `catalog/chore_ids.py`; conversation rules in `teaming/ids.py`.
See [Messaging](messaging.md#storage-events-and-statistics) for backend identity
and storage boundaries.
