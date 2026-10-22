# Plan

Move shared option-reference details into explicit CLI help topics. Success means
ordinary `--help` keeps visible options concise, while `too help TOPIC`
provides the corresponding keys, rules, and examples without runtime setup.

Status: proposed; human confirmation is required before implementation.

See [Expected output and design review](cli-help-topics-output.md) for all topic
pages, navigation/error cases, and representative affected command help.

## Scope

- In: a topic-help entry point, discovery links, and relocation of the existing
  `Allow fields`, `Limit fields`, `Default fields`, and `Model parameters`
  appendices across Chat, runtime commands, rerun, and generated script help;
  consistent assignment metavars and caller-specific default descriptions.
  This supersedes their placement, references, and assignment metavars in
  [CLI Help Consistency](cli-help-consistency.md).
- Out: option names, accepted values, parsing, policy and execution semantics,
  command-help aliases, additional topics, `--help-all`, pagers, interactive Chat
  help, and unrelated help layouts. This definition ships no implementation code.

## Action items

- [ ] Add `too help [TOPIC]` as a native command group through the existing lazy
  group factory, with four fixed, case-sensitive topic subcommands in this order:

  | Topic | Summary | Referenced options |
  | --- | --- | --- |
  | `allow` | Resource selection keys and query rules | `--allow` |
  | `limit` | Run limit keys and units | `--limit` |
  | `default` | CLI defaults and their setting lifetime | `--default` |
  | `model` | Model selection and parameters | `--model`, `--compact-model` |

  `too help` and `too help --help` show the same usage and topic directory,
  with a reminder to use `too COMMAND --help` for command-specific options.
  For each topic, bare invocation, `-h`, and `--help` show the same reference;
  eager help must stay on the selected topic. Render reference content directly:
  single-option topics start with the option declaration and a short explanation;
  `model` starts with its topic title and the relevant option forms. Omit the
  helper command's description/Usage/Options wrapper and its self-help row.
  Keep normal directory help and native usage in invocation errors.
  Successful output goes to stdout and exits 0. Unknown topics, options, and
  extra operands use native diagnostics on stderr and exit 2; command names
  such as `chat` are not topics. Support
  the same paths through `toolang`. Topic leaves have no business operands.

- [ ] Make topics discoverable without expanding the main command directory.
  Register `help` as a root-hidden command, like `more`, and add
  `Run 'too help' to see option help topics.` to the root-help footer alongside
  the existing `too more` hint. Use the invoked executable name in that footer.
  Replace references to removed appendix headings in option descriptions with
  explicit topic commands; use the documented `too` alias in these shared strings:

  ```text
  --allow <KEY>=<VALUE>     Select allowed resources; see 'too help allow'
  --limit <KEY>=<VALUE>     Set run limits; see 'too help limit'
  --default <KEY>=<VALUE>   Set session defaults; see 'too help default'
  --compact-model <MODEL>   Set the compaction model; see 'too help model'
  ```

  Use `<KEY>=<VALUE>` consistently for generic assignment options: KEY names a
  supported setting and VALUE follows that key's grammar. Label their topic
  definition lists `Keys`. The `allow` topic explains resource-selection keys
  and query values; these meanings do not need separate RESOURCE/QUERY metavars.
  Keep placeholders uppercase and literal keys lowercase.
  This changes only display; `--allow tools=none` and other accepted inputs stay
  unchanged. Semantic operands such as `<MODEL>` and `[TOPIC]` keep their names.
  The `--default` row above is for Chat; runtime commands say `Set runtime
  defaults`. Supply caller-specific metadata through the existing Annotated
  declarations/factories without mutating shared parameters.

  Both Script and rerun `--model` descriptions point to `too help model`.
  Keep existing option visibility, ordering, other metavars, required markers, bare
  values, caller-owned sections, Examples, flow outlines, and epilogs.

