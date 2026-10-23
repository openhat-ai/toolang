# Define Root and Agent Environments

Status: Approved in #937; implementation covers the environment rules below.
Nonresident root-file lookup remains deferred with the reserved path/ref/URL codec.
Work type: feature implementation.

## Goal and Scope

Limit inherited environments without requiring configuration before using a
common model provider. Keep variable names in TOML, secret values in dotenv
files, and durable agent-specific policy under the selected Toolang root.

Cover root commands, agent commands, resident/visiting/roaming execution,
foreground/background AgentServers, executors, guest workloads, and `cd` shells.
Tools and adapters share the resulting agent environment; this feature adds no
separate permissions for them. Keychain support, credential brokers, plugin
process isolation, and a new environment-management CLI are outside this scope.

Success means every covered boundary receives the same resolved environment,
unselected shell variables cannot reappear through Toolang's loaders, and common
provider credentials work automatically when no explicit agent file exists.

## Behavior Before Implementation

- [CLI context](../../src/toolang/cli/common/context.py) and
  [Setup configuration](../../src/toolang/setup/config.py) independently merge
  place/home dotenv values with the full process environment. Bare dotenv names
  are discarded; process values override assignments.
- [Shell execution](../../src/toolang/plugin/toolsets/shell.py) and
  [`cd`](../../src/toolang/cli/toolang/commands/cd.py) launch subprocesses without
  an explicit environment. Service tools temporarily modify the process
  environment in [service.py](../../src/toolang/plugin/toolsets/service.py).
- [Docker](../../src/toolang/plugin/sandboxes/docker/sandbox.py) passes all
  authored dotenv names and separately filters process names with its own
  pattern. [Guest bootstrap](../../src/toolang/plugin/sandboxes/docker/docker_guest.py)
  merges the staged dotenv with the image environment.
- [Agent addresses](../design/agent-addresses.md) already separate the selected
  Toolang root, placement, stable source, home, and runtime generation.

## Root Configuration

`ROOT` is the selected Toolang root, never a visiting/roaming `PLACE`. Only
`ROOT/config.toml` accepts the environment configuration below. These are the
proposed built-in defaults:

```toml
[env]
keep = [
  "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG",
  "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "TMP", "TEMP",
  "TERM", "COLORTERM", "NO_COLOR",
]
pick = [
  "OPENAI_API_KEY",
  "ANTHROPIC_API_KEY",
  "OPENROUTER_API_KEY",
  "GOOGLE_API_KEY",
  "GOOGLE_GENERATIVE_AI_API_KEY",
  "GEMINI_API_KEY",
  "DEEPSEEK_API_KEY",
  "MISTRAL_API_KEY",
  "GROQ_API_KEY",
  "XAI_API_KEY",
]
```

Each present list replaces its built-in default; an omitted list retains its
default, and `[]` clears it. Names are exact, case-sensitive environment names,
not patterns. Reject invalid names, duplicate names within a list, wrong types,
unknown `[env]` fields, and `[env]` in home/project TOML.

TOML must never carry secret values. Environment configuration accepts names
only, not assignments, literal values, or inline dotenv. Retain the existing
[plugin contract](../design/plugins.md) of using secret references rather than
values. Do not copy dotenv content or resolved values into generated TOML.

## Environment Files

Use the existing dotenv parser without interpolation, with these binding rules:

| Binding | Meaning |
| --- | --- |
| `NAME` | Inherit from the supplied parent environment; omit if absent. |
| `NAME=VALUE` | Assign the literal value, overriding an inherited value. |
| `NAME=` | Assign an empty string; this does not remove the name. |

Support ordinary dotenv comments and quoting. Reject malformed bindings and
duplicate names with file/line diagnostics that do not echo values. An unreadable
or invalid file fails resolution instead of falling back to another file.

Recommend central storage of secret values in `ROOT/.env`. Agent-specific files
normally contain bare names, so they select centrally stored values without
duplicating them. Assignments remain supported in all environment files,
including `HOME/.env` and project-local files. Keep these files out of Git; any
future Toolang-created secret-bearing files must use mode `0600`.

## Resolution

Resolve the selected root and stable agent identity before applying environment
policy. Capture the caller's shell environment once; do not rediscover it inside
core execution.

Let `keep(E)` select configured `keep` names present in environment `E`, and let
`apply(F, E)` resolve file `F` using `E` for bare names. `overlay(A, B)` combines
maps with values from `B` winning on collisions.

### Root environment

- Without `ROOT/.env`, the root environment is the complete captured shell
  environment.
- With `ROOT/.env`, it is
  `overlay(keep(shell), apply(ROOT/.env, shell))`.

Root-scoped commands use this result. Thus adding `ROOT/.env` can reduce root
exposure while providing central literal values. `pick` is not applied at root
scope.

### Agent environment

Compute the environment from the root result, never directly from the original
shell. Select the first existing agent file from this table:

| Placement | First choice | Local fallback |
| --- | --- | --- |
| Resident | `ROOT/.{encoded(name)}.env`, e.g. `.alice.env` | `HOME/.env` |
| Roaming | `ROOT/.{encoded(canonical_source_path)}.env` | `SOURCE_DIR/.toolang.env` |
| Visiting | `ROOT/.{encoded(canonical_requested_ref_or_url)}.env` | None |

Resident names are unchanged by encoding. Roaming uses the canonical absolute
`.too` path, not a generated home. Visiting uses the existing requested-source
identity; runtime instances, fetched content hashes, and mutable resolved commits
do not replace it. Nonresident sources never borrow same-name resident policy.

