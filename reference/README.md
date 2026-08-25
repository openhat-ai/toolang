# Toolang Reference

This directory is reserved for implementation reference documents generated
from code.

It is intentionally separate from `docs/`.

- `docs/`
  - hand-written design documents
  - concepts, boundaries, lifecycle, layout, and implementation direction
- `reference/`
  - generated reference material derived from code
  - package/module/function reference, extracted API surface, and similar output

Rules:

- do not put design documents in `reference/`
- do not treat `reference/` as the source of truth for design semantics
- generated files in `reference/` should reflect the current code surface
- when implementation reference and design docs disagree, update either the
  code or the design docs so the disagreement disappears

## One-Command Build

Generate the current implementation reference with:

```bash
uv run python reference/build.py
```

Configuration for the generation step lives in:

- `reference/config.toml`
- `reference/templates/`

Generated output is written to:

- `reference/generated/html/`

The generated site currently covers:

- `toolang`
  - including `toolang.base`
  - including `toolang.execution`
  - including `toolang.plugin.models`
  - including `toolang.plugin.toolsets`
  - including other public subpackages under `src/toolang/`
