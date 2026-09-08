# Root help version

Status: approved by the human on 2026-09-08.

## Goal and success criteria

Show the current Toolang version in the outermost `too --help` output so users
can identify their CLI build without running another command.

## Scope and design

- Add `toolang <version>` above `Run and manage Toolang agents.` in the root
  help description, using Typer's existing help layout.
- Resolve `<version>` through `toolang.common.version.toolang_version()`, the
  same source used by `too --version`; preserve its complete string, including
  source revision, dirty marker, or `unknown` fallback.
- Apply the same display to the `toolang` alias and help shown without arguments.
- Keep agent-targeted help, subcommand help, `caps` help, command panels, flags,
  routing, and exit codes unchanged. Do not add a separate help renderer.

Example description, with an illustrative version:

```text
toolang v0.3.0

Run and manage Toolang agents.
```

## Implementation touchpoints

- `src/toolang/cli/toolang/main.py`: populate the version in the root app's
  help description; keep `_run_target_help` descriptions unchanged.
- `tests/unit/cli/test_cli_help.py`: verify root, alias, no-argument, and nested
  help behavior with a deterministic version provider.
- Existing `tests/system/cli/test_cli_entry_points.py` checks protect installed
  entry points and lazy runtime imports.

## Acceptance and verification

1. Root help displays the version once above the existing description for both
   executable names; no-argument help includes it and retains its exit code.
2. Stubbed release, revision/dirty, and `unknown` versions are displayed intact
   and agree with `--version`.
3. Agent-targeted and subcommand help do not gain the version banner; existing
   options and command panels remain present.
4. Default Ruff, formatting, ty, and offline pytest checks pass.

## Risks and open questions

Long development versions can wrap in narrow terminals; use Typer's normal
wrapping. Avoid introducing runtime command imports during root help rendering.
No open design questions.
