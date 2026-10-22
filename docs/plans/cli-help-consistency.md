# Plan

Unify CLI help wording, argument notation, field reference sections, and example
layout. Success means concise, consistent help across `too`, `toolang`, `caps`,
agent-target help, and generated script help, with unchanged invocation behavior.

Status: historical definition. Current wording, placeholders, and topic references
are documented in [CLI help vocabulary](../design/cli.md#help-vocabulary).
The standalone `caps` command and inline field reference sections below were removed.

## Scope

- In: built-in help text and rendering, generated runnable usage, focused tests,
  and current documentation that describes changed help conventions. This plan
  supersedes only the corresponding presentation rules in
  [Script Help Layout](script-help-layout.md) and
  [Script CLI Input Usage](script-cli-input-usage.md).
- Out: command names, flags, routing, parsing, execution, policy semantics,
  command visibility/grouping, authored descriptions, flow outlines, interactive
  chat help, new dependencies, and special layouts for unusually long examples.
  This definition ships no implementation code.

## Action items

- [ ] Standardize built-in wording. Use concise, verb-first command summaries;
  use lowercase ordinary nouns in prose (`agent`, `run`, `thread`, `source ref`,
  `hub`, `chore`). Preserve sentence-initial capitals, proper names, acronyms,
  authored types, and literal identifiers. Use sentence-case headings, no final
  periods in row descriptions, and periods in standalone sentences. Use the
  same terms across parent/child help and cap entry points: `List available caps`,
  `Add a remote skill reference`, `Remove a skill reference`, and
  `Delete a local skill`; preserve the reference/file distinction. Keep
  `TARGET` simply `New agent name`; briefly explain omissions such as `home`
  defaulting to the Toolang root.

- [ ] Keep required/optional notation simple. Static usage uses `<NAME>` for
  required operands, `[NAME]` for optional operands, `<NAME>...` for one or more,
  and `[NAME...]` for zero or more. Option values use `--port <PORT>` when a value
  is required and `--thread [THREAD]` when it can be omitted, independently of
  whether the option is required. Retain defaults and bare-option values.
  Uppercase placeholder-only labels, preserve assignment keys, and use `*`
  only for required Arguments rows. Runnable usage uses one `[ARGUMENTS]` when the
  signature accepts named or primary input, otherwise omits it. Do not expand
  assignments or capture alternatives in usage. The Arguments panel retains
  signature order, types/docs, and stdin/line-input guidance; category brackets do not
  make required inputs optional:

  ```text
  Usage: too demo.too greet [OPTIONS] [ARGUMENTS]

  Arguments:
    * name=<NAME>  Named input (Text)
      tone=<TONE>  Named input (Text)
  ```

- [ ] Compose supplemental sections through existing formatter hooks. Callers
  choose rows and paragraphs using `section()`, `write_dl()`, `write_paragraph()`, and
  `write_text()`. The renderer owns heading styles, indentation, column alignment,
  and wrapping. Share field writers in CLI-owned code; keep field semantics out
  of the common formatter. Preserve lazy loading, consoles, themes, and ordinary
  epilog prose. Do not parse epilog headings, add a schema/DSL, or pad rows by hand.
  Place field sections after Options, then Examples, then any remaining footer.

- [ ] Shorten option descriptions and add only the field sections supported by
  that command. Use `Allow fields`, `Limit fields`, and `Default fields`, with
  concise field rows followed by applicable value/repetition rules:

  | Section | Fields and required details |
  | --- | --- |
  | Allow fields | `models`, `tools`, `psyches`, `skills`, `services`, `prompts`; TQ queries, `all`, and `none`; repeated queries for one resource form a union; queries cannot mix with `all`/`none`. |
  | Limit fields | `agic_model_calls` and `agic_tool_calls` count per agic; `tokens` totals input/output tokens, `cost` is USD, and `time` is seconds per run tree; non-negative integers, decimal cost, and `none`; each field once. |
  | Default fields | `model` and `runnable`; each field once; quote values containing spaces. |

  Preserve existing policy-layer restrictions when explaining sentinels. Use
  short option summaries pointing to these sections. Where model options need
  expansion, share a `Model parameters` section for `effort` and `max_output`,
  preserving the exact-model requirement of `--compact-model`. Move the dense
  `inspect` query explanation into caller-organized syntax sections and examples
  without changing its grammar.

- [ ] Render Examples as compact command/description rows using `write_dl()`.
  Descriptions wrap under their own column without blank lines between examples
  or shell prompts. Use existing wrapping for long commands; defer special
  handling until a concrete case requires it:

  ```text
  Examples:
    too talk        List conversations
    too talk alice  Open a conversation with an agent
  ```

- [ ] Implement through the existing factories and helpers. Likely touchpoints:
  `src/toolang/common/typer/ui.py` only for shared layout support if needed;
  `src/toolang/cli/common/{help,lazy,parameters,routing,runnable_parameters}.py`
  for integration; CLI-owned field-section helpers;
  `src/toolang/cli/toolang/main.py`, `src/toolang/cli/toolang/commands/`, and
  `src/toolang/cli/caps/` for wording and section composition. Verify field names
  against `cli/common/policy.py` and model parsing. Validate examples against
  their intended scripts/templates; the repository's `aid.too` differs from
  the generated template.

- [ ] Add offline acceptance coverage in `tests/unit/common/typer/` and
  `tests/unit/cli/{test_cli_help,test_runnable_parameters,test_script_command,test_script_entry}.py`.
  Check 44/80/120-column output in PLAIN/UV themes and without color: shared
  heading styles, aligned continuation lines, complete literal text, compact
  example rows, and section paragraphs. Cover named-only, primary-only, mixed,
  optional-only, and empty signatures; static operands; option values; and all
  scoped entry points. Supported field sections appear once; others stay absent.
  Explicit help exits 0 without stdin reads, network calls, or execution;
  incomplete-input help retains existing exit codes and required markers.
  Retain parser/policy tests; assert content/layout properties, not full snapshots.

- [ ] Verify updated examples and relative links, synchronize current help
  documentation where necessary, and generate relevant Unreleased coverage with
  `too aid.too update_changelog` during implementation. Run `git diff --check`
  and the required implementation checks: `uv run ruff check .`,
  `uv run ruff format --check .`, `uv run ty check`, and `uv run pytest -n auto`.
  For this definition-only change, validate the plan and run `git diff --check`.

## Open questions

None. Risks: field/parser drift and inconsistent lazy/static/generated help;
shared CLI writers and acceptance coverage address both.
