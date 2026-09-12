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

## Stable Grouping

Keep adjacent declarations or directive-like statements together by category.
Within a category, use no blank lines and preserve the author's original order;
do not alphabetize or otherwise sort the entries. Separate categories with at
most one blank line when the distinction improves readability.

```too
with psyche briceyan/concise
with psyche briceyan/safety

with skill briceyan/review
with skill briceyan/verification
```

Apply the same stable grouping to resource directives inside an agic. Do not
move executable flow statements across one another merely to make groups.

## Default Capabilities

Psyches, skills, and services available to an agic are not normally repeated
inside it. Omit the corresponding directives when the default set is correct.
Declare `psyches`, `skills`, or `services` only to narrow or deliberately
change that selection.

```too
agic diagnose:
  tools = fs/read, shell/execute

  Diagnose {{_}} using the available Toolang guidance.
```

Here the tool boundary is intentional; repeating every available psyche and
skill would add noise without changing the runnable.

## Context And Instruct Declarations

Prefer top-level `context` and `instruct` declarations over inline blocks. This
keeps reusable data and agent behavior separate from runnable resources and
messages.

When the program has only one declaration of a kind, it may be unnamed. The
unnamed declaration is the program default, so an agic that uses it does not
need a selector:

```too
context:
  The current project belongs to {{agent.name}}.

instruct:
  Review evidence carefully and distinguish facts from assumptions.

agic review:
  Review {{_}} and return prioritized findings.
```

Name declarations and select them with `context: name` or `instruct: name`
when the program offers multiple choices or the name clarifies a reusable
role.

Use an inline `context:` or `instruct:` only when it is short, specific to one
agic, and a name would add indirection without clarifying ownership. Do not
duplicate the same inline body across runnables.

## Messages

Write a message on the same line as its role when its content fits on one line:

```too
agic continue_conversation:
  user: Review the current conclusion.
  assistant: The conclusion needs stronger evidence.
  user: Revise it using {{_}}.
```

When an agic has only one user message, omit the role and write the message
directly. Toolang treats it as the runnable's user request:

```too
agic review:
  Review {{_}} and return prioritized findings.
```

Use an explicit role when multiple messages form a conversation or an
`assistant` turn must be represented.

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
