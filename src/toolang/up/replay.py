"""Validate retained replay provenance before execution."""

from toolang.common.layout import AgentLayout
from .instance import read_instance


def require_complete_seed(layout: AgentLayout) -> None:
    entry = read_instance(layout.runtime_status)
    if entry is not None and entry["control"].get("replay_seed_complete") is False:
        raise ValueError("incomplete replay seed cannot execute")
