# Define Concise Chat Resource Tables and Model Status

## Status

Pending human confirmation. Definition issue: #438.

## Goal

Make `/models`, `/caps`, and `/tools` useful as compact terminal discovery
commands, and make the status bar unambiguous about the session model and its
reasoning setting. Resource results should expose only the fields needed to
choose an item, align within that submitted result, and remain readable without
wrapping when identities or descriptions are long.

## Success Criteria

- Resource tables use neutral headers followed by a visible separator.
- Column widths are derived independently for each submitted result; there is
  no global identity-column width.
- Every header and data row occupies exactly one terminal line. Long cells use
  an ellipsis instead of wrapping.
- `/models` shows model, input/output price, and reasoning effort, with the
  default model marked by a trailing `*`.
- `/caps` shows cap, scope, form, and a useful description fallback.
- `/tools` shows only tool and description and hides toolsets whose names start
  with `_`.
- The status bar compactly distinguishes an absent model, automatic effort,
  and every explicit effort value without redundant field labels.
- Local and remote Chat produce the same rows from the same effective resource
  snapshots.

## Scope

In scope:

- the three submitted Chat slash-command result tables;
- shared terminal table measurement, truncation, and rendering;
- additive model, tool, and cap list metadata needed by remote Chat;
- local/remote parity, API documentation, Chat documentation, and offline
  regression tests;
- the right-side model and effort segment of the Chat status bar.

Out of scope:

- popup or completion UI;
- query syntax, matching, ordering, or resource allow semantics;
- `too models` and other non-Chat tables;
- model price calculation or accounting changes;
- adding, removing, or renaming resources;
- generic styling changes to non-table slash results or other status fields.

## Shared Table Contract

A table remains structured through presentation. The TUI must not flatten it
to space-delimited text and then infer columns again. Headers, rows, alignment,
and column behavior are passed to one table renderer.

The renderer uses these rules:

1. Prefix every table line with the existing two-space scrollback indent.
2. Render headers in normal text with no header-specific or column-specific
   color. Render a neutral separator immediately below the header, using one
   run of `─` per displayed column and the same two-space column gaps.
3. Measure display-cell width, not Python string length. Preferred width is the
   widest header or cell in this result only.
4. If the preferred table fits the current output width, use those widths. Do
   not impose the existing fixed identity or badge widths.
5. If it does not fit, shrink flexible text columns in command-specific
   priority order and replace removed display cells with one `…`. Continue to
   preserve two spaces between columns. On exceptionally narrow terminals,
   every column may shrink, but the renderer never wraps a header or row.
6. Normalize embedded whitespace in display descriptions to a single space.
   Every header and data item is one physical line.
7. Empty optional cells display `-`. Styling may improve scanability but must
   not encode default state or another value absent from the text.

The summary continues to precede the table as `Found N models`, `Found N caps`,
or `Found N tools`. `N` is the number of displayed rows. An empty displayed
collection keeps the existing `No ... found` result and emits no table.

## Models

`/models [QUERY]` has exactly three columns:

```text
  MODEL                          PRICE ($/1M)      EFFORT
  ─────────────────────────────  ────────────────  ──────────────────
  openai/gpt-5*                  $ 1.25 / $10.00   low, medium, high
  deepseek/deepseek-v4-flash     $ 0.30 / $ 0.88   -
```

`MODEL` is the canonical selectable ref. Append `*` directly to the configured
session default ref when that ref is present in the filtered result. Do not add
a separate state column and do not mark the current session selection. If a
marked model must be truncated, truncate the ref before the suffix so `*`
remains visible.

`PRICE ($/1M)` is base input/output token price in USD per one million tokens,
formatted as `$input / $output`. Each numeric component always has two digits
after the decimal point and reserves at least two positions before it. Within
one result, input components and output components are independently
right-aligned so every `/` occupies the same display position. Values that need
more than two integer positions expand the price column for that result. A
missing component displays a right-aligned `-`; zero displays `$ 0.00`.

