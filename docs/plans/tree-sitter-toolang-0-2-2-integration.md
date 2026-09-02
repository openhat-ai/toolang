# Define tree-sitter-toolang 0.2.2 Integration

## Status

Approved for implementation on 2026-09-02. The human confirmed that old 0.2.1
authored syntax receives no compatibility layer and that complete `far`, `near`,
and `line` runtime semantics remain a follow-up. The same approval requires
strict kind-specific cap property and body validation.

## Goal

Adopt `tree-sitter-toolang` 0.2.2 and make every released grammar change
visible through `toolang.lang` lowering, validation, formatting, and public AST
behavior. The integration migrates repository-owned source to the new syntax
and leaves the language layer ready for later recall-local runtime work.

The user-facing summary is:

> Toolang accepts the 0.2.2 grammar, derives prompt inputs from placeholders,
> uses `=` for Content locals, and validates every cap against its own exact
> property and body contract.

## Success Criteria

- `pyproject.toml` requires `tree-sitter-toolang>=0.2.2` and `uv.lock` resolves
  0.2.2 while retaining the compatible Tree-sitter 0.25 runtime.
- Cap lowering consumes the 0.2.2 declaration-level `property` fields and
  optional `body` field without losing metadata or raising internal errors.
- Duplicate, unknown, missing, empty, mutually exclusive, and malformed cap
  properties produce kind-, name-, property-, and line-specific language
  diagnostics.
- Psyche, skill, service, and prompt bodies follow the exact per-kind contract
  defined below.
- Prompt parameters are derived from body placeholders; the removed `params`
  property is rejected rather than treated as prompt text or metadata.
- Direct Content locals use `let name = CONTENT`; `let name: CONTENT` is a
  syntax error with no compatibility alias.
- Recall syntax lowers only `auto`, `none`, `far`, `near`, or `far, near` and is
  valid only on Agics.
- `Pack` is no longer a builtin type and remains available as a user type name.
- Untyped named runnable parameters lower as `Text`; omitted runnable return
  types lower as `Part[]`.
- `far`, `near`, and `line` are reserved against authored parameter and Flow
  binding declarations, without implementing their runtime values here.
- Formatting is idempotent for every new form and never emits removed syntax.
- Repository-owned examples, fixtures, tests, and language documentation use
  only 0.2.2 syntax.
- The default offline verification suite passes.

## Verified Current Behavior

The repository currently declares `tree-sitter-toolang>=0.2.1`, and `uv.lock`
resolves 0.2.1. `toolang.lang.lower` expects cap properties inside `cap_body`,
requires every cap body, reads prompt parameters from a `params` property, and
supports the old `let name: CONTENT` CST.

The current semantic validator has a detailed service check, a prompt `params`
check, and no equivalent psyche or skill contract. Property lowering converts
directly to a dictionary, so duplicate keys are overwritten before validation.
Body requirements are therefore not consistently enforced by cap kind.

Against current `origin/main` at `e2ef9127`, running the unit language suite
with an ephemeral 0.2.2 dependency produces 10 failures and 121 passes. The
failures cover cap metadata loss, missing optional cap bodies, old Content-local
syntax, prompt parameter publication, formatter behavior, and repository-owned
fixtures and examples.

## Upstream 0.2.2 Changes

The released grammar changes relevant to Toolang are:

- cap declarations expose repeated `property` fields directly and an optional
  `body` whose node type is `cap_body`;
- prompt declarations have no parameter property or typed signature;
- `let name = CONTENT` replaces `let name: CONTENT`;
- recall has dedicated keyword/value nodes and a closed value vocabulary;
- `Pack` is parsed as a user type rather than a builtin;
- optional Agic and Flow names remain syntax, with uniqueness left to semantic
  validation; and
- the grammar documents `Text` for untyped named parameters, `Part[]` for
  omitted returns, and `far`, `near`, and `line` as reserved runtime locals.

No grammar source or generated parser artifact is copied into this repository.
The published Python package remains the sole parser dependency.

## Scope

In scope:

- dependency and lockfile update;
- CST-to-AST lowering changes in `toolang.lang`;
- exact cap property/body validation;
- prompt placeholder discovery and prompt parameter publication;
- runnable default types and reserved names;
- recall and Content-local syntax representation;
- formatter support;
- minimal downstream adaptation needed to consume the new AST vocabulary;
- migration of tracked `.too` sources, embedded test sources, and language
  documentation; and
- offline regression and acceptance tests.

Out of scope:

- parsing or translating removed 0.2.1 authored syntax;
- warning or deprecation periods for `params`, old recall values, or
  `let name:`;
- implementing durable `far`, current-thread `near`, or current-run `line`
  values as a complete runtime local model;
