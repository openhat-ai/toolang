# Chat Input Namespaces

Status: Implemented from the definition approved on 2026-08-29. Input syntax
is superseded by [Chat Commands and Run Directives](chat-prefix-rules.md) and
[Call Input](../call-input.md). Those documents own terminology, dispatch,
input boundaries, and signatures.

## Retained Contract

- Chat retains authored submission text for editing, input history, and
  transcript presentation.
- Run directives describe one run. Parsing produces `RunOverride` and
  `CallInput[str]`; execution resolves the public runnable, prompts, includes,
  and argument types against the accepted run's immutable State.
- Prompt calls use dollar directives. Resource includes use at directives.
  Neither syntax is passed to model adapters as an unevaluated instruction.
- Durable preparation retains authored input, resolved input, and ordered
  prompt-call provenance. `PromptInvocation` records the resolved definition
  and arguments; it is execution provenance rather than a command type.
- Recall consumes resolved user-message parts. Model-call inspection uses
  the persisted effective request. Retry and rerun retain their recorded input
  and State semantics; submitting authored text again creates a new run.
- Adapters consume normalized model calls and do not parse submission syntax
  or load prompt definitions.

## Current Owners

- [Input Syntax](../input-syntax.md): command and run-directive grammar.
- [Call Input](../call-input.md): input forms, signatures, and Content expansion.
- [Chat](../chat.md): commands, run submission, transcript, and status behavior.
- `execution/input.py`: shared submission parsing and reserved names.
- `lang/input.py`: call-input parsing and prompt-call provenance.
- `execution/calls.py` and `execution/records.py`: resolution and persistence.

## Acceptance

Authored text remains available after prompt expansion; prompt provenance refers
to immutable definitions; adapters receive normalized inputs; retry and rerun
preserve recorded behavior. The syntax acceptance matrix is maintained in the
current [prefix plan](chat-prefix-rules.md#implementation-and-acceptance).
