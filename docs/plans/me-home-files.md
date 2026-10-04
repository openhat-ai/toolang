# Current-agent home files through me

## Goal

Give agents one file interface for reading and changing their own authored home
files. A key identifies a complete file relative to home. Reads return current
disk content, and digest preconditions prevent overwriting a changed file.

## Contract

- `list()` returns sorted file metadata: `key`, `digest`, and `bytes`.
- `get(key)` also returns complete `content` and `encoding`.
- `create(key, content, encoding?)` requires the file to be absent.
- `update(key, content, if_digest, encoding?)` replaces one complete file.
- `delete(key, if_digest)` removes one file. Removing a skill definition leaves
  its assets; removing a task/chore does not archive it or cancel an active Run.
- Supported paths: `agent.too`, `config.toml`, `flows/<name>.too`,
  `psyches/<name>.md`, `services/<name>.md`, `prompts/<name>.md`,
  `skills/<name>/SKILL.md`, `skills/<name>/assets/**`, `tasks/<name>.md`, and
  `chores/<name>.md`.
- Content uses UTF-8 by default. Non-UTF-8 files are returned as base64; base64
  input supplies exact binary bytes. Digests are lowercase SHA-256 of file bytes,
  including comments and line endings. Successful writes return the saved item;
  identical updates report `changed=false`.
- Update/delete check `if_digest` under the owning write lock. A conflict requires
  rereading and reconciling the file. Writes replace files atomically and preserve
  existing modes. Reads do not allocate job ids or prepare State.
- Validate paths, request encoding, and concurrency preconditions in me. File
  syntax, metadata, and composition validation belong to existing loaders and
  watchers, equally for me writes and direct filesystem edits. A successful save
  does not indicate valid content or publish a new runtime State.
- Access stays within the listed home paths. Reject directory operations and
  arbitrary symlinks. The canonical roaming `agent.too` link addresses its own
  source file and is preserved during edits.

## Implementation scope

- `execution/tools/me`: replace kind-specific handlers and schemas with whole-file
  operations, path classification, raw-byte digests, and structured I/O errors.
  Remove the flow-specific CRUD implementation once the shared file path owns it.
- `common/files`: add atomic byte writes, reused by the text writer and me, and
  a shared lock-path helper using `.<target>.lock`.
- Main-program operations use `.agent.too.lock`; flow operations use
  `.flows.lock`. Config writers in `catalog/config`, `state/config`, and the
  roaming projection in `up/process` share `.config.toml.lock`. Cap/asset and job
  operations use `.caps.lock` and `.jobs.lock` respectively.
- Update the runtime authoring protocol, tools documentation, and acceptance tests
  for this file interface. Runtime loading, publication, and Run binding retain
  their existing behavior.

## Acceptance

Verify all supported paths, exact Unicode/CRLF/binary round trips, raw-byte digests,
full-file CRUD, required digest conflicts, cooperating writers, atomic-save
failure, path boundaries, roaming links, and legal filesystem names. Confirm
invalid content is saved unchanged, the State watcher rejects and recovers from
bad source regardless of writer, and active Runs can read and repair newer files
without replacing their bound revision. Run the default verification suite.

## Compatibility and risks

Callers use file paths and complete content strings instead of kind/name pairs
and parsed fields. Update/delete require a digest. Preserve unrelated source and
config fields when replacing a file. Locks coordinate participating writers;
external editors that bypass them can race a save. Runtime adoption and Setup
refresh continue through their existing mechanisms. Restart older writers when
changing from `.authored-flows.lock` to `.flows.lock` or from `.project.lock` to
`.config.toml.lock` so all writers use the same locks.
