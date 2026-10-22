# Plan

Add standalone option-reference topics through the existing lazy CLI factory and
help formatter. The human approved this command-only portion of
[the help-topic definition](https://github.com/openhat-ai/toolang/pull/920).

## Scope

- In: `too help`, the `allow`, `limit`, `default`, and `model` topics, both
  executable names, and a root-help footer hint beside `too more`.
- Out: changes to other commands, option metavars/descriptions, existing field
  appendices, parsers, runtime behavior, and shared formatting. Separate PRs own
  those changes.

## Action items

- [x] Register a root-hidden lazy `help` group in `cli/toolang/main.py` and add
  its footer hint; keep existing command panels unchanged.
- [x] Add `cli/toolang/commands/help.py`. The directory lists four native topic
  subcommands. Bare invocation, `-h`, and `--help` show identical directory/topic
  content. Topic pages omit helper-command Usage/Options boilerplate.
- [x] Render `<KEY>=<VALUE>`, Keys, shared Values, and examples with existing
  styles. Keep key-specific values and restrictions beside their definitions;
  model grammar owns model parameters and reset/compaction constraints.
- [x] Verify reference content against current parsers/configuration, including
  runtime versus Chat defaults, sentinel mixing, units, and quoting.
- [x] Cover stdout/exit 0, native stderr/exit 2, reserved-name collisions,
  44/80/120-column styling, copyable examples, and offline operation without
  stdin, configuration/catalog loading, source acquisition, or runtime setup.
- [ ] Document the new entry, generate its changelog entry through `aid.too`,
  run the default checks, and open an implementation PR with this plan.

## Risks and open questions

Reference/parser drift and the new reserved `help` name require acceptance
coverage. Colliding agents remain selectable as `agent:help`; script entries
remain selectable through `run FILE help` or typed runnable selectors. No open
questions remain.
