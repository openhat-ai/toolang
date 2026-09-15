# Define Unnamed Runnable Identity

## Work Type and Status

Feature implementation. Approved and implemented on 2026-09-15.

## Goal

Keep unnamed agics and flows unnamed in the AST. When a string is required,
identify them by optional module path, optional kind, role, and source line.

## Vocabulary

| Term | Meaning |
| --- | --- |
| **unnamed** | No authored identifier. AST `name` is `None`. `span.line` is the line. |
| **named** | Has an authored identifier. AST `name` is that identifier. |
| **entry** | Unnamed top-level agic or flow a surface can select. |
| **adhoc** | Unnamed agic in a flow-statement body. |

AST never stores `entry` or `adhoc`.

## Ref grammar

```text
ref        := [ module '::' ] runnable
module     := ident ( '::' ident )*
runnable   := [ kind ':' ] name
kind       := 'agic' | 'flow'
name       := ident | '<' role [ ':' line ] '>'
role       := 'entry' | 'adhoc'
line       := [1-9] [0-9]*
ident      := [A-Za-z_] [A-Za-z0-9_-]*
```

Parse in this order. Do not treat `::agic:` or `::flow:` as a special split.

1. If the string contains `::`, the last `::` separates module from
   runnable. Left of that `::` is the module; inner `::` is path structure
   (`flows::research`). If there is no `::`, there is no module.
2. The runnable is `[kind:]name`. Kind is present only when the first
   `:`-segment is exactly `agic` or `flow`. Otherwise the whole runnable is
   `name`.
3. `name` is an authored ident, `<entry>`, `<entry:N>`, `<adhoc>`, or
   `<adhoc:N>`.

A first `:` whose left side is not `agic` or `flow` is not a kind tag.
`agent:research` is a name, not module `agent`. Write `agent::research` or
`agent::agic:research` to qualify by module.

Legal:

```text
agic:<entry>
agic:<entry:3>
agic:chat
flow:<entry>
flow:<entry:1>
flow:research
chat
<entry>
<entry:3>
agent::agic:<entry:3>
agent::chat
agent::agic:chat
flows::research::flow:research
flows::research::agic:<adhoc:5>
agent::agic:<adhoc:5>
```

## Canonical vs selector

Lined unnamed refs are the only standard form. They are what records,
indexes, listings, and inspect persist and echo.

```text
agent::agic:<entry:3>
agic:<entry:3>
flows::research::agic:<adhoc:5>
```

Unlined `<entry>` / `agic:<entry>` is a selector alias only. It may resolve
when the module has exactly one unnamed top-level export. It is not a map
key, not a stored run identity, and not a listing name.

Unlined `<adhoc>` is not a selector. Adhoc always needs a line, and stored
adhoc always includes the module.

## Index

String map. Keys are lookup names. Values are `(module, decl)`. `decl.name`
may be `None`.

**Public map** (listings, Script/Chat selection, `hands`):

| Key | Hits |
| --- | --- |
| authored name (`chat`, `research`) | that decl |
| filename (`research`) | flow-module export (AST may be unnamed) |
| `<entry:3>` | unnamed top-level in the agent/Script module at line 3 |

No `<entry>` key. No adhoc keys. At most one unnamed top-level per
agent/Script module.

**Lookup**

1. Named ident → public key, exact.
2. `<entry>` / `agic:<entry>` → the unique public `<entry:N>` if exactly
   one exists; otherwise not found / ambiguous.
3. `<entry:N>` / `agic:<entry:N>` / `agent::agic:<entry:N>` → key
   `<entry:N>`, require `decl.span.line == N`. Kind and module must match
   when present.
4. `flow:research` / `research` → public key `research`.
5. `…::agic:<adhoc:N>` → **not public**. In that module's Program, the
   unnamed `AgicDecl` with `span.line == N`.

Static `run name` still keys only authored names. `hands`, `handoffs`, and
flow `run` cannot target unnamed decls. `<entry>` is a CLI/Chat selector, not
an authored runnable name.

`ResolvedRunnable.name` for an unnamed agent/Script entry is `<entry:3>`.
`.ref` is `agic:<entry:3>`. `.qualified` is `agent::agic:<entry:3>`.
`executable.name` stays `None`.

## Records

Store the module-qualified lined form for unnamed and adhoc; named runnables
store their unqualified public ref. No new column.

```text
agent::agic:<entry:3>
flows::research::agic:<adhoc:5>
agic:chat
flow:research
```

Never store `agic:<entry>` or `agic:<adhoc>` for unnamed or adhoc.

## Selection

- `too FILE` / Chat idle: unique unnamed entry; `agic:<entry>` may be typed
  as a selector.
- `agic:<entry:3>` is the exact / standard ref.
- Adhoc is not a Script/Chat selector.
- `run <entry>` / `run <adhoc:5>` invalid source.
- The runtime never generates a runnable. A surface resolves `chat`, then the
  unnamed entry, and otherwise reports a failure. `too agent new` writes an
  `agent.too` whose only declaration is the minimal unnamed `agic: {{_}}`, so a
  new agent always has an entry.

## Display

- Script help and Chat TUI: `agic:<entry>` / `agic:<adhoc>` (no module, no
  line).
- Progress: `agic:<entry:3>` / `agic:<adhoc:5>` (line, no module).
- Records persist the module-qualified lined form for unnamed and adhoc.
- Named runnables keep `agic:NAME` / `flow:NAME` everywhere.
- Adhoc stays off the public map; only its own Step shows it.

## Scope

In: ref parse; public/local index; module id `flows::research`; inline
lowering (`name=None`, statement/run stores lined qualified adhoc); run
strings; UI; the `too agent new` template; docs; tests.

Out: CST changes; RunStore migration; filename public names; unnamed as
`run` targets.

## Touchpoints

- `lang/types.py`, `lower.py`, `validate.py`, `description.py`
- `state/state.py` (index, flow module name), `prepare.py`, `cache.py`,
  `runnable_collections.py`
- `execution/runnables.py`, `calls.py`, child-run path
- Script, Chat, progress filters
- `docs/program.md`, `docs/flow-syntax.md`
- Tests: prepare, lowering, script entry, chat status, ref parse

## Acceptance Tests

1. Unnamed/adhoc AST: `name is None`, span line set.
2. `agic:<entry>` resolves as a selector; public key and stored ref are
   `agic:<entry:3>` / `agent::agic:<entry:3>`.
3. `agic:<entry:3>`, `agic:chat`, `flow:research` parse. Kind is only the
   `agic`/`flow` tag; `agent:research` is not a module prefix.
4. Two unnamed top-level decls in one module collide.
5. `too FILE` runs unnamed entry; adhoc not selectable.
6. Adhoc at line 5 in `flows/research.too` stores
   `flows::research::agic:<adhoc:5>`; not in the public map.
7. Same line in `agent.too` stores `agent::agic:<adhoc:5>`.
8. Unnamed `flows/research.too`: AST unnamed, public `flow:research`.
9. Default `ruff`, `ty`, `pytest` gate implementation.

## Risks

- `agent:research` (single `:`, left side not `agic`/`flow`) is not a
  module prefix. Use `agent::research`.
- Line identity is per captured Program.
