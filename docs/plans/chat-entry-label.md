# Compact entry labels

Status: approved on 2026-09-29; revised on 2026-09-30.

## Goal and scope

Show anonymous entry points as `agic:_` or `flow:_` in Chat status and run
context labels, and `_` in the script help name column. Script descriptions
retain the lined identity, such as `<entry:5>`, followed by any authored comment.
Named runnables keep their names; adhoc labels remain distinct.
This is a display-only change approved in the UI discussion.

## Design

Use the language-owned `display_runnable_ref` formatter. For the `chat` and `help`
surfaces, render parsed references whose role is `entry` with `_` as the
name. Preserve the kind, defaulting to `agic` for unqualified entry references
as today. Module qualification and line numbers remain hidden in Chat labels.

Keep `<entry>` selector aliases, lined stored references, parsing, persistence,
and progress diagnostics unchanged. `_` remains a valid authored name rather
than an entry selector alias; script input `-` continues to read stdin.
Malformed labels retain the formatter's existing unchanged-value fallback.

## Touchpoints and acceptance checks

- `src/toolang/lang/types.py`: share the entry shorthand across Chat and help.
- `tests/unit/lang/test_program.py`: cover lined/unlined and qualified entry
  labels, both kinds, and unchanged progress/selector behavior.
- `tests/unit/cli/test_chat_tui.py`: verify status and run context labels use
  the same shorthand while named and adhoc labels stay unchanged.
- Script help tests: verify the name, kind, source line, and authored comment.
- Update the display section of `unnamed-runnable-identity.md`.

The primary risk is accidentally applying shorthand to selectors or diagnostic
references. Surface-specific tests must distinguish presentation from identity.
Run focused language/Chat tests and the repository's default lint, format,
type, and offline test checks before committing.

No open questions.