- adding typed or optional prompt placeholder syntax;
- changing Call Input forms or the prohibition on nested prompt calls;
- changing the tree-sitter grammar repository; and
- unrelated cap catalog, state, execution, or CLI redesign.

## Cap Lowering Boundary

All four cap kinds share this syntactic CST shape:

```text
CapDecl = kind name ":" line_end Property* Body?
Property = key "=" text_line
Body = cap_body
```

Lowering reads properties with `children_by_field_name("property")` from the
cap declaration. It reads `body` with `child_by_field_name("body")`; absence
normalizes to an empty string and is then accepted or rejected by the kind
validator.

Property occurrences must remain ordered until validation completes. The
implementation must not create `CapDecl.meta` with a dictionary comprehension
before duplicate detection. After successful validation, it may construct the
existing immutable metadata mapping so downstream consumers and serialized AST
data keep one canonical property representation.

The body contains only the `cap_body` text. Leading properties and trivia are
never included in `CapDecl.body`. Once the grammar has started the body, a
property-looking line is literal body text and is not reinterpreted by semantic
validation.

## Kind-Specific Cap Validation

Validation dispatches by `CapDecl.kind`; there is no shared permissive fallback.
Every property value is stripped, must be nonempty when present, and preserves
its authored text otherwise.

| Kind | Allowed properties | Required properties | Body |
| --- | --- | --- | --- |
| `psyche` | none | none | required and nonempty |
| `skill` | `description` | `description` | required and nonempty |
| `service` | `description`, `transport`, `protocol`, `target`, `headers`, `env` | `description`, exactly one of `transport` or `protocol`, `target` | optional |
| `prompt` | none | none | required and nonempty |

The common rules are:

- a property name may appear at most once;
- an unknown property is rejected on its own source line and the diagnostic
  names the allowed properties for that cap kind;
- a missing required property is reported on the declaration line;
- an empty required or optional property value is reported on the property
  line;
- a required body containing only whitespace or trivia is treated as missing;
  and
- prompt `params` is an unknown property and receives the prompt-specific
  no-properties diagnostic.

Service adds these value and cross-field rules:

- `transport` and `protocol` are equivalent accepted spellings but are mutually
  exclusive; successful lowering canonicalizes either spelling to a single
  `transport` entry in `CapDecl.meta` so downstream service consumers never
  need to branch on the alias;
- the selected transport value is exactly `http` or `stdio`;
- `target` is nonempty and remains opaque text at the language boundary;
- `headers`, when present, is nonempty inline text and is valid only for
  `http`;
- `env`, when present, is a nonempty comma-separated list of unique canonical
  environment names matching `[A-Za-z_][A-Za-z0-9_]*`; and
- service body text is optional because `description` supplies the required
  selection summary.

These rules apply to inline `.too` cap declarations. Markdown/frontmatter cap
files retain their catalog-owned parser and validation; this change does not
make `toolang.lang` parse those files or introduce a dependency from `lang` to
`catalog`.

## Prompt Inputs

Prompt input discovery scans the raw lowered body with the same Mustache name
rules used by Content template resolution. It collects each root name on first
appearance, including names introduced by section or inverted-section tags,
and ignores closing tags and repeated appearances.

`{{_}}` declares use of primary Call Input and is not added to
`CapDecl.params`. Every other discovered root becomes one required
`Parameter(name=<root>, type_name="Text")`. There is no optional marker,
default value, property declaration, or typed placeholder in this version.

The raw placeholders remain unchanged in `CapDecl.body`. Existing prompt
completion, HTTP, Chat, execution, and provenance consumers continue reading
`CapDecl.params`; they receive the derived tuple without learning parser
details.

## Runnable Defaults And Reserved Names

Lowering materializes grammar-defined defaults in the semantic AST:

| Authored form | AST value |
| --- | --- |
| implicit or explicit `_` without a type | `Part[]` |
| named parameter without a type | `Text` |
| omitted Agic or Flow return type | `Part[]` |
| explicit empty `()` | no primary input and no named parameters |

`far`, `near`, and `line` cannot be authored as named parameters or Flow
bindings. `_` retains its existing primary/current-value meaning. Placeholder
references to reserved runtime locals remain syntax; execution values for those
locals are deferred.

`Pack` is absent from the builtin/reserved type set. A struct named `Pack` and
references to that user type follow the same rules as any other declared
struct.

## Recall Integration

The grammar guarantees one of these authored values:

```text
auto
none
far
near
far, near
```

Lowering preserves the ordered values in `Directive.values`. Validation makes
`recall` singular and Agic-only; Flow recall is rejected. Grammar-invalid
operators, orderings, and values remain syntax errors.

