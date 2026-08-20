# Execution Transcript

The transcript grammar is now part of
[execution-presentation.md](./execution-presentation.md). That document is the
single normative specification for Script and Chat execution progress.

The former transcript design exposed Run headers, StepPath facts, child-Run
closure rows, verbosity-specific structures, and the `·`, `↳`, and `!` row
markers. Those forms are retired. Current progress uses:

```text
[2] Search the web for each query

• Mapped all 6 items in parallel
  31.0s · 6 runs · 12 model calls · 8 tool calls · ↑18.4k ↓5.2k $0.00

• run_nrqpt0mf succeeded ──────────────────────────────
  1m 16s · 26 runs · 32 model calls · 8 tool calls · ↑43.8k ↓17.6k $0.01
```

This file remains as a compatibility pointer for existing documentation links.
