"""Prepare isolated package-owned programs without a watcher-owned directory."""

from hashlib import sha256

from .source import ProgramSource
from .state import AgentState, compose_agent_state


def prepare_builtin_state(source: str) -> AgentState:
    digest = sha256(source.encode("utf-8")).hexdigest()
    program = ProgramSource(
        "runtime", "agent", "agent.too", "agent.too", source, digest
    ).parse()
    return compose_agent_state(
        name="runtime",
        root_revision=sha256(b"toolang:runtime").hexdigest(),
        home_revision=digest,
        root_config={},
        home_config={},
        root_caps=(),
        home_caps=(),
        modules={"agent": program},
        module_sources={"agent": "agent.too"},
        module_digests={"agent": digest},
        module_caps={"agent": ()},
    )