`EFFORT` lists advertised reasoning-effort levels in catalog order, separated
by comma and space. Models without advertised effort levels display `-`.
When width is constrained, shrink effort first and model second; preserve the
aligned price column until the terminal is too narrow for the other columns'
minimum display.

The model list payload gains input and output price values in USD per million
tokens and an `effort`-applicability value that covers either an advertised
effort-level control or token-budget control. Local and HTTP projections derive
them from the same immutable `ModelEntry.info` used for selection; Chat does
not reload the catalog or infer prices or controls from a provider name.

## Status Model and Effort

The status bar's right side is one compact, right-aligned positional segment:

```text
openai/gpt-5 · high
openai/gpt-5 · 4096
openai/gpt-5 · auto
basic/text-model
[model not set]
```

Do not prefix values with `model` or `effort`. Their fixed positions make the
meaning clear while preserving horizontal space. The first value is the
canonical session model ref. If there is no selected model, the complete
right-side segment is the placeholder `[model not set]`. This placeholder is
not used for a configured default: when a default model exists, its concrete
ref is the effective session model and is displayed normally.

When a model is selected, append ` · VALUE` only when effort applies. `VALUE`
is:

- the canonical explicit effort level when one is set, including the valid
  level `none`;
- the explicit token budget as an ungrouped integer when one is set;
- `auto` when the selected model advertises an effort-level or token-budget
  control but the session has no explicit reasoning value.

If the selected model advertises neither control, omit the effort segment. If
control metadata cannot be resolved, also omit it rather than guessing support
or adding an `unknown` placeholder. These shapes remain unambiguous:
`[model not set]` means no effective model, while `MODEL · none` means a
selected model with the explicit `none` effort level. `MODEL · auto` means
effort applies but no explicit effort is set.

A toggle-only reasoning option does not make the input-level `effort` setting
applicable. `effort=auto` clears the explicit reasoning value and therefore
returns to `auto`, not to a blank field. The status bar continues to describe
session defaults only; an active run or child model call never replaces these
values.

Applicability comes from an exact lookup of the selected model in the immutable
model collection. Chat may cache that model's list metadata and reuse metadata
already obtained by `/model` or `/models`; it must not enumerate the full model
collection merely to render status. Failure to obtain display metadata does not
block Chat startup or a setting update and leaves the effort segment absent.

The complete segment remains right-aligned. At constrained widths, truncate the
model ref first while retaining an applicable effort suffix. Status values use
normal terminal styling and do not encode state through color.

This extends the earlier bare-model status rule without restoring redundant
labels. The active/default runnable rules and right alignment remain unchanged.

## Caps

`/caps [QUERY]` has exactly four columns:

```text
  CAP             SCOPE  FORM        DESCRIPTION
  ──────────────  ─────  ──────────  ───────────────────────────────
  skill/review    home   authored    Review code changes carefully.
  prompt/commit   root   configured  Create a semantic commit message…
```

`CAP` is `kind/name`. `SCOPE` and `FORM` use the effective State values.
`DESCRIPTION` selects the first non-blank value in this order:

1. `title` metadata;
2. `description` metadata;
3. the first non-empty body paragraph from capability content, excluding
   frontmatter.

The content fallback is normalized to one line and bounded to 256 Unicode code
points before transport. The terminal renderer may truncate it further for the
available width. If every source is absent, display `-`. This fallback is a
display summary only: collection queries continue to match the existing public
`description` field and do not silently treat title or content as description.

The list protocol exposes this bounded display summary and `form`; remote Chat
must not fetch every cap detail or transfer complete content to build the
table. When width is constrained, shrink description first and cap second;
preserve scope and form when possible.

## Tools

`/tools [QUERY]` has exactly two columns:

```text
  TOOL                    DESCRIPTION
  ──────────────────────  ─────────────────────────────────────────
  filesystem/read_file    Read a file from the workspace.
  web/search              Search public web pages.
```

Remove the plugin column. `TOOL` is the canonical `toolset/name` ref and
`DESCRIPTION` is the published tool description or `-`.

After applying the submitted query, hide every item whose structured toolset
name starts with `_`. This is presentation filtering for `/tools` only:

