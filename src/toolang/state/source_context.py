"""Reconcile source path metadata with accepted State workspace identities."""

from collections.abc import Mapping
from dataclasses import replace
import os
from pathlib import Path
from typing import cast

from toolang.common.config_runtime import ConfigOrigins
from toolang.common.config_sources import config_origins
from toolang.common.layout import AgentLayout

from .cache import layer_revision_dir, load_agent_revisions, load_home_layer
from .config import parse_config, root_workspace_declarations
from .source import ROOT_WORKSPACE_SOURCE


def source_config_context(layout: AgentLayout) -> ConfigOrigins | None:
    """Reuse published host identities for unchanged authored workspace references.

    Launch origins resolve paths unavailable in the guest. They must not undo a
    later canonical identity published by the host after a symlink is retargeted.
    Execution bindings remain frozen and will reject that newly bound source.
    """
    context = config_origins(layout)
    if not context.origins or not context.captured:
        return context

    def declarations(path: Path, *, root: bool) -> Mapping[str, object]:
        config = parse_config(path.read_bytes()) if path.exists() else {}
        values = config.get("workspaces", {})
        if root:
            return root_workspace_declarations(config, layout.config_key)
        return cast(Mapping[str, object], values) if isinstance(values, Mapping) else {}

    try:
        _, root_revision, home_revision, _, _, _, _ = load_agent_revisions(layout)
        home = load_home_layer(layout, home_revision)
        root_source = (
            layer_revision_dir(layout, "root", root_revision) / "source/config.toml"
        )
        home_source = home.revision_dir / "source/config.toml"
        receipt = home.revision_dir / "source" / ROOT_WORKSPACE_SOURCE
        root_grants = declarations(receipt, root=False)
        home_grants = cast(Mapping[str, object], home.config.get("workspaces", {}))
        origins = dict(context.origins)
        for scope, current, captured, grants in (
            ("root", layout.place_config, root_source, root_grants),
            ("home", layout.config, home_source, home_grants),
        ):
            previous = declarations(captured, root=scope == "root")
            for name, value in declarations(current, root=scope == "root").items():
                canonical = grants.get(name)
                if (
                    not isinstance(value, str)
                    or previous.get(name) != value
                    or not isinstance(canonical, str)
                    or scope == "home"
                    and name in root_grants
                ):
                    continue
                reference = os.path.normpath(
                    str(context.origin(scope, current).parent / value)
                )
                origins[reference] = Path(canonical)
        return (
            replace(context, origins=origins) if origins != context.origins else context
        )
    except (OSError, KeyError, TypeError, ValueError):
        # Missing/rejected snapshots are handled by ordinary source preparation.
        return context
