# Disjoint conversation IDs and derived talk-thread IDs

Status: implementation authorized on 2026-10-16 as preparation for conversation
dispatch. This scope upgrades codecs only; Talk still creates threads per batch.

## Goal and scope

Guarantee that generated DM and GC suffixes never overlap within one backend,
and provide a stable `talk_<8 chars>` derivation for each agent/conversation.
Keep eight-character Base32 suffixes, deterministic unordered DM lookup, atomic
GC reservations, and existing local/run/chore IDs. Do not implement thread reuse,
dispatch controls, conversation rules, or old-dataset migration.

## Encoding

Conversation schema `3` uses one 40-bit permutation over two disjoint domains:

| Kind | Input to the shared permutation |
| --- | --- |
| DM | `0 | pair_hash(39)` |
| GC | `1 | hourly_tick(20) | sequence(19)` |

DM pairs retain exact typed names, sorted by UTF-8 bytes. Hash compact UTF-8 JSON
`["toolang:dm:v3", a, b]` with SHA-256, take its first five bytes, and mask to
39 bits. Preserve pair verification: collisions between different DM pairs fail
without exposing or overwriting the existing conversation.

GC keeps epoch `2026-01-01T00:00:00Z`, hourly backend time, monotonic rollback
handling, and 20-bit ticks. The 19-bit sequence permits 524,288 reservations per
hour, including abandoned candidates. Exhaustion fails before advancing state.

Both kinds use the existing four-round keyed BLAKE2s Feistel permutation. Its
key is BLAKE2s of compact UTF-8 JSON `["toolang:conversation:v3"]`. Fixed-width
Base32 uses `0123456789abcdefghjkmnpqrstvwxyz`. The kind prefixes remain `dm_`
and `gc_`; distinct input domains guarantee distinct suffixes, without an index
or cross-kind retries.

`talk_thread_id(conversation, agent_name=...)` validates the canonical full
conversation ID and agent name, decodes the suffix, and permutes its 40 bits
using the key derived from `["toolang:talk-thread:v1", agent_name]`. Return
`talk_` plus eight Base32 characters. The derivation has no storage or time input
and creates no thread. Per-agent conversation mappings are injective; different
agents share the output space, so external references remain agent-qualified.
Names remain case-sensitive and Unicode spelling is preserved.

## Module ownership and rollout

- `common/id_codec.py`: pure Base32, framed key derivation, and permutation.
- `common/ids.py`: existing local/run families, SQLite allocation, and chore IDs;
  reuse the primitives without changing existing golden vectors.
- `teaming/ids.py`: versioned conversation bit layouts, limits, and talk-ID
  derivation. Keep conversation parsing in teaming and allocator I/O in backend.
- `teaming/backend/valkey/scripts.py`: import schema and limits from the codec
  owner; enforce independent tick/sequence limits before mutation.

Upgrade Hub and clients together. Preserve the old runtime/data for historical
access, stop writers, and configure a fresh Redis/Valkey dataset for schema `3`.
Old initialized schemas fail without mutation; no automatic reset, import, or
rewriting of conversation IDs, messages, memberships, or talk cursors occurs.
Existing execution IDs and SQLite stores retain their formats. This follows
the existing fresh-dataset rollout policy, with a new conversation schema gate.

## Acceptance and verification

1. Pin DM, GC, and talk golden vectors, including participant reversal, typed
   namespaces, case, and distinct Unicode spellings.
2. Verify domain separation even for equal 39-bit payloads; boundary and sampled
   GC reservations remain injective. Derived talk IDs preserve per-agent
   conversation uniqueness and reject invalid identities.
3. Preserve allocator concurrency, hourly rollover, clock rollback, restart,
   candidate-conflict budgets, and both exhaustion boundaries without mutation.
4. Reject old/future backend schemas without changing their stored contents.
5. Keep all existing local/run/chore vectors and the default offline suite green.

Likely files: the modules above, codec/backend tests, the predictable-ID fixture,
`docs/ids.md`, `docs/messaging.md`, and the generated Unreleased changelog entry.
Run the default ruff, formatting, ty, and pytest checks before committing.

## Risks and open questions

This is a breaking conversation codec change and halves GC's per-hour budget.
DM pair hashes have 39 bits; collisions remain possible and must fail closed.
Agent-keyed talk IDs reduce shared-input cross-agent collisions, without a
global uniqueness guarantee. Later thread reuse must verify the persisted
backend/conversation binding, including collisions with old allocated talk IDs.
There are no remaining design questions in this preparation scope.
