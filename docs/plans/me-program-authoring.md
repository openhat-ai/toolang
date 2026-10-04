# Current-agent program authoring

## Goal and approved scope

Implement the interface agreed in the design discussion: `me` can inspect and
edit the current main program by declaration, or replace its complete source.
The human requested implementation and a PR after agreeing to the operations,
comment ownership, full-file digest, and whole-program validation below.

## Contract

- Add `kind=program` to the existing five operations. List returns declarations
  in source order. Get/update without key address the complete main program.
  Create/delete require a declaration key; whole-program create/delete are absent.
- Keys are `kind:name` for agic, flow, instruct, context, struct, psyche, skill,
  service, prompt, task, and chore. Unnamed agic/flow use `_`; unnamed
  instruct/context use their language name `default`. Imports (`with`) and
  file-level comments are edited through whole-source replacement.
- Create appends one declaration, rejecting duplicates. Keyed update requires
  one matching declaration and replaces it; delete removes it. Updates do not
  rename declarations. Cross-declaration changes use whole-source replacement.
- Content is `{source: string}`. Plain and item-doc comments immediately above
  a declaration belong to it; a blank line separates file comments. Shebangs
  and module-doc comments always remain file-level. Preserve unrelated bytes.
- All item digests cover the complete file. Required `if_digest` on every
  program write checks that snapshot under the shared home-program authoring lock. Prepare the response
  and validate the composed main program plus authored flows before atomic save.
- Whole-file get does not parse; whole-file update can repair malformed source.
  Syntax errors, semantic errors, key mismatch, missing items, conflicts, and
  digest conflicts are structured tool failures with no partial writes.
- Resolve the current main program from AgentLayout only. Resident/visiting
  source must be regular; for roaming scripts allow only the canonical runtime
  link to `<source directory>/<agent name>.too`, preserving that link.
- Keep existing operation semantics for other kinds. No arbitrary
  path, root scope, configured-cap editing, or new tool leaves.

## Runtime and knowledge

Explain inline declarations/caps, authored caps/flow modules, configured refs,
source scope, editing operations, comment ownership, and version adoption in
protocol and tools documentation. Saving source does not publish State or replace
accepted Run code; static flow calls retain their parent's bound version. Runtime
hot swapping and changes to call authorization are excluded.

## Touchpoints

- `lang`: pure CST-backed declaration indexing and source splicing.
- `execution/tools/me`: schemas, dispatch, safe storage, diagnostics, descriptions.
- Runtime protocol and `docs/tools.md`: agent-facing operation and ownership guide.
- Language and tool integration tests: editing, storage, contracts, validation.

## Acceptance and risks

Cover named/unnamed declarations, all supported declaration kinds, comment and
Unicode preservation, append/replace/delete, full replacement, invalid-source
recovery, key mismatch/extra declarations, invalid composition, concurrent digest
conflicts, symlink boundaries, roaming source writes, and unchanged existing kinds.
Verify watcher publication remains separate. Run all default checks before commit.

Comment ownership can surprise authors: document adjacency, preserve file-level
trivia, and reject fragment content that cannot belong to the target declaration.
Whole-file digest can reject otherwise independent edits; callers reread and retry.
No open design questions remain in this scope.

## Follow-up decisions during implementation

- Program responses expose the bound Run's main-source digest separately from
  the authored digest and report whether they match. All program writes require
  the authored whole-file digest; other kinds retain optional preconditions.
- Standardize resource lock names as `.<target name>.lock`: main/flow mutations
  acquire `.agent.too.lock` then `.flows.lock`. Configured caps/workspaces already
  share `.config.toml.lock`; roaming projection joins that lock instead of using
  `.project.lock`. Older writers must be restarted on upgrade.
