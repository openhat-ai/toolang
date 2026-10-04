# Current-agent home files through me

## Goal

Give agents one file interface for reading and changing their own authored home
files. A key identifies a complete file relative to home. Reads return current
disk content, and digest preconditions prevent overwriting a changed file.
Receipts can be compared with the immutable files captured by the calling State.

## Contract

- `list()` returns `{files: [{key, digest, bytes}]}`, sorted by key.
- `get(key)` returns that metadata plus complete `content` and `encoding`, without
  an `item` wrapper.
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
  including comments and line endings. Create/update return only `{key, digest}`;
  identical updates return the same receipt. Delete returns `{key, digest: null}`.
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

## Loaded files and errors

- Every new AgentState exposes immutable `files` entries `{scope, key, digest}`,
  sorted by scope and key. Scope is `root` or `home`; keys are relative to that
  scope. The list records construction inputs, including shadowed files and skill
  assets, rather than only effective declarations. Digests hash raw authored
  bytes and participate in State identity, including when config paths are rebased.
- `loaded(receipts)` compares the input `{key, digest}` objects against the home
  files of the State bound to the current call. It returns
  `{loaded, revision, mismatches: [{key, digest}]}`. Mismatch digests are loaded
  values; expected values remain in the input. No disk read, refresh, publication,
  waiting, or revision selector is involved.
- Missing or untracked keys have digest `null`, including independent task/chore
  files. A null receipt matches absence from the list; it does not prove that a
  task was cancelled or that a same-named root definition is absent. Empty input
  matches. Duplicate receipt keys are rejected as `invalid_request`.
- Failures are flat `{error, message, key?}` objects. Codes are `invalid_request`,
  `not_found`, `already_exists`, `digest_mismatch`, and `io_error`.
  Digest mismatches also include `expected_digest` and `actual_digest`. Successful
  outputs never contain `error`; `loaded: false` is a successful comparison.
- Historical manifests without raw config identity do not claim a loaded config
  digest. Preparation upgrades the layer/source schemas without modifying old
  revisions. Existing runtime adoption and Setup refresh behavior is unchanged.

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
  for this file interface. `state/source`, `state/cache`, and State composition
  retain raw source identities; the executor injects its captured State into me.

## Acceptance

Verify all supported paths, exact Unicode/CRLF/binary round trips, raw-byte digests,
full-file CRUD, required digest conflicts, cooperating writers, atomic-save
failure, path boundaries, roaming links, and legal filesystem names. Confirm
invalid content is saved unchanged, the State watcher rejects and recovers from
bad source regardless of writer, and active Runs can read and repair newer files
without replacing their bound revision. Verify flat successes/errors, loaded
receipts before and after publication/deletion, untracked and empty inputs, root
shadowing, raw config digests, historical loading, immutable snapshot round trips,
and the executor's actual call binding. Run the default verification suite.

## Compatibility and risks

Callers use file paths and complete content strings instead of kind/name pairs
and parsed fields. Update/delete require a digest. Preserve unrelated source and
config fields when replacing a file. Locks coordinate participating writers;
external editors that bypass them can race a save. Runtime adoption and Setup
refresh continue through their existing mechanisms. Restart older writers when
changing from `.authored-flows.lock` to `.flows.lock` or from `.project.lock` to
`.config.toml.lock` so all writers use the same locks.
