# ID Model

This document defines the Toolang-owned id families and allocator model.

This design is implemented in `toolang.common.ids` and is used for allocated thread/run IDs and name-derived chore IDs.


## Goals

Toolang-owned ids should be:

- generated automatically
- short enough to copy and quote by hand
- stable across authored-file rename, move, archive, and restore operations
- monotonic at allocation time without exposing raw time buckets directly
- reversible for local tooling and archive bucketing
- safe to allocate from both CLI and runtime processes


## Scope

The local allocator described below applies to Toolang-owned local ids.
Conversation IDs use the separate backend allocation contract at the end of this
document.

It does not rewrite the opaque payload of external ids such as:

- transport-native ids exposed under Toolang prefixes such as
  `tg_<external_id>`
- provider-native ids such as OpenAI response ids


## Families

The local allocator defines two families:

| Family | Width | Raw layout | Intended use |
| --- | --- | --- | --- |
| `local` | 8 chars | `tick(20) + seq(20)` | generated chore filenames and allocated local thread IDs |
| `run` | 8 chars | `tick(20) + seq(20)` | run ids |

Shared rules:

- the raw id model still consists of one time bucket plus one monotonic
  per-bucket sequence
- the visible leading and trailing chars are produced by one whole-id
  reversible permutation, so ids from the same tick do not need to share the
  same visible prefix
- all characters use one lowercase Crockford-style base32 alphabet:
  `0123456789abcdefghjkmnpqrstvwxyz`
- one fixed epoch is used for all families: `2026-01-01T00:00:00Z`
- the current prototype uses one-hour tick buckets
- all epoch and bucket timestamps in this design are expressed in `UTC`; avoid
  ambiguous abbreviations such as `CST`

For the `local` family:

- 4 base32 tick chars = 20 tick bits
- 4 base32 seq chars = 20 seq bits
- capacity is 1,048,576 ids per hour per allocator state

For the `run` family:

- 4 base32 tick chars = 20 tick bits
- 4 base32 seq chars = 20 seq bits
- capacity is 1,048,576 ids per hour per allocator state


## Full Forms

The current full identities are:

- chat context: `chat_<allocated-id>`
- incoming-message context: `talk_<allocated-id>` (one per durable agent)
- chore context: `chore_<name-derived-id>`
- independent spawned context: `work_<allocated-id>`
- standalone script context: `script_<allocated-id>`
- run: `run_<allocated-id>`

Chore IDs do not use the monotonic allocator: hash the exact UTF-8 name with
SHA-256, take its first 80 bits, and encode them in 16 lowercase Base32
characters. Name changes create new IDs; body, title, schedule, and location
changes preserve them. Names are case-sensitive and are not normalized.
Different-name hash collisions are rejected. The canonical ref is `chore/name`.


## Reversible Obfuscation

Toolang does not expose the raw `tick + seq` values directly. Instead it uses
one two-stage reversible encoding per family.

For one family with tick modulus `2^n` and seq modulus `2^m`:

```text
tick_code = (A * tick + B) mod 2^n
seq_code = (C * (seq xor mask(tick)) + D) mod 2^m
raw_code = (tick_code << m) | seq_code
wire_code = feistel(raw_code)
```

Where:

- `A` and `C` are odd so the affine maps are invertible modulo `2^n`
- `mask(tick)` is one deterministic tick-derived bit mask trimmed to the seq
  width
- `feistel(...)` is a four-round reversible whole-id permutation using keyed
  BLAKE2s round functions; its key is BLAKE2s of the compact UTF-8 JSON array
  `[family.name, agent_name]`
- encoded ids are `encode_base32_fixed(wire_code)`

This gives Toolang-owned ids these properties:

- allocation order is hidden
- ids still decode back to raw `tick` and `seq`
- ids from the same tick bucket can still display different visible prefixes
- archive bucketing can still use the decoded tick-derived UTC bucket

This mechanism is not meant to be cryptographic secrecy. It is only a compact,
reversible local obfuscation layer.

Encoding and decoding require the same canonical, case-sensitive agent name.
Callers pass `layout.name`; paths, sandbox names, and process IDs do not affect
the codec. An empty or whitespace-padded name is rejected. Different agent
names diversify equal tick/sequence pairs, but their permutations share the
same 40-bit space: residual cross-agent collisions remain possible. Team
references must still include the agent identity. Within one agent/family,
distinct tick/sequence pairs remain distinct IDs.

This codec does not decode historical unkeyed IDs. Existing records keep their
IDs. Allocation state now lives in the `seqs` table in `runs.db`; legacy JSON
counters are not imported. Code integrations must supply `agent_name` to ID
helpers and `IdIssuer`; manual decoding requires the original agent name and
codec.


## Allocation

Allocator state is durable and per-agent. It is not runtime-only state.

The `seqs` table in each agent's `runs.db` stores one monotonic
`(last_tick, last_seq)` pair per family. Rows are created on first successful
allocation; a missing family starts from `(-1, -1)`.

