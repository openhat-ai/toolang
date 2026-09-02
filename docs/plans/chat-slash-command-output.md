# Refine Chat Slash Command Output

## Status

Approved in the 2026-09-02 design discussion. This definition extends the
implemented Chat slash-command and resource-table plans. It is authoritative
where help structure, resource collection scope, command names, width, or
spacing differs from those plans.

## Goal

Make slash-command output concise, instructional, and visually scannable. The
main help should act as the entry point, focused help should show users how to
complete a command, and inspection results should state whether they describe
the session-allowed collection or every available resource.

## Success Criteria

- `/help` is a compact, aligned index grouped into session, inspection, and
  other commands, with aliases visually subordinate to canonical commands.
- Bare commands with required input show focused usage, purpose, required
  fields when necessary, and examples instead of a one-line usage warning.
- `/models`, `/tools`, and `/caps` inspect session-allowed resources by default;
  `-a` inspects all available resources and adds an `ALLOWED` column.
- Resource summaries name the displayed collection and its denominator.
- `/agics` and `/flows` list all available runnables of their kind without a
  query option.
- The current model, agic, or flow is marked with a protected ` *` suffix.
- `/output [RUN]` shows the given or latest run output; `/show` remains an alias.
- Every slash result honors the configured execution-output maximum width,
  distinguishes structural text with styling, and has exactly one trailing
  scrollback separation line.
- Local, remote, and scripted Chat have equivalent text semantics.

## Scope

In scope:

- slash registry metadata, main help, and focused help;
- `/models [-a] [QUERY]`, `/tools [-a] [QUERY]`, and
  `/caps [-a] [QUERY]` collection selection and summaries;
- `/agics`, `/flows`, and `/output`;
- slash text, table, and run-output rendering width, styling, and spacing;
- Chat documentation plus offline regression tests.

Out of scope:

- queue panel behavior or presentation;
- Enter, Meta-Enter, queue-selection, or editing shortcuts;
- `/keys` content;
- removing or changing the existing `/queue` and `/steer` commands;
- collection query syntax or public HTTP resource-list behavior.

## Command Help

The registry separately owns the canonical name, argument form, description,
aliases, category, visibility in main help, and optional focused help. Aliases
remain dispatchable but are never presented as peer commands.

`/help` has no introductory prose. All rows share one command-column width and
one argument-column width across the complete result:

```text
Session commands:

  /model      [MODEL] [effort=VALUE]  Set the session model or effort
  /runnable   RUNNABLE                Switch the session runnable
  /agic       AGIC                    Switch the session agic
  /flow       FLOW                    Switch the session flow
  /allow      FIELD=QUERY...          Set session resource ceilings
  /limit      FIELD=VALUE...          Set session run limits

Inspection commands:

  /models     [-a] [QUERY]            List allowed models (-a: all available)
  /tools      [-a] [QUERY]            List allowed tools (-a: all available)
  /caps       [-a] [QUERY]            List allowed capabilities (-a: all available)
  /agics                              List available agics
  /flows                              List available flows
  /output     [RUN]                   Show output from the given or latest run (alias: /show)

Other commands:

  /help                               Show this help (alias: /?)
  /exit                               Exit Chat (alias: /quit)
  /keys                               Show keyboard shortcuts

To list one-run colon directives, type :?.
```

`/queue` and `/steer` stay registered but are omitted until the queue workflow
is redesigned. `/keys` continues to return its existing content.

A required command invoked without its body returns focused help. Focused help
contains only aligned usage, one purpose sentence, necessary field names, and
examples. `/runnable`, `/agic`, and `/flow` share one focused result:

```text
/runnable RUNNABLE
/agic     AGIC
/flow     FLOW

Switch the session runnable

Examples:
  /runnable flow:review
  /runnable agic:chat
  /runnable default
  /agic chat
  /flow review
```

`/model`, `/allow`, and `/limit` use the same shape with their own examples.
The unchanged `/steer` command may retain its existing one-line usage result.

## Inspection Collections

For models, tools, and capabilities, the default base is the collection
selected by the current `SessionSetting.allow` field. An explicit query is
evaluated against all available resources and intersected by stable identity
with that allowed base. It is never unioned with the session allow queries.

`-a` changes the base to every available resource. It always adds an `ALLOWED`
column with `yes` or `no`; an explicit query filters displayed rows but the
summary denominator remains the complete available collection. The summaries
are:

```text
12 models allowed.
5 models matched out of 12 allowed.
700 models available.
5 models matched out of 700 available.
```

The same grammar applies to tools and capabilities, with correct singular,
plural, and zero forms. Private toolsets remain hidden before row counts and
intersection. Capability allow fields are evaluated independently for psyche,
skill, service, and prompt, then combined in the established stable order.

`/agics` and `/flows` accept no arguments and list every available item in a
one-column table. The current session runnable is marked. The model, agic, and
flow identity suffix is exactly ` *`; truncation preserves the complete suffix.

## Output and Rendering

`/output [RUN]` uses the existing durable result lookup. With no argument it
loads the latest result in the current thread. Its divider says `RUN_ID output`.
`/show` remains a compatibility alias and produces the canonical behavior.

Slash control bars, prose, focused help, tables, and durable output use the
configured execution maximum width as well as the physical terminal width.
Prose wraps by display width. Table headers and rows remain one physical line
and elide cells using the existing table priorities.

Commands use cyan, argument forms and aliases use dim text, section headings
and labels such as `Examples:` are emphasized, and the table separator remains
dim. Success, usage, warning, and error text retains semantic wording so color
is not the only signal.

Slash content does not append its own trailing blank rows. The scrollback
writer adds exactly one separation row after every submitted slash result, so
a summary-only usage result has the same ending as other slash output.

## Design Touchpoints

Likely files:

- `src/toolang/cli/toolang/commands/chat/slashes.py`
- `src/toolang/cli/toolang/commands/chat/blocks.py`
- `src/toolang/cli/toolang/commands/chat/tui.py`
- `src/toolang/cli/toolang/commands/chat/main.py`
- `src/toolang/cli/toolang/commands/chat/presenter.py`
- `docs/input-syntax.md`
- `docs/chat.md`
- `docs/execution-presentation.md`
- Chat slash, TUI, local, remote, and scripted tests

## Acceptance Tests

- Main help exactly preserves group order, global column alignment, aliases,
  hidden queue commands, and the `:?` footer.
- Bare model, runnable-kind, allow, and limit commands return their focused help;
  existing queue and steer behavior is unchanged.
- Default resource inspection covers unrestricted, narrowed, empty, and
  query-matched allowed collections.
- `-a` covers unrestricted, narrowed, empty, and query-matched available
  collections and always displays correct `ALLOWED` values.
- User query and allow query results are intersected by stable identity for
  models, tools, and each capability kind.
- Agic and flow lists reject arguments, mark only the current item, and preserve
  ` *` at narrow widths.
- `/output`, its latest-run form, and `/show` return a divider ending in
  ` output`.
- TUI and scripted slash text fit configured narrow and wide maximums; narrow
  help retains every command description and alias, tables never wrap, Unicode
  display widths remain correct, and exact rendered output has one trailing
  separation row.
- The complete default verification suite passes offline.

## Risks

- Multiple resource snapshots can change between requests. Chat intersects the
  returned stable identities and renders one internally consistent result; it
  does not add a new server-side snapshot protocol in this scope.
- Very narrow help output may wrap descriptions below their aligned row. The
  canonical columns remain aligned whenever their preferred width fits.

## Open Questions

None for this implementation. Queue interaction and shortcut decisions are
reserved for the follow-up feature definition.
