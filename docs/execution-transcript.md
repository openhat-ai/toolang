# Execution Transcript

The transcript grammar is now part of
[execution-presentation.md](./execution-presentation.md). That document is the
single normative specification for Script and Chat execution progress.

The former transcript design exposed Run headers, standalone StepPath facts,
child-Run closure rows, verbosity-specific structures, and the `·`, `↳`, and
`!` row markers. Those forms are retired. Current progress uses:

```text
[2] Search the web for each query

• Mapped all 6 items in parallel
  31s · 6 runs 12 models 8 tools · ↑18.4k ↓5.2k(3.1k) · ≈$0.01        run_root.2

∎ run_nrqpt0mf succeeded        1m16s · 26 runs 32 models 8 tools · ↑43.8k ↓17.6k(9.2k) · ≈$0.01
```

Run Steps owned by an Agic Run use a `---  run agic:NAME` or
`---  run flow:NAME` progress scope and close with aggregate facts plus
`STATUS CHILD_RUN_ID`. Run Steps owned by a Flow Run keep their numbered
headers and StepPath footers. This distinction is derived from the owning
`RunBegin.runnable`; the root `∎` footer is unchanged.

This file remains as a compatibility pointer for existing documentation links.
