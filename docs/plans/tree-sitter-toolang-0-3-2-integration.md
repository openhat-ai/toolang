# Adopt Tree-sitter Toolang 0.3.2

## Status and Goal

Approved by the human on 2026-09-14, following the
[approved upstream adoption contract](https://github.com/openhat-ai/tree-sitter-toolang/blob/v0.3.2/docs/plans/documentation-comments.md#required-toolang-adoption).
Implementation is drafted in [Toolang #532](https://github.com/openhat-ai/toolang/pull/532).

Adopt the published grammar and carry module, runnable, and parameter
documentation from authored source into existing AST and consumer interfaces.
Success requires a PyPI-backed installation, preserved language and history
boundaries, complete offline verification, and green PR checks.

## Baseline at Approval

- [PyPI 0.3.2](https://pypi.org/project/tree-sitter-toolang/0.3.2/) is available,
  with an sdist and macOS, Linux, and Windows wheels. Its release provenance
  identifies tag `v0.3.2`, commit `ac0e2d942bc7e7c4022a6f45bf16dfff53eb9562`.
- PR #532 at `057b15cb` requires `>=0.3.2,<0.4` but still uses a temporary uv
  Git source. The semantic integration, formatting, cache rebuilding, and
  documentation changes are already implemented in that draft.
- The draft passes all four default checks locally: 5,376 tests pass and 20
  are skipped. Package validation and all six Linux/macOS Python 3.11–3.13
  test jobs pass in CI. Quality fails because the Git dependency has no package
  file hash for pip-audit; CI Gate consequently fails. PyPI verification remains
  outstanding, and the PR is not ready to merge.

## Scope and Decisions

In scope: dependency provenance, CST comment consumers, documentation binding,
formatting, derived State metadata, existing help/calling descriptions, and
their tests and current documentation.

Out of scope: new AST nodes or fields, new CLI commands, argument coercion or
default changes, richer runnable-query schemas, new documentation tags, upstream
grammar changes, runtime routing authority, execution-store migrations, and a
Toolang package release.

1. **Dependencies:** retain `tree-sitter-toolang>=0.3.2,<0.4`, remove its uv Git
   override, and lock the published 0.3.2 artifacts and hashes. Keep the existing
   Tree-sitter range and locked 0.25.2 runtime. Avoid unrelated lock upgrades.
2. **CST and AST:** consume `plain_comment`, `shebang_comment`,
   `item_doc_comment`, and `module_doc_comment`, using their structured fields.
   Collect column-zero module text into each file's `Program.doc`; ordinary
   attached item text goes into the target's `doc`. Parameter tags populate the
   existing `Parameter.doc`, separately from the runnable description. The AST
   dataclasses and serialization shape stay unchanged.
3. **Parameter binding:** bind tags by exact signature name, independently of
   tag order. Support explicit and implicit `_`; an empty signature has no
   primary input. Types, optionality, and parameter order come from the signature.
   Reject attached unknown/duplicate names and tags attached to non-runnables.
   Ignore well-formed detached tags; malformed reserved tags remain syntax errors.
4. **Consumers:** callers read `runnable.input` and `runnable.params` directly.
   Reuse `runnable_parameters()` for full CLI help and `runnable_signature()` for
   structured input/output contracts. Calling descriptions keep the existing
   512-code-point documentation limits and routing authority. Runnable queries
   retain runnable descriptions, parameter names, and required-parameter names;
   detailed parameter documentation remains available through the AST/signature.
5. **Formatting:** preserve `##!` compatibility, module-comment source order in
   the collected description, documentation attachment, literal text, and
   shebang classification. Moving module comments must preserve the attachment
   boundary at their former location. Formatting is idempotent and preserves
   semantic AST data apart from source spans.
6. **Persistence:** retain the draft's State layer schema 8. Preparation rebuilds
   older derived metadata; loading an exact historical revision preserves its
   recorded Programs and files. Do not rewrite history or change the execution
   store schema.

## Implementation Touchpoints

| Area | Files |
| --- | --- |
| Remaining dependency changes | `pyproject.toml`, `uv.lock` |
| Existing semantic and formatting changes | `src/toolang/lang/lower.py`, `src/toolang/lang/format.py` |
| Existing cache boundary | `src/toolang/state/cache.py` |
| Consumer verification | `src/toolang/cli/common/runnable_parameters.py`, `src/toolang/execution/runnables.py`, `src/toolang/state/runnable_collections.py` |
| Acceptance tests | `tests/unit/lang/test_doc_comments.py`, `tests/unit/lang/test_program_format.py`, `tests/unit/execution/test_route_snapshots.py`, `tests/unit/state/test_prepare.py` |
| Current documentation | `docs/program.md`, `docs/toolang-authoring-conventions.md`, `docs/agent-state.md` |

## Execution and Acceptance

- [ ] Replace the Git source with PyPI 0.3.2 and regenerate only the affected lock
  entries. Confirm the registry, version, artifact hashes, and unchanged runtime
  dependency versions; confirm no grammar Git reference remains.
- [ ] Recheck the existing acceptance tests against the published package:
  named/unnamed agics and flows, explicit/implicit/absent input, optional and
  reordered parameters, validation errors, detached docs, LF/CRLF, EOF, Unicode,
  legacy module comments, literal markers, and formatting attachment boundaries.
- [ ] Verify authored docs reach CLI help, runnable query descriptions,
  `hands`/`handoffs`, and input contracts with their existing limits. Verify AST
  serialization and old-cache rebuilding preserve exact historical data.
- [ ] Build the Toolang wheel into a temporary directory and install it in a
  clean environment using PyPI dependencies, with no editable checkout or uv
  source override. Confirm imports resolve to that installation and parse a
  module/runnable/parameter-documentation fixture successfully.
- [ ] Fetch and rebase onto the latest `origin/main`. Before committing, run
  `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`, and
  `uv run pytest`. Validate the edited documentation examples and links.
- [ ] Run CI's dependency export and pip-audit with their existing options.
  Require Quality, all six test jobs, Package, and CI Gate to pass; investigate
  any remaining failure rather than exempting the grammar from the audit.
- [ ] Update PR #532's description to the final PyPI implementation, push with
  `--force-with-lease` after any rebase, resolve every review thread, and mark it
  ready for human review. Merge remains a human decision.

## Risks and Open Questions

The patch release changes public CST names, so the dependency and its consumers
must ship together. Newly reserved or semantically invalid `@param` comments
can reject sources previously treated as prose, including during cache rebuild.
Formatter changes can silently alter attachment or literal content; round-trip
tests cover those boundaries. A working Git checkout does not prove normal
package installation or hash-based auditing, hence the separate PyPI checks.

No technical design questions remain open. The human has approved this scope
for implementation in PR #532; merge remains a separate human decision.
