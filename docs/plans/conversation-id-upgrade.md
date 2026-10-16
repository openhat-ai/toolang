# Short ID Preparation

Implementation authorized on 2026-10-16. Goal: disjoint conversation suffixes and
uniform eight-character thread suffixes. Talk dispatch and conversation rules
remain outside this preparation.

## Rules

1. All generated suffixes use eight lowercase Base32 characters. Keep local/run
   codecs unchanged. Add no compatibility codec or data migration.
2. Conversation schema 3 permutes disjoint inputs: DM `0 | pair_hash(39)`;
   GC `1 | hourly_tick(20) | seq(19)`. Reject mismatched backend schemas.
3. Derive future talk IDs from the conversation suffix and agent. The helper
   creates no thread; the driver continues to allocate per batch.
4. Every chore ID uses the exact name and agent: take the first 40 SHA-256 bits
   of the UTF-8 name and permute with the agent key. No filename special cases,
   manual/automatic partitions, or creation-provenance metadata.
5. Automatically create `chore-<local ID>.md`; allocate its filename suffix from
   the existing epoch/sequence allocator, then derive the chore ID from the full
   name using rule 4. State uses the same rule for every authored file.
6. Names are case-sensitive. Title/body/schedule/location edits preserve identity;
   renames create a new one. Reject different-name collisions. Agent keys reduce
   cross-agent collisions without guaranteeing global uniqueness.

## Touchpoints and checks

Shared primitives: `common/id_codec.py`; allocation: `common/ids.py`; chore rules:
`catalog/chore_ids.py`; conversation layouts/limits: `teaming/ids.py` and backend
scripts. Catalog/State/CLI/API callers supply the concrete agent.

Acceptance: golden vectors; DM/GC separation; uniform chore name handling,
including hand-created `chore-<id>` and `auto_<id>` names; State reload; edit/move
stability; concurrent allocation; unchanged local/run vectors. Run the default
offline checks and generate the changelog. No design questions remain.
