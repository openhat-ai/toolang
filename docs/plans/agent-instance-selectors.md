# Implement agent addresses and unified CLI operands

Approved in #914. No implementation is included in that PR. The approved
contracts live in [agent addresses](../design/agent-addresses.md) and
[CLI command recognition](../design/cli-command-recognition.md); this plan records
implementation scope and verification only.

## Goal and scope

Make command parameters, help, home paths, active records, and copyable addresses
agree within one Toolang root. Implement the prefix grammar, leaf contracts,
`h/s` IDs and area layout, address/record binding, active discovery, and same-home
continuation. Preserve source-resource inspection and runtime ownership. No old
syntax/layout compatibility or hub backend format redesign is required.

Deliver in independently reviewable increments:

1. Unify native Typer agent operands, group/leaf binding, grammar, and descriptions.
2. Implement address identity/layout, active lookup/output, and bound execution.

Each implementation PR names its increment and completed acceptance scenarios;
subsequent increments follow the same target contract without temporary aliases.

Native grammar, leaf declarations, descriptions, instance identities/layout,
active lookup, and bound continuation are implemented. Instance selection uses
`AGENT` with no `--instance/-i` compatibility. `cd` retains source preparation,
and `top`/`chore` retain resident scope pending their expanded home views.

## Typical cases

Implemented interface; shell variables contain complete IDs. Assume valid source,
home, and record bindings; `alice` is initially unused.

| Command | Result |
| --- | --- |
| `too new alice`, then `too alice start` | Create/start the resident home. |
| `too gh:acme/agents/lex.too@main start` | Start its stable visiting home; the equivalent GitHub URL selects the same home. |
| `too "lex@$hotel_id" stop`, then `too "lex@$hotel_id" inspect threads` | Stop its runtime; retained history remains readable by path. |
| `too /work/aid.too chat` | Allocate a new roaming home. |
| `too /work/aid.too inspect threads` | Read the last-created matching home without creating one. |
| `too "/work/aid.too@$site_id" inspect threads` | Read the exact home offline, even without source contents. |
| `too "aid@$site_id" cancel "$run_id"` | Cancel the live Run without stopping its script. Short address lookup fails after registration is removed. |
| `too /work/aid.too chat -t "$thread_id"` | Bind the unique owning home, including an older one. |
| `too "/work/aid.too@$site_id" chat -t` | Continue the latest Thread in that exact home. |
| `too /work/aid.too models` | Inspect source configuration without changing last-created home selection. Decorated addresses are rejected. |
| `too alice workspace remove data` | Bind `alice`; native Typer treats `data` only as the workspace name. |
| `too /work/aid.too start` | Reject roaming hosting. `too info alice` also fails: agent scope is prefix-only. |
| `too agent:top info` | Inspect the resident agent named `top`. |
| `too --root /srv/toolang-dev "lex@$hotel_id" info` | Resolve exclusively in that Toolang root's area. |

## Touchpoints and acceptance

Implementation under `src/toolang`: `cli/common/{agent_argument,context,execution,help,lazy}.py`,
CLI routing/command factories, `common/{layout,instance_ids}.py`,
`up/{addresses,identity,source,private_paths,observation,workload,config}.py`,
Chat/thread continuation, `top` presentation/schemas, and source/workspace inspection.
Update current CLI/layout/hosting/execution docs with the implemented increment.

Acceptance scenarios and pass conditions:

- Native operands: required/optional prefix, group leaf refinement, source/home
  roles, state/driver limits, native conversion/callback count, and parameter source
  match one declaration. Cached command/parameter objects remain unchanged.
- Routing/help: command collisions, unknown commands, both Script entries, dynamic
  signatures, eager help, completion, and literal input boundaries follow the
  grammar without unrelated imports, acquisition, or placeholder argv rewriting.
- Identity/isolation: exact field widths/bounds and ordered Base32; canonical remote
  aliases agree; two Toolang roots sharing sources remain isolated. Wrong ownership
  or malformed IDs fail. Fixed/backward clocks and concurrent allocation preserve
  creation order without overwriting; restart does not change that order.
- Binding: address/record matches, ambiguous imports, explicit/bare Thread selection,
  live attachment, stopped recovery, and concurrent callers preserve one home/owner.
  Post-edit Chat failure preserves the committed edit.
- Discovery/control: active cleanup retains history; short roaming lookup fails
  offline while path lookup survives missing source contents. Copy commands quote
  paths and retain root scope. Roaming hosting rejects; Run controls preserve the
  executor lifetime; missing drivers and replaced owners fail.
- Source inspection: resource commands reject decorated addresses and keep their
  configuration view while running. Source inspection/workspace edits do not
  advance home selection; unavailable offline data is not presented as live state.
- Workspace scope: list/add/remove accept resident and project sources; visiting
  refs and URLs fail before acquisition or configuration changes.
- Help wording: built-in argument descriptions use sentence case and uppercase
  URL/ID, list accepted input forms, and keep placement/driver constraints in
  validation. Authored descriptions remain unchanged.

Definition checks: verify links, examples, field calculations, and `git diff --check`.
Implementation checks: default repository verification, acceptance tests, and a
verified changelog entry generated through `too aid.too update_changelog`.

Risks: Typer/Click adapter behavior is version-sensitive; missing required inputs
can prevent stopped-home startup; hash collisions fail rather than alias owners.
Generic URL spellings and moved source paths may have different identities.
