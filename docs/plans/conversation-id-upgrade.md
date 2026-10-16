# Disjoint conversation IDs and eight-character thread identities

Status: implementation authorized on 2026-10-16 as preparation for conversation
dispatch, including agent-bound chore IDs and separate manual/automatic chore
name domains. Talk still creates threads per batch.

## Goal and scope

Guarantee that generated DM and GC suffixes never overlap within one backend,
and provide a stable `talk_<8 chars>` derivation for each agent/conversation.
Use eight-character Base32 suffixes for every generated thread, including
`chore_`. Keep deterministic unordered DM lookup, atomic GC reservations, and
existing local/run codecs. Derive chore IDs from name and agent, partitioning
manual and automatic filenames. Do not implement thread reuse, dispatch controls,
conversation rules, or old-dataset migration.

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

## Chore identities

`catalog/chore_ids.py` owns filename validation and the chore codec. Hash the
exact UTF-8 stem/declaration name with SHA-256, take the first five bytes, and
mask to 39 bits. The 40-bit permutation input is `0 | hash(39)` for manual names
or `1 | hash(39)` for automatic names. Use the shared permutation keyed by compact
UTF-8 JSON `["toolang:chore:v1", agent_name]`, producing eight Base32 characters.
The domain bit is not a visible prefix. Chore threads remain `chore_<id>`.

Reserve `auto_<8 lowercase Base32 characters>` for automatic names. Automatic
authoring wraps a durable `local` allocation in that format; a manually authored
file matching it also uses the automatic domain. All other names use the manual
domain. No origin registry or authored ID field is introduced. Inline declarations
follow the same name rule.

Names and agents determine identity; title, body, schedule, and location do not.
Different-name hash collisions fail before catalog writes or State acceptance.
Domain separation guarantees no manual/automatic collision within one agent;
cross-agent uniqueness remains probabilistic. Source parsing stays portable:
`ChoreFile.id_for(agent_name)` binds identity explicitly, effective `Chore`
values carry their agent, and cached terms acquire it from the loading layout.

## Module ownership and rollout

- `common/id_codec.py`: pure Base32, framed key derivation, and permutation.
- `common/ids.py`: existing local/run families and SQLite allocation;
  reuse the primitives without changing existing golden vectors.
- `catalog/chore_ids.py`: pure chore identity and reserved automatic name format.
  Catalog, State, and authoring callers supply the concrete layout agent name.
- `teaming/ids.py`: versioned conversation bit layouts, limits, and talk-ID
  derivation. Keep conversation parsing in teaming and allocator I/O in backend.
- `teaming/backend/valkey/scripts.py`: import schema and limits from the codec
  owner; enforce independent tick/sequence limits before mutation.

Upgrade Hub and clients together. Preserve the old runtime/data for historical
access, stop writers, and configure a fresh Redis/Valkey dataset for schema `3`.
Old initialized schemas fail without mutation; no automatic reset, import, or
rewriting of conversation IDs, messages, memberships, or talk cursors occurs.
Chore IDs switch directly to the new calculation. Historical chore IDs,
checkpoints, and threads are outside scope: add no compatibility codec, migration,
or execution/cache schema gate. Local/run codec vectors remain unchanged.

## Acceptance and verification

1. Pin DM, GC, and talk golden vectors, including participant reversal, typed
   namespaces, case, and distinct Unicode spellings.
2. Verify domain separation even for equal 39-bit payloads; boundary and sampled
   GC reservations remain injective. Derived talk IDs preserve per-agent
   conversation uniqueness and reject invalid identities.
3. Preserve allocator concurrency, hourly rollover, clock rollback, restart,
   candidate-conflict budgets, and both exhaustion boundaries without mutation.
4. Reject old/future backend schemas without changing their stored contents.
5. Pin chore vectors for manual/automatic names, different agents, case, and
   Unicode spelling; equal hash payloads remain disjoint across name domains.
6. Verify eight-character chore/thread IDs, identity stability across edits and
   moves, agent binding after cache reload, collision rejection before writes,
   and automatic filename allocation under concurrent writers.
7. Keep local/run vectors and the default offline suite green.

Likely files: the modules above, chore catalog/CLI/API/State callers,
codec/backend/catalog/cache tests, the predictable-ID fixture,
`docs/ids.md`, `docs/tasks.md`, `docs/messaging.md`, and the generated Unreleased
changelog entry.
Run the default ruff, formatting, ty, and pytest checks before committing.

## Risks and open questions

This is a breaking conversation codec change and halves GC's per-hour budget.
DM pair and chore name hashes have 39 bits; collisions must fail closed.
Agent-keyed talk IDs reduce shared-input cross-agent collisions, without a
global uniqueness guarantee. Later thread reuse must verify the persisted
backend/conversation binding, including collisions with old allocated talk IDs.
There are no remaining design questions in this preparation scope.