For compatibility with current executable behavior while full recall locals
remain out of scope, omission and `auto` use the current default conversation
history behavior, `near` enables that same existing history source, `none` and
`far` do not include it, and `far, near` includes it because `near` is present.
No implementation claims to populate a distinct `far` value in this change.

## Flow Content Locals

Both inline and indented Content assignments use `=`:

```too
let note = Keep this text.
let detail =
  Keep this block too.
```

They lower to the existing `LetStmt(binding=<name>, value=<text>)` and retain
the existing implicit `Part[]` local type. Operation bindings such as
`let result = run transform` remain distinct through the CST `statement`
field. `let run transform` still discards the operation result.

The formatter emits spaces around `=` and preserves the block indentation.
It never rewrites removed colon syntax because 0.2.2 rejects that source before
formatting.

## Migration

Repository-owned migration includes:

- replacing prompt `params` properties with the placeholders already present
  in each body;
- adding missing placeholders where a test intentionally exposes named prompt
  completion metadata;
- replacing every tracked `let name:` Content assignment with `let name =`;
- replacing old recall values with the canonical 0.2.2 value that preserves the
  tested behavior;
- updating service examples as needed to satisfy the exact service contract;
- updating `docs/program.md` and directly related input documentation; and
- updating AST/API expectations for explicit default types and derived prompt
  parameters.

The implementation must use `git ls-files` when enumerating migration targets
so untracked source in the developer's primary checkout is never included.

## Design Touchpoints

Likely product files:

- `pyproject.toml`
- `uv.lock`
- `src/toolang/lang/ast.py`
- `src/toolang/lang/lower.py`
- `src/toolang/lang/validate.py`
- `src/toolang/lang/format.py`
- `src/toolang/lang/types.py`
- `src/toolang/lang/input.py`
- `src/toolang/execution/executor/prepare.py` for the minimal recall mapping

Likely test and authored-source files:

- `tests/unit/lang/test_program.py`
- `tests/unit/lang/test_program_format.py`
- `tests/unit/lang/test_input.py`
- prompt publication/API tests that assert parameter metadata
- execution tests containing Content locals or old recall values
- tracked `tests/fixtures/*.too` and `examples/*.too`
- `docs/program.md` and directly affected input documentation

Implementation must keep the diff limited to grammar integration and necessary
migration. It must not edit `archive/`, `dist/`, `scratch/`, or unrelated files.

## Acceptance Tests

Language tests must cover:

- properties lowered from the declaration rather than `cap_body`;
- absent and nonempty bodies for every cap kind;
- the exact allowed/required property matrix;
- duplicate, unknown, empty, and missing properties with precise lines;
- service transport alias exclusivity, value validation, HTTP-only headers,
  and environment-name validation;
- prompt `params` rejection;
- ordered, deduplicated prompt placeholder discovery, including `_`, sections,
  dotted references, and closing tags;
- required prompt arguments published through existing API/completion surfaces;
- inline and block `let name = CONTENT`, operation bindings, and rejection of
  colon syntax;
- every canonical recall form plus rejected Flow recall;
- named-parameter and return defaults;
- reserved parameter/binding names;
- `Pack` as a declared user type;
- Program data round trips with the updated semantic AST; and
- formatter idempotence and parse/format semantic equivalence.

Repository tests must prove that every tracked `.too` fixture and example
parses and formats under 0.2.2. The existing default suite remains offline and
deterministic.

## Verification

Run targeted checks first:

```sh
uv run pytest tests/unit/lang
uv run pytest tests/integration/api tests/unit/execution/test_calls.py
```

Then run the required default verification before commit and again after
rebasing onto the latest `origin/main`:

```sh
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest
```

Live-provider tests remain opt-in and are not part of acceptance.

## Risks

- Collapsing property nodes too early can hide duplicates and produce metadata
  that appears valid; validation must precede mapping construction.
- Prompt placeholder extraction can diverge from Content rendering; both must
  share the same root-name rules and tests.
- Explicit AST defaults can change serialized Program data and downstream CLI
  schemas; round-trip and public metadata tests must cover this.
- Migrating recall vocabulary without implementing `far` can imply unavailable
  behavior; documentation and tests must state the minimal mapping precisely.
- Repository source migration is broad enough to overlap active work; the
  implementation branch must rebase first and keep changes mechanical outside
  the language modules.

## Resolved Decisions

- Removed 0.2.1 authored syntax has no compatibility or deprecation path.
- Full `far`, `near`, and `line` runtime-local semantics are deferred.
- Cap syntax is shared, but property and body validity is strict and dispatched
  by cap kind.
- Inline cap metadata remains an immutable mapping after successful validation;
  downstream packages do not consume raw CST properties.
- No open design questions remain for implementation.
