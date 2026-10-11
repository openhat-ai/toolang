# Prune obsolete commands and regroup CLI help

Status: approved in conversation, including implementation and a pull request
containing this definition.
Baseline: #767, merged as `dcc07b2e`.

## Goal and success criteria

Remove redundant chore cloning and the unused channel plugin surface. Group
commands by their current responsibilities without a Teaming panel. Removed
commands have no compatibility aliases. Creation uses one bundled default
template per kind, without a template selector.

## Decisions

- Remove `too AGENT chore clone` and its implementation. Retain
  `list/new/edit/delete/run/cancel`. Users create a new definition with `new` or
  copy a home file under a different name; use `run` to request another occurrence
  of an existing chore. A new name creates a new execution identity.
- Remove `too channel` and `channel list`, their registration and routing,
  the `toolang.channel` entry-point family, the bundled Telegram channel,
  `create_channel`, and channel-only protocols, value types, and helpers.
  Read and validate only supported plugin families. Ignore unconsumed families,
  including former `[plugin.channel.*]` configuration, with no channel-specific
  compatibility or rejection code.
  Remove their dedicated tests and update shared plugin tests. No replacement
  Telegram integration is introduced. Preserve Hub/Talk transport and its
  messaging checkpoints; their paths containing `channels` do not make them
  part of the removed plugin system.
- Remove `--template/-t` from `too new` and cap `new` commands, and remove
  the cap `template` subcommand from both `too` and `caps`. Creation uses the
  bundled default directly; cap creation opens its content in the editor.
  Keep one bundled default for each kind and remove `service.stdio.md`.
  Load defaults directly; remove template discovery, sorting, and name lookup.
  Keep HTTP template list responses as a one-entry list and the existing
  `/templates/default` detail paths as fixed routes. No named-template route
  remains. To author a stdio service, edit `transport` and `target` in the
  generated default; service transport support is unchanged.
- Remove the `Teaming Commands` help panel. Root help uses these panels in this
  exact order:

  | Panel | Command order |
  | --- | --- |
  | Agent Commands | `new clone remove list info home workspace` |
  | Cap Commands | `psyche skill service prompt` |
  | Runtime Commands | `serve start stop hub` |
  | Work Commands | `chat talk chore` |
  | Inspection Commands | `caps tools models providers inspect top` |
  | Script Commands | `init run` |

- Keep Hub discoverable in root help alongside runtime lifecycle commands.
  Keep `too hub` and its `start/serve/stop/status` subcommands callable with their
  existing syntax. Workspace configuration belongs under Agent; Work contains
  interactive work and chore definitions.
- In `too more`, rename `Runtime Commands` to `Plugin Commands`. Preserve its
  inventories and the other panels, in this order:

  | Panel | Command order |
  | --- | --- |
  | Run Commands | `steer cancel retry rerun` |
  | Thread Commands | `fork rewind` |
  | Plugin Commands | `catalogs adapters toolsets sandboxes` |
  | Language Commands | `parse highlight fmt` |

- Use these command descriptions consistently in root, target, and command help:

  | Command | Description |
  | --- | --- |
  | `chat` | Chat directly with an agent |
  | `talk` | Exchange messages through the Hub |
  | `chore` | Manage one-off and scheduled work |
  | `start` | Start an agent in the background |
  | `hub` | Manage the shared Hub service |
  | `top` | Monitor running agents and scripts |
  | `inspect` | Inspect run history and details |

  Preserve other descriptions, including `workspace`. Top's detailed help
  explains observation of runtime instances, threads, and runs. Chore subcommand
  help preserves the distinction between deleting a definition while retaining
  history and canceling one occurrence while retaining future scheduled work.
- Target-specific help uses the same placement for `top`, without exposing
  root-only `talk` or `hub` as agent-targeted commands. Other commands,
  options outside template selection, driver selection, and scheduling semantics
  stay unchanged.
- Update current user documentation and record breaking removals and migration
  guidance through `too aide.too update_changelog`. Preserve historical records
  and superseded plans; this definition supersedes their conflicting CLI scope.

## Implementation touchpoints

- `src/toolang/cli/toolang/{main,routing}.py` and
  `commands/{chore,plugin}.py`: registration, grouping, and removed handlers.
- `src/toolang/plugin/{config,loading}.py`, `plugin/channels/`,
  `src/toolang/base/{protocols/channel,types/channel,utils/channels}.py`, and
  `pyproject.toml`: remove the channel plugin contract and implementation.
  Refresh `uv.lock` if required by the package metadata change.
- `src/toolang/catalog/templates/`, CLI agent/cap/chore authoring, and
  `src/toolang/api/routers/caps.py`: single default loading and inspection.
- CLI help/routing and integration tests; channel and shared plugin tests.
- Current plugin/layout guidance, the reference index template, chore docs, and
  `CHANGELOG.md` where descriptions reference the affected behavior.

## Acceptance checks

1. Chore help lists exactly the six retained commands; cloning is rejected.
   Existing authoring and execution operations pass their regression tests.
2. `channel` is absent from command registration and routing; its former
   invocation fails with no matching agent. No channel entry point, factory,
   implementation, or channel-only contract remains. Unknown plugin-family
   configuration is ignored without validating its contents; supported families
   retain their normal validation.
3. Root help has the six panels, command order, and descriptions specified
   above, with no Teaming panel. `too more`, `too more -h`, and
   `too more --help` agree on the four advanced panels. `too hub --help` retains
   all four subcommands.
4. Agent-targeted help places lifecycle commands under Runtime, Workspace under
   Agent, and Top under Inspection; it omits Talk/Hub. Updated descriptions agree
   across help surfaces. Hub/Talk behavior and messaging persistence remain
   covered by existing tests.
5. Agent and cap creation succeed using defaults without template flags;
   `--template/-t` fail. Both CLI entry points omit cap `template` from help
   and reject it, with and without an agent target. Every kind loads its
   default directly.
   HTTP template lists contain only default, its detail remains readable,
   and non-default template URLs return 404.
6. Documentation examples match the resulting CLI. Run `git diff --check` and
   the default Ruff, formatting, Ty, and full offline pytest checks before
   implementation commits.

## Risks and open questions

Channel removal breaks external imports and entry-point integrations, in
addition to the removed CLI calls. Chore definition copying must use a new name
to create independent progress. Help panel and description changes affect
snapshot expectations. Existing creation invocations must omit template flags;
use the editor opened by cap `new` instead of the removed `template` command.
Service template API clients must use default and edit stdio fields.

No open design questions.