Files replace one another as complete rule sets; do not merge lower-priority
agent files. Do not load project `.env`, ancestor `.toolang.env`, or visiting
temporary-home dotenv files. Root policy survives home removal and is reused for
the same stable identity.

- With a selected file `F`, the agent environment is
  `overlay(keep(root), apply(F, root))`; skip `pick` entirely.
- Without one, it is `overlay(keep(root), first_available(root, pick))`.
  Select at most one name, using list order and a nonempty, non-whitespace value;
  preserve the original value. No available candidate means only `keep(root)`.

An empty agent file therefore preserves `keep` and disables `pick`. An explicit
assignment can override a kept value or introduce a supplied value, but a bare
name can never recover a shell value excluded from the root environment.

## Runtime Boundary

Resolve policy at CLI/hosting boundaries and pass concrete mappings into core
modules. Establish the resolved process environment before loading agent plugins;
passing a filtered adapter dictionary while leaving ambient `os.environ` intact
is insufficient. Root commands and temporary agent commands use their respective
environments, including inspection and `cd`.

Carry the final agent mapping through host/guest startup, model calls, tool
execution, and subprocess launch. Setup refreshes must retain this startup
snapshot instead of rereading raw dotenv files or recovering the controller's
environment. Environment changes take effect on restart; attachment to an
existing server retains that server's environment.

Guest staging must contain only resolved values, and bootstrap must not re-add
unselected controller or image variables to the agent workload. Docker's default
provider pattern must not independently veto a name admitted by this policy;
retain an explicitly configured legacy pattern only as an additional narrowing
filter. Preserve public sandbox factories and entry points.

Toolang may add a finite set of generated runtime facts, such as launch identity,
sandbox/workspace mappings, and guest bootstrap paths. Authored environment files
cannot override those facts; do not broadly exempt `TOOLANG_*`, `PYTHON*`, or
provider prefixes. Resolve controller-only startup inputs before filtering and
carry them as explicit settings where needed.

Report the selected policy file or automatic variable name without values.
Do not automatically serialize environment values into diagnostics, runtime
metadata, or ordinary logs.
This policy controls Toolang's initial environment propagation; it is not a
filesystem, memory, or OS sandbox. User code and shell startup files can load new
values, and an agent can read a credential explicitly admitted to its environment.

## Implementation Plan

- [x] Add a shared, explicit environment binding/resolution helper; parse the
  root-only lists there, and resolve root/layout inputs in
  `cli/common/context.py`.
- [x] Route agent startup, root/agent inspection, and `cd` through the resolver
  using existing CLI entry points and lifecycle ownership.
- [x] Thread the captured mapping through `up/sandbox.py`, `up/server.py`, and
  Setup configuration/watcher inputs; remove independent dotenv reload paths.
- [x] Align host/Docker startup, guest staging/bootstrap, and subprocess
  environments; preserve trusted generated controls without ambient widening.
- [x] Add the acceptance tests below and verify diagnostics expose provenance
  without values. Use existing CLI, Setup, sandbox, and toolset test modules.
- [x] Update the focused Setup, Hosting, Plugins, Scripts, and CLI design docs
  and migration guidance; generate the user-visible changelog through `aid.too`
  according to repository policy.
- [x] Run focused tests, then `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run ty check`, and `uv run pytest -n auto` before implementation commits.

## Acceptance Tests

| Scenario | Pass condition |
| --- | --- |
| No root file | Root commands retain the full supplied shell environment. |
| Root filtering | Root file plus `keep` removes an unrelated sentinel, supports literal overrides and empty values, and supplies central credentials. |
| Automatic agent | Only kept names and the first available `pick` name reach the agent; missing/blank candidates are skipped in configured order. |
| Explicit agent file | Kept names remain; bare names use root values; assignments win; `pick` and lower-priority files contribute nothing. |
| Empty explicit file | Only kept names remain; no fallback file or provider is selected. |
| Placement precedence | Root file wins over resident home/roaming local files; repeated source runs and same-name resident recreation reuse root policy. |
| Root authority | A bare agent binding cannot recover an excluded shell value, including through Setup refresh or guest bootstrap. |
| Configuration safety | Missing lists use defaults; empty lists disable them; invalid TOML/env bindings fail without leaking values. |
| Execution parity | Root/agent commands, foreground/background servers, `cd`, host/guest workloads, adapters, and tool subprocesses receive their expected mapping. |
| Runtime lifetime | Refresh and attachment retain the captured environment; restart applies changed policy. |
| Secret handling | Resolution/staging never copies a credential sentinel into generated TOML, diagnostics, provenance, or runtime metadata; this does not sanitize output deliberately produced by user code. |

## Risks and Deferred Encoding

Defaults intentionally admit one common provider credential to the whole agent,
including its tools. Selecting an unavailable provider or needing several
providers requires an explicit agent file or a changed `pick` order. Existing
deployments that rely on full inheritance, process-over-dotenv precedence, or
home dotenv hot reload need migration and restart.

**Reserved: path/ref/URL filename encoding.** Define the exact reversible,
readable codec separately before implementing nonresident root-file lookup.
Its contract must preserve resident names, operate on standardized stable
identities, and specify escaping, case/Unicode handling, filename limits, and
invalid/colliding names. This plan deliberately chooses no slug, hash, or other
encoding algorithm. Complete that section and obtain definition approval before
shipping the corresponding implementation.
