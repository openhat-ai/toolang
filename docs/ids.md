# ID Model

This document defines the Toolang-owned id families and allocator model.

This design is implemented in `toolang.common.ids` and is used for local task ids,
chore ids, Toolang-owned local thread ids, and run ids.


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
| `local` | 8 chars | `tick(20) + seq(20)` | task ids, chore ids, and Toolang-owned local thread ids |
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

The proposed full identities are:

- task thread: `task_<id>`
- chore thread: `chore_<id>`
- web chat thread: `web_<id>`
- terminal and TUI chat thread: `term_<id>`
- one-shot script invocation thread: `script_<id>`
- Telegram thread: `tg_<external_id>`
- run id: `run_<id>`

For task and chore ids, the bare `<id>` stays stable even when the authored file
is renamed, moved, archived, or restored.

Each one-shot script invocation receives a fresh `script_<id>` thread so its history
and result remain independently inspectable.


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
IDs, and the `ids.json` format is unchanged. New allocations continue from its
saved counters. Code integrations must supply `agent_name` to ID helpers and
`IdIssuer`; manual decoding requires the original agent name and codec.


## Allocation

Allocator state is durable and per-agent. It is not runtime-only state.

One shared snapshot file stores one monotonic `(last_tick, last_seq)` pair for
each id family:

```json
{
  "families": {
    "local": {
      "last_tick": 42,
      "last_seq": 3
    },
    "run": {
      "last_tick": 42,
      "last_seq": 14
    }
  }
}
```

Allocation steps:

1. compute the current tick from the configured epoch and bucket size
2. load the current family state
3. keep the tick monotonic with `max(now_tick, last_tick)`
4. increment the per-tick seq
5. encode the id
6. persist the updated state

The allocator uses one POSIX file lock around the snapshot update so multiple
CLI and runtime processes can share one allocator safely.
Host and sandbox writers must share that same file and a supported locking
domain. Independent copies of an agent's allocator file are not coordinated.
Malformed snapshots are rejected instead of being treated as empty allocator
state, which prevents silent sequence reuse after state corruption.

State-loss recovery remains manual. Stop writers before reconstructing the
snapshot. Decoding retained IDs and taking the maximum `(tick, seq)` per family
can recover a persisted high-water mark; timestamp or insertion order does not
necessarily match allocation order. `runs.db` is not a complete allocation log:
jobs and scheduler claims have other stores, and deleted or unpersisted IDs
cannot be reconstructed from it. No automatic scan or repair is performed.


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

Examples:

- `archive/tasks/20260423T10Z/<id>.md`
- `archive/chores/20260423T10Z/<id>.md`

The `T` separates date from hour and `Z` makes the UTC timezone explicit while
keeping the directory compact.


## Current API

`toolang.common.ids` currently exposes:

- `IdFamily`
- `LOCAL_ID_FAMILY`
- `RUN_ID_FAMILY`
- `AllocatorState`
- `AllocatorSnapshot`
- `IdIssuer(state_path, agent_name=...)`
- `encode_id(...)`
- `decode_id(...)`
- `reserve_next_id(...)`
- `allocate_id(...)`
- `archive_prefix(...)`

The encode, decode, reserve, allocate, and archive-prefix helpers require
`agent_name`. `IdIssuer.issue_run()` and `IdIssuer.issue_thread(prefix)` retain
their existing signatures and prepend the full-ID prefix after allocation.
These helpers are used by local task, chore, thread, and run creation.


## Conversation IDs

Conversations use eight characters from the same base32 alphabet, prefixed by
`dm_` or `gc_`. Execution/thread outputs and per-agent allocator files are unchanged.

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
