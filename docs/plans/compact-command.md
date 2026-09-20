# Standalone compact command

Historical definition. CLI inputs and producer selection are superseded by
[Compact CLI execution modes](compact-cli-modes.md); result normalization is
superseded by [Compaction contract](compaction-contract-and-admission.md).

The original command ran one model-backed Run that returned a structured result,
and exposed begin/end/bare inputs. Those designs are superseded:

- [Compact CLI execution modes](compact-cli-modes.md): DEFAULT, local file, and
  FORGET producers; public thread/before inputs and lock validation.
- [Text-only algorithms](compact-summary-algorithm.md): required read bounds,
  injected previous summary, text-only producer output, and framework-owned
  result assembly/persistence.
- [Compaction contract](compaction-contract-and-admission.md): typed non-null
  coverage, result validation, and model input/output admission.

Current CLI usage and external algorithm requirements are in
[the API reference](../api.md#compact-local-history).
