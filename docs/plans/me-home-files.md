# Current-agent home files through me

## Goal and approved scope

Implement the latest interface agreed in the discussion: `me` represents the
current agent and manages its latest home files. Keep the five operations and
toolset name. Do not add inspect, revision selectors, shadow projections, source
editing tools, or runtime-tool renaming. This supersedes the earlier bound-State
and declaration-editing proposals.

## Contract

- `list()`; `get(key)`; `create(key, content, encoding?)`;
  `update(key, content, if_digest, encoding?)`; `delete(key, if_digest)`.
- Keys are canonical paths relative to the current home, with no kind selector:
  `agent.too`, `config.toml`, `flows/<name>.too`, `psyches/<name>.md`,
  `services/<name>.md`, `prompts/<name>.md`, `skills/<name>/SKILL.md`, and
  `skills/<name>/assets/**`. Each mutation addresses exactly one file.
- Ready `tasks/*.md` and `chores/*.md` also support complete file CRUD, as
  explicitly requested. They use live disk content and required write digests.
  Delete removes that file; it does not archive it or cancel existing Runs.
  Draft/archive directories and other job lifecycle operations stay outside me.
- All reads use latest disk bytes. List returns sorted file metadata (`key`,
  `digest`, `bytes`); get adds complete `content` and `encoding`.
  UTF-8 is the default. Non-UTF-8 bytes are returned as base64; callers can use
  base64 to write exact binary bytes. Digests always hash decoded file bytes.
- Create requires absence; update/delete require a lowercase SHA-256 digest.
  Check under the owning lock before atomic replacement/removal.
  Successful writes return the actual saved content/digest; identical updates
  report `changed=false`. Read after write observes the saved file.
- Do not parse or validate file content in me. Syntax, metadata, and composition
  errors belong to the existing loaders/watchers, equally for me writes and
  direct filesystem edits. Saving succeeds even for invalid content. State
  watcher rejection retains its last valid publication and reports diagnostics;
  repairing the source allows a later refresh to publish. Me does not install
  plugins, resolve remote references, or publish State. Removing SKILL.md never
  recursively removes assets.
- No arbitrary host paths, root resources, other agents, runtime files, or
  symlink traversal. Keep the previously agreed canonical roaming main-program
  link to its own original script; arbitrary links (including config links) are
  rejected. Reads never allocate job ids or prepare State.
- Saving changes authored definitions only. Active Runs/Setup retain their
  existing adoption rules. No revision/shadow metadata is mixed into file results.

## Implementation

Replace old kind dispatch and per-kind content fields with file classification,
exact-byte reads/writes, and digest preconditions in `execution/tools/me`. Reuse
the normal toolset factory and MeToolContext. Add byte writes and consistent lock
paths in `common/files`; update configured writers and roaming projection to use
that helper. Use `.agent.too.lock` for the main program, `.flows.lock` for flows,
`.config.toml.lock` for config (shared with configured caps/workspaces and roaming
projection), `.caps.lock` for caps/assets, and `.jobs.lock` for jobs. Update the
runtime authoring protocol and tools documentation. Snapshot inspection and
AST-based declaration editing are outside scope.

## Acceptance

Verify all whitelisted paths, latest reads, exact CRLF/Unicode/binary digests,
full-file CRUD, read-after-write, required digest and concurrent conflicts,
invalid TOML/cap/program content saved exactly, loader rejection and recovery
regardless of writer, broken composition after single-file changes,
asset round trips, job CRUD and reads without implicit id allocation, arbitrary path/root/
symlink rejection, canonical roaming writes, mode preservation, unchanged State
and active Run behavior, and exactly five schemas with no kind/revision selector.
Run default Ruff lint/format, ty, full offline pytest, and diff checks.

## Risks

This intentionally replaces old kind-based me calls. Full config replacement must
start from get so uncaptured Setup fields/comments survive; writes do not reload
Setup. Old writers must restart when switching flow lock names. Filesystem locks
coordinate cooperating writers; external editors must not bypass them during a
concurrent save. No open design decisions remain in this scope.
