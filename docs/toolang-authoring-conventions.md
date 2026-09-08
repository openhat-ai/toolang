# Toolang Authoring Conventions

These conventions apply across Toolang source and its consumers. They guide
authoring style without introducing syntax or replacing parser requirements.

## Principle

Toolang source should explain itself. Prefer clear verbs, runnable names, and
natural prose over comments. Add a comment only when it supplies a description
needed by a consumer or information that the source cannot express directly.

Keep comments rare, short, and close to what they describe.

## Input Terminology

Call primary input **input** and named inputs **arguments** (singular:
**argument**). Use the full names when the distinction needs emphasis. Follow
the [input terminology](./call-input.md#terminology) and
[flat mapping convention](./call-input.md#flat-input-mappings) across authored
prose, documentation, and CLI help.

## Natural Language

Toolang deliberately keeps authored instructions close to natural language.
Write prompt bodies, agic messages, implicit runs, and other authored content as
ordinary prose.

### Implicit runs

An implicit run is a flow statement written directly as prose without the
`run` keyword. Write its prose naturally:

- Start English prose with normal sentence capitalization.
- End prose with appropriate punctuation.
- Use blank lines to make transitions between prose and explicit flow
  statements visually clear.
- When prose would begin with a lowercase statement-boundary keyword, capitalize
  it or use an explicit inline `run`. Write `Until ...` or `run: Until ...`
  instead of lowercase `until ...`.

These capitalization and punctuation recommendations are conventions. The
grammar separately reserves lowercase statement-boundary keywords; other
prose in an implicit run may still begin with interpolation, quoted text,
numbers, or a language without English letter case.

```too
flow research:
  Identify the important uncertainties in the question.

  storm 8 in 4 lanes using investigate
  sort descending by confidence

  Write an answer supported by the strongest findings.
```

## Type Annotations

Omit parameter and return types when Toolang's defaults already express the
runnable contract:

- an omitted parameter list implies a primary `_` input of `Part[]`;
- an explicit untyped `_` also defaults to `Part[]`;
- an untyped named parameter defaults to `Text`; and
- an omitted return type defaults to `Part[]`.

Prefer:

```too
agic transform:
  Transform the current input.

agic rewrite(_, instruction):
  Rewrite {{_}} according to {{instruction}}.
```

over redundant signatures:

```too
agic transform(_: Part[]) -> Part[]:
  Transform the current input.

agic rewrite(_: Part[], instruction: Text) -> Part[]:
  Rewrite {{_}} according to {{instruction}}.
```

Add a type only when it differs from the default or communicates a contract
that the consuming syntax does not already determine. Keep `()` when a
runnable intentionally accepts no primary input, and keep `?` when a named
parameter is optional.

Do not repeat a return type that the consuming context already determines.
Inline runnables after `if` and `by` are the common cases, with `Boolean` and
`Number` results respectively:

```too
keep if:
  Return true when the current item is actionable.

sort descending by:
  Score the current item by priority.
```

## Documentation Comments

Use `##` when the text is documentation that a consumer may display. Place it
immediately above the item or statement it describes, with no blank line in
between. Prefer one concise line.

### Runnable descriptions

A `##` comment immediately above an `agic` or `flow` supplies its runnable
description when the source is used as a script.

```too
## Review a change and return prioritized findings.
agic review -> ReviewResult:
  ...

## Investigate a question and produce a supported answer.
flow research(question) -> Answer:
  ...
```

Describe the capability or result. Do not repeat the declaration name,
parameters, or return type.

### Flow stage descriptions

A `##` comment immediately above a flow statement supplies its plan phase or
stage description for UI and progress displays.

```too
flow research:
  ## Generate independent approaches
  storm 8 in 4 lanes using investigate

  ## Review proposals in parallel
  map in 4 lanes using review_proposal

  ## Prioritize the strongest findings
  sort descending by confidence
```

Start with an action verb and describe the purpose of the stage. Do not merely
translate the statement into prose.

```too
# Avoid: restates the syntax without adding intent.
## Run investigate eight times in four lanes
storm 8 in 4 lanes using investigate
```

Use `##!` only when the parent or the complete script needs a description. A
source file should rarely need more than one parent documentation comment.

## Ordinary Comments

Use `#` for short, human-only rationale or constraints that should not become a
runnable or stage description.

```too
# Keep this limit within the provider quota.
storm 8 in 4 lanes using investigate
```

Avoid comments that narrate the syntax, long design notes, and frequent inline
comments. Prefer a short standalone line when a comment is necessary, and keep
comments outside natural-language content.

## Review Checklist

- Can clearer source or a better name remove the comment?
- Does each `##` provide useful consumer-facing text?
- Is each `##` immediately adjacent to its target?
- Does each `#` explain rationale or a constraint instead of restating code?
- Can every remaining comment be shortened?