- hidden tools do not contribute to `Found N tools`;
- a result containing only hidden tools becomes `No tools found`;
- `/allow tools=...`, execution resources, and the public query collection are
  unchanged;
- a tool name beginning with `_` remains visible when its toolset itself is
  public.

The list payload exposes the structured toolset name so remote Chat does not
infer it by splitting display text. When width is constrained, shrink
description first and tool second.

## Design Touchpoints

Likely implementation files:

- `src/toolang/cli/toolang/commands/chat/slashes.py`: command-specific columns,
  summaries, default marker, price formatting, and private-toolset filtering;
- `src/toolang/cli/toolang/commands/chat/blocks.py`: structured one-line table
  rendering, neutral header, separator, width allocation, and ellipsis;
- `src/toolang/cli/toolang/commands/chat/tui.py` and scripted Chat output:
  preserve `SlashTable` structure and provide the current output width;
- `src/toolang/cli/toolang/commands/chat/tui.py` and `widgets.py`: resolve and
  render the compact session model/effort status segment without projecting
  active-run model calls;
- `src/toolang/cli/toolang/commands/chat/local.py` and `remote.py`: aligned
  model price, cap summary/form, and toolset payload validation;
- `src/toolang/api/routers/agent.py`, `src/toolang/api/routers/caps.py`, and
  caller-facing cap schemas/helpers: additive remote list metadata;
- `docs/chat.md`, `docs/api.md`, and `docs/execution-presentation.md`: visible
  columns, marker, price unit, fallback, filtering, status values, and additive
  response fields.

Keep table layout owned by Chat presentation. Do not change the collection
query table renderer or introduce fixed cross-command column widths.

## Acceptance Tests

- Headers render in normal text and are followed by column-width-matched
  separator segments.
- Two separate tables with different identities choose different first-column
  widths; neither uses a fixed 40-cell identity column.
- At narrow widths, headers and every row remain one physical line and long
  model, cap, tool, effort, and description cells end in `…`.
- A truncated default model retains its trailing `*`.
- Status shows explicit level and budget values and `auto` for an applicable
  but unset effort, without `model` or `effort` labels.
- `[model not set]` is visually distinct from `MODEL · none`; a model without
  effort support or resolvable control metadata has no effort suffix, and
  clearing with `effort=auto` updates an applicable model back to
  `MODEL · auto`.
- Status performs at most one exact selected-model metadata lookup when its
  cache is empty, reuses model command/list results, stays session-default-only
  during runs, and retains the effort field when width forces model truncation.
- Model rows contain only `MODEL`, `PRICE ($/1M)`, and `EFFORT`; price slashes
  align across one result, numeric rates have two fractional digits, and
  missing rates align as `-`.
- Cap rows contain `CAP`, `SCOPE`, `FORM`, and `DESCRIPTION`; title,
  description, content, and empty fallbacks are each covered.
- Tool rows contain only `TOOL` and `DESCRIPTION`; private toolsets are absent
  even when queried, while a public toolset's `_name` tool remains visible.
- Hidden tools do not alter `/allow tools=...` results.
- Local Chat, remote Chat, and HTTP payload tests cover the new metadata and
  produce equivalent visible rows.
- Existing no-match, slash-result scrollback, query forwarding, and session
  setting tests remain green.

## Risks

- Rich text wrapping can reappear if a rendered cell is passed as ordinary
  wrapping `Text`; tests must render at explicit narrow widths.
- Reading cap content per row can make listing expensive. Compute only the
  bounded fallback when title and description are absent, and return it in the
  list response rather than issuing per-cap detail requests.
- Filtering private toolsets too early would change allow counts and resource
  semantics. Keep it in `/tools` presentation after query selection.
- Price units can be confused with per-token accounting values. Name and
  document the list-protocol unit explicitly and derive it once at the local
  and HTTP projection boundary.
- Resolving effort applicability by listing all models would add startup cost
  and couple status to collection size. Use an exact selected-model query and
  treat metadata failure like unavailable effort for presentation.

## Open Questions

None. The examples and rules above are the implementation contract.
