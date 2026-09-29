# Compact Chat entry labels

Status: approved for implementation on 2026-09-29.

## Goal and scope

Show anonymous entry points as `agic:-` or `flow:-` in Chat status and run
context labels. Named runnables keep their names; adhoc labels remain distinct.
This is a display-only change approved in the UI discussion.

## Design

Use the language-owned `display_runnable_ref` formatter. For the `chat`
surface only, render parsed references whose role is `entry` with `-` as the
name. Preserve the kind, defaulting to `agic` for unqualified entry references
as today. Module qualification and line numbers remain hidden in Chat labels.

Keep `<entry>` selector aliases, lined stored references, parsing, persistence,
script help, and progress diagnostics unchanged. `-` is not a new selector.
Malformed labels retain the formatter's existing unchanged-value fallback.

## Touchpoints and acceptance checks

- `src/toolang/lang/types.py`: change only the Chat entry display branch.
- `tests/unit/lang/test_program.py`: cover lined/unlined and qualified entry
  labels, both kinds, and unchanged help/progress/selector behavior.
- `tests/unit/cli/test_chat_tui.py`: verify status and run context labels use
  the same shorthand while named and adhoc labels stay unchanged.
- Update the display section of `unnamed-runnable-identity.md`.

The primary risk is accidentally applying shorthand to selectors or diagnostic
references. Surface-specific tests must distinguish presentation from identity.
Run focused language/Chat tests and the repository's default lint, format,
type, and offline test checks before committing.

No open questions.
