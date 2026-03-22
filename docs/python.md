# Toolang Python Design Style

This document defines how Python code in Toolang should reflect the runtime
design clearly and consistently.

The goal is not to maximize comments. The goal is to make module boundaries,
names, types, and docstrings express the design directly, so comments only need
to explain the parts that code cannot show naturally.


## 1. General Rule

Prefer code structure over explanatory prose.

When a design concept is stable, express it through:

- package and module boundaries
- names
- typed data structures
- small public facades

Use comments and docstrings to explain:

- why a boundary exists
- what a module does not do
- invariants and tradeoffs

Do not use comments to restate obvious code.


## 2. Package And Module Boundaries

Module layout should reflect the design map.

Examples:

- `toolang.agent`
  - agent identity, resolution, preparation, and registry
- `toolang.runtime`
  - turn execution, prompt build, chat state, and serving
- `toolang.caps`
  - cap sync, overlay, and scope-aware runtime views
- `toolang.layout`
  - canonical path and layout helpers

Rules:

- one module should own one main responsibility
- do not mix adjacent design layers into one module unless the boundary is
  trivial
- do not add layers that only forward parameters without adding meaning
- if a module needs a long explanation to justify its contents, the module
  boundary is probably wrong


## 3. Naming

Design terms in documentation and code must match.

When Toolang defines a stable concept, the code should use the same word:

- `runtime loop`
- `execution strategy`
- `activation`
- `thread`
- `turn`
- `step`
- `agent_uri`
- `agent_id`
- `agent home`
- `agent room`

Rules:

- do not use one term in docs and another in code for the same concept
- prefer direct names over implementation slang
- prefer stable domain names over temporary migration names


## 4. Types Before Comments

Core design concepts should become explicit Python types.

Prefer:

- `dataclass`
- `Enum`
- `Literal`
- `Protocol`
- explicit return types

Example direction:

```python
@dataclass
class Turn:
    turn_id: str
    thread_id: str
    activation_id: str
    origin: Origin
    strategy: ExecutionStrategy
```

Rules:

- important invariants should appear in types whenever possible
- do not leave core semantics only in comments or external docs
- make relationships visible through fields and signatures


## 5. Module Docstrings

Every package and every non-trivial module may have a short docstring.

Package-level docstrings belong in `__init__.py`.

Use a package docstring to describe:

- the package responsibility
- its boundary relative to nearby packages

Use a module docstring to describe:

- the module responsibility
- what this module does not own
- any especially important invariant

Rules:

- keep docstrings short
- prefer 3 to 8 lines
- do not duplicate the entire design doc in module docstrings
- do not move all design explanation into `__init__.py`

Example package docstring:

```python
"""Runtime execution package.

This package contains turn execution, long-lived server surfaces, and chat
state. It does not define agent identity, path layout, or source syncing.
"""
```

Example module docstring:

```python
"""FastAPI wiring for one running agent.

This module defines HTTP routes only. Running-state persistence lives in
`state.py`, and response shaping lives in `presenters.py`.
"""
```


## 6. Function Docstrings

Function docstrings are for public functions, boundary functions, and functions
whose role is not obvious from the name alone.

Focus on:

- the semantic meaning of the inputs
- the semantic meaning of the output
- the layer this function belongs to
- what it intentionally does not do

Rules:

- small private helpers usually do not need docstrings
- do not write parameter-by-parameter prose unless needed for semantics
- prefer describing the boundary and side effects over repeating the code flow


## 7. Comments

Comments should explain design intent, not restate code.

Good topics for comments:

- why a specific invariant exists
- why an ordering matters
- why a tradeoff was chosen
- why a lower-level helper must stay pure

Bad topics for comments:

- translating a line of code into English
- describing obvious assignments or branches

Rules:

- keep comments short
- place comments near the code that enforces the invariant
- prefer one precise comment over many low-signal comments


## 8. `__all__`

`__all__` is optional and should be used only when a module or package has a
clear public surface.

Good uses:

- a package `__init__.py` that acts as a stable facade
- a module with a small, intentional public export set

Bad uses:

- mechanically listing every symbol in an internal implementation module
- exporting underscored private helpers
- preserving a fake public API only for tests

Rules:

- most internal implementation modules should not define `__all__`
- if `__all__` exists, it should reflect a real public interface
- package facades may use `__all__`, but only when the facade has meaning


## 9. Facades

Some packages should expose a small stable import surface.

Examples of acceptable facades:

- `toolang.cli`
  - `app`, `main`
- `toolang.runtime`
  - stable runtime entry points only
- `toolang.agent`
  - stable agent-resolution and preparation entry points only

Rules:

- a facade should expose only meaningful public entry points
- do not route all imports through `__init__.py` by default
- do not keep compatibility exports that no longer match the real boundary


## 10. Relationship To Design Docs

The documents under `docs/` define stable concepts and boundaries.

Python code should mirror those concepts directly.

Rules:

- use the same vocabulary in code and docs
- when a stable design term changes, update names and docs together
- prefer changing code structure and naming before adding explanatory prose


## 11. Toolang-Specific Guidance

For this repository:

- package-level facades may use `__all__`
- most implementation modules should not
- package docstrings should describe boundaries
- module docstrings should describe local responsibility
- comments should explain invariants such as:
  - `thread` may outlive an `activation`
  - a `turn` always belongs to exactly one `activation`
  - `runtime loop` and `execution strategy` are orthogonal
  - shared bus state is a projection, not execution truth


## 12. Practical Checklist

When writing or reviewing Python code, check:

1. Does the module boundary match one design concept?
2. Do names match the design vocabulary?
3. Are core semantics represented as types?
4. Does the docstring describe responsibility and boundary?
5. Do comments explain why, not what?
6. Is `__all__` present only if this file is a real public facade?
