# Compact entry labels and selectors

Status: approved on 2026-09-29; revised on 2026-09-30.

## Goal and scope

Show anonymous entry points as `agic:_` or `flow:_` in Chat status and run
context labels, and `_` in the script help name column. Script descriptions
retain the lined identity, such as `<entry:5>`, followed by any authored comment.
Named runnables keep their names; adhoc labels remain distinct.
Use `_` to select the unnamed entry, as omitting the runnable does in script
calls. Remove the old `<entry>` selector alias.

## Design

Use the language-owned `display_runnable_ref` formatter. For the `chat` and `help`
surfaces, render parsed references whose role is `entry` with `_` as the
name. Preserve the kind, defaulting to `agic` for unqualified entry references
as today. Module qualification and line numbers remain hidden in Chat labels.

Accept `_`, `agic:_`, and `flow:_` at runnable selection boundaries. Script
dispatch also accepts `runnable:_`. Resolve them to the unique unnamed entry;
reject a missing entry or mismatched kind without falling back to a named one.
Script calls may omit the selector; existing Chat session defaults are unchanged.

Script commands no longer accept `<entry>`, `<entry:N>`, or the implicit `entry`
alias. A genuinely named `entry` remains selectable. Unlined `<entry>` is also
rejected by runtime selection; lined references remain valid internal identities.
Keep persistence and progress diagnostics unchanged. Script input `-` continues
to read stdin. Source declarations still omit the name; `_` is not a legal
authored runnable name, so the selector has no name collision.
Malformed labels retain the formatter's existing unchanged-value fallback.

## Touchpoints and acceptance checks

- `src/toolang/lang/types.py`: share the entry shorthand across Chat and help.
- Script dispatch, shared runtime resolution, and module-local lookup: resolve
  `_` to a lined entry identity, including input-local runnable overrides.
- `tests/unit/lang/test_program.py`: cover lined/unlined and qualified entry
  labels, both kinds, and the selector role without changing stored identities.
- `tests/unit/cli/test_chat_tui.py`: verify status and run context labels use
  the same shorthand while named and adhoc labels stay unchanged.
- Script help tests: verify the name, kind, source line, and authored comment.
- Invocation tests: cover omitted and explicit selectors, kind mismatches,
  missing entries, rejected legacy aliases, and `_ -` reading stdin.
- Hosted execution tests: verify canonical lined identities reach the records.
- Update the display section of `unnamed-runnable-identity.md`.

The primary risk is persisting a selector instead of its resolved identity.
Surface-specific tests must distinguish selection, presentation, and identity.
Run focused language/Chat tests and the repository's default lint, format,
type, and offline test checks before committing.

No open questions.
