# Hub empty roster presentation

Status: Approved for implementation on 2026-10-21 after the user explicitly
confirmed the dim tree leaf. This supersedes only the empty-roster presentation in
[Hub snapshot colors and alignment](hub-snapshot-presentation.md).

## Goal and scope

Visually attach the empty agent roster to its Hub in `too hub` (`too` aliases
`toolang`). Success means an empty snapshot uses the same tree grouping as a
populated snapshot, with a secondary notice and no count summary.

Change only empty-roster rendering and its acceptance coverage. Keep the Hub
row's columns, readiness/version semantics, nonempty rendering, discovery,
diagnostics, lifecycle commands, flags, and exit codes unchanged.

## Presentation

Keep the current Hub row and replace the standalone `no agents` line with a
single empty tree leaf immediately below it:

```text
hub                               running      v0.4.0a11-13-gc0e99626
└── no agents
```

An offline Hub uses the same empty leaf:

```text
hub                               not running
└── no agents
```

- Render the entire empty leaf in Rich `dim`, using the default foreground.
  Its text begins after the same four-cell prefix as real agent names. It is a
  roster notice, with no status or Talk placeholders, and is never counted.
- Reuse the renderer's existing terminal/encoding decision: `TERM=dumb` and
  ASCII-only streams use `\-- no agents`. Pipes and `NO_COLOR` retain the notice
  without ANSI controls. The leaf fits the existing effective minimum width of
  20 cells and does not participate in column sizing.
- Keep the empty leaf after all wrapped Hub rows. Add no blank line or zero-count
  summary. Existing `partial:` diagnostics follow it in their existing order and
  style, including when discovery fails.

The leaf preserves grouping without adding another field to the Hub row or
changing its spacing between empty and populated snapshots.

## Implementation touchpoints

- [Snapshot renderer](../../src/toolang/cli/toolang/commands/hub_status.py):
  update only the empty branch in `render_hub_snapshot`, using `_ascii_tree`.
- [Renderer tests](../../tests/unit/cli/test_hub_status.py): cover empty-leaf
  text/style/fallback and update the long-version test's Hub-row boundary.
- [CLI tests](../../tests/unit/cli/test_hub_commands.py): assert the empty leaf
  for ready and non-ready Hub fixtures, preserving existing request checks.
- Generate the user-visible Unreleased entry through
  `too aid.too update_changelog` during implementation; a maintainer verifies it
  before merge.

## Acceptance scenarios

| Scenario | Pass condition |
| --- | --- |
| Empty, ready or non-ready Hub | Existing Hub fields precede exactly one empty leaf; no blank separator, agent placeholders, or count summary appears. Missing versions still show `unknown`. |
| Terminal styles and fallback | A color-capable terminal renders the whole notice in default-foreground dim; pipes and `NO_COLOR` contain no ANSI controls; dumb/ASCII streams use the ASCII branch. |
| Narrow terminal and long version | At widths 20, 32, 40, 80, and 120, complete Hub values survive wrapping and the leaf follows the last Hub row within the effective width. |
| Partial discovery or Hub inspection | The leaf remains visible and all existing diagnostics follow it in the same order, without losing the partial indication. |
| Nonempty roster | Existing names, branches, columns, styles, ordering, counts, and diagnostics remain unchanged; the empty leaf is absent. |

Use offline fixtures and fake transports. Run focused renderer/CLI tests and
the repository's default verification before implementation commits.

## Risks and open questions

Consumers matching the exact standalone `no agents` line must adjust. Tree
grouping remains visual and does not imply agent connectivity. Terminal palettes
control the appearance of dim text; the notice remains explicit in plain output.

Open questions: none.
