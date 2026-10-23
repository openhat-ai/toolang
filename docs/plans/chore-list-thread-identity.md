# Chore list thread identity

Status: Approved in conversation on 2026-10-23; implementation requested in the
same pull request.

## Goal and scope

Make each row in `too AGENT chore list` directly match the corresponding
`THREAD` value in `too AGENT inspect threads`. Change human CLI presentation
only, extending the [existing layout](chore-list-presentation.md).

## Decisions

- Keep the first header `CHORE`; replace the authored name with the complete
  `ChoreInfo.runtime.thread_id`, such as `chore_nhwyhvsv`. Read the existing
  inspection field rather than deriving another identity in the CLI.
- Show this identity for valid definitions even before their first run or
  scheduler synchronization. It identifies their execution thread; it does not
  imply that a persisted thread already exists.
- If an invalid source has no thread identity, retain its authored name as the
  first-cell fallback. Preserve its source location and diagnostic.
- Keep identity fixed and untruncated. Apply the existing width, clipping,
  omission, overflow, ordering, and summary rules using the new cell width.
- Add no name column. File locations retain `chores/<name>.md`; inline locations
  retain their source file and line number, which identify the declaration but
  do not repeat its name. Do not change title semantics.
- Preserve selectors, API schemas, ID derivation, scheduling, persistence, and
  read-only listing. Accepting `chore_...` as a chore command selector is outside
  this change.

## Implementation touchpoints

- `src/toolang/cli/toolang/commands/chore.py`: first-cell selection only.
- `tests/unit/cli/test_chore_output.py`: identity assertions, fallback coverage,
  and width expectations affected by the longer first cell.
- `tests/integration/cli/test_local_core_commands.py`: listing and historical
  thread correlation, including inline and never-run definitions.
- `docs/design/cli.md`: describe the first column and fallback.
- `CHANGELOG.md`: generate the presentation entry through
  `too aid.too update_changelog`; maintainer verification remains required.

## Acceptance checks

- A chore with execution history has exactly the same complete first-cell
  identity as its row in `inspect threads`.
- Valid file and inline definitions without runtime state display their derived
  thread identity; listing creates neither prepared state nor execution storage.
- An invalid-name source remains listed with its sanitized name, location, and
  source error, without a fabricated thread identity.
- At 80, 120, and 240 cells and with fallback geometry, identity stays complete;
  existing omission priority, diagnostics, source suffixes, and counts hold.
- Existing status, due-time selection, and error-precedence checks pass. Run
  the repository's default verification before committing implementation.

## Risks and open questions

The fixed identity can use more space than a short name, causing optional columns
to disappear sooner. An inline declaration's name may require opening its source
location. Human-output consumers expecting a name in column one must adapt.
No open design questions.