| family | last_tick | last_seq |
| --- | --- | --- |
| local | 42 | 3 |
| run | 42 | 14 |

Allocation opens a dedicated SQLite connection to an initialized database:

1. acquire the write transaction with `BEGIN IMMEDIATE`;
2. load and validate the current family state;
3. keep the tick monotonic with `max(now_tick, last_tick)`;
4. increment the per-tick sequence and encode the ID;
5. upsert the updated state and commit before returning the ID.

Allocation uses `synchronous=FULL` and a 30-second SQLite busy timeout. Failures
roll back the reservation and propagate without an allocation fallback.
Reservations precede business write transactions and remain committed when
subsequent run, thread, or authored-file creation fails. Deleting execution
history does not reset counters. Invalid stored states are rejected rather than
treated as empty state.

Host and sandbox writers must share the same database, including its WAL and
shared-memory files, on storage supporting SQLite's locking requirements.
Independent database copies do not coordinate allocations.

This storage change supports fresh data only. Writable execution opens reject
old schemas and adjacent legacy `ids.json` files; no counter import or history
scan occurs. Stop all writers, preserve the complete old runtime, and explicitly
start with fresh runtime data. Supported old stores remain available for
read-only inspection. Resetting an allocator does not guarantee disjoint IDs
from earlier allocations with the same agent name.

State-loss recovery remains manual. Decoding retained IDs and taking the maximum
`(tick, seq)` per family can help reconstruct a high-water mark after stopping
writers. Timestamp or insertion order does not necessarily match allocation
order. Historical records cannot recover deleted or unpersisted reservations;
there is no automatic counter reconstruction. Like the other execution tables,
`seqs` is recreated empty if missing on writable initialization. Allocation can
then reissue IDs; existing run/thread duplicate checks and primary keys reject
creation of a record with an already stored full ID. They do not recover lost
reservations or detect IDs whose records were deleted or never persisted.


## Collision Handling

The main uniqueness guarantee comes from the durable monotonic allocator state,
not from random generation and not from scanning every existing job file.

The allocator supports one optional `exists(id)` callback as a safety belt.
Callers use this to reject ids already present in active or archived
definitions when local migration needs one extra check.


## Archive Buckets

Because visible id prefixes are now mixed across the whole encoded value,
archive bucketing uses the decoded tick-derived UTC bucket rather than the
literal leading chars shown in the id string.

These generic decoded buckets do not define chore lifecycle directories.
Chores have no archive stage.


## Current API

`toolang.common.ids` currently exposes:

- `IdFamily`
- `LOCAL_ID_FAMILY`
- `RUN_ID_FAMILY`
- `AllocatorState`
- `IdIssuer(db_path, agent_name=...)`
- `encode_id(...)`
- `decode_id(...)`
- `reserve_next_id(...)`
- `allocate_id(...)`
- `archive_prefix(...)`

The encode, decode, reserve, allocate, and archive-prefix helpers require
`agent_name`. `IdIssuer.issue_run()` and `IdIssuer.issue_thread(prefix)` retain
their existing signatures and prepend the full-ID prefix after allocation.
Pass the initialized `runs.db` path to `allocate_id` and `IdIssuer` (the path
parameter is now `db_path`). `AllocatorSnapshot` and `AgentLayout.id_state` have
been removed. These helpers are used for generated chore names, threads, and
runs; chore identities remain name-derived.


## Conversation IDs

Conversations use eight characters from the same base32 alphabet, prefixed by
`dm_` or `gc_`. Execution/thread ID formats are unchanged; conversation allocation
does not use `seqs`.

DM IDs hash compact UTF-8 JSON `["toolang:dm:v1", a, b]`, where `a` and `b` are
the exact typed participant names sorted by UTF-8 bytes. The first five SHA-256
bytes form the 40-bit suffix. Reversed lookups agree; a stored pair mismatch fails
as a collision. The hash is not reversible and membership cannot change.

GC IDs encode `(hourly_tick << 20) | sequence` with the shared four-round Feistel
permutation, using BLAKE2s of `toolang:conversation:gc:v1` as its fixed key. GC uses
no local-family affine transform or agent name. The epoch is 2026-01-01 UTC. Atomic
Valkey `TIME` reservations keep the tick monotonic through clock rollback; a new
hour resets the sequence to zero. This allows 1,048,576 reservations per backend
per hour (about 291.27 per second averaged over the hour), including abandoned
reservations and candidate conflicts. This is an ID budget, not a throughput
benchmark. Allocation fails at exhaustion instead of wrapping or resetting.
Retries of GC creation can create another GC if the earlier response was lost.

GC names and membership can change without changing the ID. The fixed permutation
is reversible and introduces no collisions between distinct reservations. The
[conversation contract](plans/conversation-ids.md) defines storage, exact vectors,
collision checks, and the fresh-dataset-only rollout.