- [ ] Relocate reference content into the matching topic. Each page contains
  option syntax, key/parameter rows, applicable value and repetition rules,
  and one or two short examples. Preserve these details:

  | Topic | Required content |
  | --- | --- |
  | `allow` | `models`, `tools`, `psyches`, `skills`, `services`, `prompts`; TQ queries, `all`, and `none`; union of repeated resource queries; sentinel mixing restrictions; existing policy restrictions still apply. |
  | `limit` | `agic_model_calls`, `agic_tool_calls`, `tokens`, `cost`, `time`; per-agic versus run-tree accounting; USD and seconds; non-negative integers, decimal cost, `none`, and duplicate-key rejection. |
  | `default` | `model` and Chat-only `runnable`; setting lifetime and supported commands as below; `runnable=none` returns to automatic Chat entry resolution; each key once; quoting; a cross-reference to `too help model` for model value syntax. |
  | `model` | `[ref=]MODEL`; `effort` as a level, non-negative token budget, or `auto`; `max_output` as a positive integer or `auto`; parameter-only overrides; `default`/`unset` with caller-specific restrictions; quoting; exact-model requirement of `--compact-model` for a new runtime. |

  Verify content against `cli/common/policy.py`, `base/model_settings.py`,
  [Queries](../design/queries.md), and
  [Model runtime configuration](../design/models.md#runtime-configuration).
  Model help describes syntax, not installed models or provider capabilities;
  direct users to `too models` for inventory. Do not duplicate model rules in
  the `default` topic or introduce a TQ language manual in `allow`.

  Retain `--default` as the canonical CLI option: configuration migration removed
  the `[default]` table, while CLI defaults still apply to a runtime or session
  containing multiple runs. The `default` topic documents CLI overrides and
  presents this applicability table, rather than implying a config namespace:

  | Context | Option and keys | Lifetime |
  | --- | --- | --- |
  | Runtime startup: `serve`, `start`, internal `_serve` | `--default model=BODY` | Runtime instance; existing-server compatibility checks still apply. |
  | Chat | `--default model=BODY`, `--default runnable=REF` | Current Chat session; does not reconfigure the runtime or other sessions. |
  | Script and rerun | `--model BODY`; no `--default` | One run; use `too help model` for syntax. |

  Configuration uses structured `[model]` tables; legacy `[default]` tables are
  ignored. `TOOLANG_DEFAULT_MODEL` remains supported. Global
  `TOOLANG_DEFAULT_RUNNABLE` and runtime `--default runnable=REF` are rejected;
  configured driver handlers own runtime entry selection. State these boundaries
  briefly without adding a config-help topic, restoring removed options, or
  renaming internal `RunDefaults`/override structures in this presentation change.

- [ ] Remove automatic field appendices from `CliCommand` and `CliGroup` while
  preserving their section extension points. Keep the small, fixed topic
  directory and reference writers in CLI-owned code, using the existing
  `HelpFormatter` for headings, definition lists, wrapping, and examples.
  Give only topic leaves a CLI-owned `format_help` override for reference output;
  native subcommand parsing, eager help callbacks, and error usage stay intact.
  Reuse one style hierarchy: all section headings use `cli.heading`, topic and
  example commands use `cli.command.name`, and key/parameter labels use
  `cli.argument.name`. Definitions are bold cyan in UV and bold in PLAIN;
  explanations and important rules use normal foreground, not dim text.
  Pass authored `Text` spans from CLI writers; add no per-section colors,
  automatic prose highlighter, or renderer-specific field knowledge. Plain text
  retains alignment and spacing. Topic examples use explanations above full-width
  commands, with authored shell continuations where needed to remain copyable
  at 44 columns; existing command-example layouts remain unchanged.
  Topic output must not read stdin, fetch sources, inspect agents, load model
  catalogs or project configuration, start services, or create runtime files.
  Preserve lazy imports; shared formatting must not acquire policy/runtime
  dependencies. No generic help framework or new dependency is needed.

- [ ] Preserve existing command recognition for the new reserved name `help`.
  It accepts no AGENT operand; prefixed agent/file forms receive the existing
  unsupported-prefix diagnostic. A resident named `help` remains selectable as
  `agent:help`; a script runnable named `help` remains selectable through
  `too run FILE help` or a typed runnable selector. Keep `too more` unchanged.
  See [Command recognition](../design/cli-command-recognition.md).

- [ ] Implement in the following likely touchpoints after approval:
  `src/toolang/cli/toolang/main.py` for registration/footer;
  new `src/toolang/cli/toolang/commands/help.py` for the group and topic commands;
  `src/toolang/cli/common/{help,help_sections,parameters}.py` for reference
  extraction and pointers; `src/toolang/cli/toolang/commands/chat/__init__.py`
  and `commands/runtime.py` for default-option metadata; and
  `src/toolang/cli/toolang/commands/{script,thread}.py` for local model descriptions.
  Replace `help_sections.py` with a CLI-owned
  `help_topics.py` if the former has no remaining callers. Leave the shared
  renderer and routing algorithm unchanged. Synchronize `docs/design/cli.md`
  and `docs/design/scripts.md` with the implemented help contract.

- [ ] Add offline acceptance coverage in
  `tests/unit/cli/{test_cli_help,test_cli_routing,test_script_command,test_script_entry}.py`,
  `tests/integration/cli/test_runtime_commands.py`, and
  `tests/system/cli/test_cli_entry_points.py`:

  - Topic directory, all four topic pages, both executable names, unknown topics,
    extra operands/options, and native help aliases produce the specified
    output/status. Topic-specific `--help` stays on the selected topic.
    Successful topic pages contain target-option syntax and no helper-command
    Usage/Options boilerplate; directory help and native error usage are retained.
  - Chat, serve/start/internal runtime, rerun, Script file, and runnable help
    retain their options and relevant topic pointers, with no old appendices
    or dangling heading references. Assignment options consistently show
    `<KEY>=<VALUE>`; Chat/runtime default descriptions name the correct
    lifetime. Unrelated custom sections stay intact.
  - Topic keys match parser field vocabulary; rules and examples cover repeated
    queries, sentinels, duplicates, units, Chat-only defaults, model quoting,
    and compaction restrictions without changing parser behavior.
    Check the default topic's applicability table and preserve existing coverage
    for ignored legacy config, retained model environment overrides, rejected
    global runnable settings, and rejection of options on the wrong surface.
  - Missing configuration/credentials and a nonexistent root do not prevent
    topic output or create files. Guard against stdin, network, source loading,
    catalog initialization, and execution imports in subprocess probes.
  - The reserved-name cases select the intended command, agent, or runnable.
  - At 44/80/120 columns in PLAIN/UV themes and monochrome/color output, text
    remains complete, wraps within width, and uses shared heading styles.
    Key labels and explanations have distinct emphasis; prose stays readable.
    Wrapped example commands retain valid shell argument boundaries. Assert
    content/layout/style properties rather than full-output snapshots.

- [ ] Validate links and examples, generate the user-visible changelog update
  through `too aid.too update_changelog`, and run `git diff --check` plus the
  required implementation checks: `uv run ruff check .`,
  `uv run ruff format --check .`, `uv run ty check`, and `uv run pytest -n auto`.
  Maintainer review verifies the changelog entry. For this definition-only
  change, verify the current implementation and plan links, then run
  `git diff --check`; no product tests or changelog change are required.

## Open questions

None. Risks are missed discovery links, reference/parser drift, accidental
runtime imports, and the new reserved command name. Shared topic writers and
the acceptance cases above cover them. Human confirmation remains pending.
