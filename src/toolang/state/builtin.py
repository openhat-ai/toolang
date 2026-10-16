"""Prepare isolated package-owned programs without a watcher-owned directory."""

from hashlib import sha256
from collections.abc import Mapping
from dataclasses import replace
from functools import lru_cache
from importlib.resources import files

from toolang.lang.ast import Program

from .source import ProgramSource
from .state import AgentState, compose_agent_state
from .types import RUNTIME_TALK_MODULE, RUNTIME_TALK_SOURCE


@lru_cache(maxsize=1)
def talk_handler_source() -> str:
    """Read the packaged Talk Program, independently of authored agent files."""

    return files("toolang.execution").joinpath("handlers/talk.too").read_text("utf-8")


@lru_cache(maxsize=16)
def _talk_program(source: str) -> Program:
    digest = sha256(source.encode("utf-8")).hexdigest()
    return ProgramSource(
        "runtime", "runnable", RUNTIME_TALK_SOURCE, RUNTIME_TALK_SOURCE, source, digest
    ).parse()


def with_runtime_handlers(state: AgentState, sources: Mapping[str, str]) -> AgentState:
    """Bind captured runtime code without changing user State or its revision."""

    if not sources:
        return state
    if set(sources) != {RUNTIME_TALK_MODULE}:
        raise ValueError("unknown runtime handler source")
    source = sources[RUNTIME_TALK_MODULE]
    module = RUNTIME_TALK_MODULE
    return replace(
        state,
        modules=dict(sorted({**state.modules, module: _talk_program(source)}.items())),
        module_sources=dict(
            sorted({**state.module_sources, module: RUNTIME_TALK_SOURCE}.items())
        ),
        module_digests=dict(
            sorted(
                {
                    **state.module_digests,
                    module: sha256(source.encode("utf-8")).hexdigest(),
                }.items()
            )
        ),
        module_caps=dict(sorted({**state.module_caps, module: ()}.items())),
        runtime_sources=dict(sources),
    )


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
