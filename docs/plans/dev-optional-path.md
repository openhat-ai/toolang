# Optional Development Wheel Path

Status: Approved for implementation on 2026-09-09; formatter metadata refinement
approved in the follow-up request on the same date.

## Goal and Success Criteria

Make `--dev [PATH]` a three-state option and shorten its help description.

| Invocation | CLI value | Behavior |
| --- | --- | --- |
| Omit `--dev` | `None` | Use existing package selection. |
| `--dev` | `Path(".")` | Select a local wheel under the invocation directory. |
| `--dev PATH` | `Path(PATH)` | Select a wheel from the supplied file or directory. |

Help shows `--dev [PATH]` with the shared description
`Use a local Toolang wheel` and generated metadata `[bare: .]`.

## Scope and Decisions

- Apply to every existing Toolang CLI declaration: `run`, `start`, `chat`,
  `retry`, `rerun`, and script root and runnable options.
- Reuse `toolang.common.typer.options` with the raw bare value `"."`, native
  path conversion, and an omitted default of `None`. Preserve Chat's independent
  `--thread` configuration. Do not discover wheels during parsing or help.
- A bare option occurs at end of arguments, before another option, or before
  `--`. A following non-option token is an explicit path. Support `--dev=PATH`;
  paths starting with `-` must use the attached form or a `./` prefix.
- Keep native path conversion for explicit empty values: `--dev=` and
  `--dev ""` produce `Path(".")`. Repeated occurrences use the last value.
- Preserve script option inheritance: a runnable's explicit `--dev`, including
  a bare occurrence, overrides the script root value. Before a runnable selector
  or positional input, use `--dev=.` or `--dev --` to avoid consuming it as PATH.
- `.` means the process working directory, not the script directory or agent
  home. Retain recursive Toolang wheel discovery, newest modification-time
  selection, absolute-path tie breaking, and existing missing-wheel errors.
- Retain host and attached-server rejection, omitted-option warnings, and
  existing launch behavior. Do not build wheels automatically or change the
  standalone Docker harness interface.

## Formatter Metadata

- Render independent tags in order: `[env: NAME=] [default: VALUE] [bare: VALUE]`,
  followed by any existing range and required annotations in separate tags.
- Keep native environment/default visibility and extraction behavior. Show only
  declared environment names, never their values. A `None` default stays hidden.
- Obtain bare help from the owning optional-value command or group through a
  side-effect-free accessor. Each option defines its raw value and help metadata
  together with `OptionalValue(bare_value=..., show_bare=...)`; keep raw-string
  declarations as shorthand. `show_bare=True` displays the raw value, `False`
  hides it, and a string supplies a display label. Empty strings display as `""`.
  The formatter does not special-case option names, paths, or selection markers.
- Label Chat's bare selection `latest thread`; this is help text only, not a
  new accepted thread keyword. Keep its existing selection behavior and guidance.
- Preserve standalone use of both Typer extensions: the formatter does not import
  the parser extension or any Toolang runtime module. Help does not convert bare
  values, access paths/history, or call default factories.

## Design Touchpoints and Likely Files

Use one optional-value declaration for both parsing and help. Script runnables
compose the shared parser with their input-boundary handling. Preserve native
conversion, parameter sources, script inheritance, help layout, and lazy routing.

- `src/toolang/common/typer/options.py`: shared parser composition and declarative
  bare-help metadata; avoid a second optional-value parser.
- `src/toolang/common/typer/ui.py`: format independent metadata tags.
- `src/toolang/cli/toolang/main.py`: configure existing lazy command classes.
- `src/toolang/cli/toolang/commands/{runtime,thread,script}.py` and
  `src/toolang/cli/toolang/commands/chat/__init__.py`: option declarations and
  script parser integration.
- `src/toolang/cli/common/parameters.py`: shorten the shared description.
- `tests/unit/common/typer/test_options.py`, `test_help_metadata.py`,
  `test_standalone.py`, the CLI command/help/routing tests,
  and `tests/integration/cli/test_runtime_commands.py`: acceptance coverage.
- `docs/api.md` and `docs/chat.md`: document optional PATH and the bare form.

## Acceptance Tests

1. Exercise omission, bare selection, and explicit paths through every affected
   CLI entry point. Assert the orchestration boundary receives `None`, `Path(".")`,
   or the explicit path, including both script option positions and inheritance.
2. Cover attached/separated paths, spaces, option boundaries, `--`, repeated
   options, explicit empty paths, and a path beginning with `-`. Preserve native
   handling of unrelated options and runnable inputs. Cover Chat using bare
   `--thread` and bare `--dev` together.
3. With a controlled working directory and fake guest launch, bare `--dev`
   selects the same wheel as `--dev .`, including nested wheel discovery. A
   directory without wheels reports the existing error. Help starts no runtime.
4. Host and attached-server calls still reject development selection; omitted
   selection retains its existing behavior. All affected help views show
   `[PATH]`, the concise description, and `[bare: .]`, retaining option order.
   Test combined metadata order, visibility, literal labels, empty and sentinel
   bare values, command/group ownership, both themes, narrow wrapping, and
   rendering without conversion, environment reads, or default-factory calls.
5. Validate documentation examples and run the offline default verification:
   `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`,
   and `uv run pytest`. Live-provider tests remain opt-in.

## Risks and Open Questions

- Optional values can consume a following positional token; the explicit
  `--dev=.` form and `--` make that boundary unambiguous.
- Parser composition must preserve the existing private Typer API integration
  and script-specific input handling; boundary tests protect both.
- No unresolved design questions.
