# ID Model

This document defines the proposed Toolang-owned id families and allocator
model.

This design is implemented in `toolang.ids` as an isolated prototype. It is
not yet integrated into the current task, chore, thread, or run paths.


## Goals

Toolang-owned ids should be:

- generated automatically
- short enough to copy and quote by hand
- stable across rename, move, archive, and restore operations
- monotonic at allocation time without exposing raw time buckets directly
- reversible for local tooling and archive bucketing
- safe to allocate from both CLI and runtime processes


## Scope

This design applies only to Toolang-owned local ids.

It does not rewrite opaque external ids such as:

- user-provided web thread ids like `web-1776857671893`
- transport-native ids such as `telegram:<chat_id>`
- provider-native ids such as OpenAI response ids


## Families

The current prototype defines two families:

| Family | Width | Raw layout | Intended use |
| --- | --- | --- | --- |
| `local` | 6 chars | `tick(20) + seq(10)` | task ids, chore ids, and Toolang-owned local thread ids |
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
- 2 base32 seq chars = 10 seq bits
- capacity is 1,024 ids per hour per allocator state

For the `run` family:

- 4 base32 tick chars = 20 tick bits
- 4 base32 seq chars = 20 seq bits
- capacity is 1,048,576 ids per hour per allocator state


## Full Forms

The proposed full identities are:

- task thread: `task:local:<id>`
- chore thread: `chore:local:<id>`
- local chat thread: `chat:local:<id>`
- run id: `<id>`

The bare `<id>` stays stable even when a file is renamed, moved, archived, or
restored.


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
- `feistel(...)` is one fixed-round reversible whole-id permutation seeded by
  family constants
- encoded ids are `encode_base32_fixed(wire_code)`

This gives Toolang-owned ids these properties:

- allocation order is hidden
- ids still decode back to raw `tick` and `seq`
- ids from the same tick bucket can still display different visible prefixes
- archive bucketing can still use the decoded tick-derived bucket prefix

This mechanism is not meant to be cryptographic secrecy. It is only a compact,
reversible local obfuscation layer.


## Allocation

The prototype separates allocation from integration.

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

The prototype uses one POSIX file lock around the snapshot update so multiple
CLI and runtime processes can share one allocator safely later.


## Collision Handling

The main uniqueness guarantee comes from the durable monotonic allocator state,
not from random generation and not from scanning every existing job file.

The prototype still supports one optional `exists(id)` callback as a safety
belt. Callers can use this to reject ids already present in active or archived
definitions if they want one extra local check during migration.


## Archive Buckets

Because visible id prefixes are now mixed across the whole encoded value,
archive bucketing should use the decoded tick-derived bucket prefix rather than
the literal leading chars shown in the id string.

Examples:

- `archive/tasks/<prefix>/<id>.md`
- `archive/chores/<prefix>/<id>.md`

Or the prefix can be decoded back into one UTC bucket start time and rendered as
friendlier calendar directories such as `YYYY/MM`.

The prototype currently exposes only the stable id prefix. It does not yet
define one final archive path layout.


## Current Prototype API

`toolang.ids` currently exposes:

- `IdFamily`
- `LOCAL_ID_FAMILY`
- `RUN_ID_FAMILY`
- `AllocatorState`
- `AllocatorSnapshot`
- `encode_id(...)`
- `decode_id(...)`
- `reserve_next_id(...)`
- `allocate_id(...)`
- `archive_prefix(...)`

This keeps the design concrete enough to test before wiring it into task,
chore, thread, and run creation.
