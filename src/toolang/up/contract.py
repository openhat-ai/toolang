"""Secret-free comparison of effective executor startup bindings."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from enum import Enum
from hashlib import sha256
import json
from pathlib import Path
from typing import TYPE_CHECKING

from .instance import read_instance

if TYPE_CHECKING:
    from toolang.common.layout import AgentLayout
    from .sandbox import LaunchSpec


def _value(value):
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _value(getattr(value, field.name)) for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): _value(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(_value(item) for item in value)
    if isinstance(value, (tuple, list)):
        return [_value(item) for item in value]
    if isinstance(value, (Path, Enum)):
        return str(value)
    return value


def startup_contract(spec: LaunchSpec) -> dict[str, str]:
    bindings = {
        field.name: getattr(spec.serve, field.name)
        for field in fields(spec.serve)
        if field.name not in {"layout", "launch_id", "lifetime", "automatic_port"}
    }
    bindings.update(
        sandbox=spec.sandbox,
        config=spec.config,
        root=str(spec.serve.layout.root),
        source=str(spec.serve.layout.source_path or spec.serve.layout.program),
        project_inputs=spec.serve.layout.project_inputs,
        source_edit_lock=str(spec.serve.layout.root_edit_lock),
        environ={
            key: value
            for key, value in {**spec.dotenv_envs, **spec.environ}.items()
            if key
            not in {
                "TOOLANG_LOG_PATH",
                "TOOLANG_PROGRESS_MAX_WIDTH",
                "TOOLANG_UI_BASE_URL",
                "TOOLANG_HOST_LAUNCH",
            }
        },
        development=str(spec.dev_artifact) if spec.dev_artifact else None,
    )
    if spec.dev_artifact is not None:
        bindings["development_digest"] = sha256(
            spec.dev_artifact.read_bytes()
        ).hexdigest()
    return {
        key: sha256(json.dumps(_value(value), sort_keys=True).encode()).hexdigest()
        for key, value in bindings.items()
    }


def require_compatible(layout: AgentLayout, requested: LaunchSpec) -> None:
    entry = read_instance(layout.runtime_status) or {}
    saved = entry.get("control", {}).get("startup_contract")
    if not isinstance(saved, dict):
        raise ValueError(
            "running startup parameters are unverifiable; stop and restart the agent"
        )
    desired = startup_contract(requested)
    different = sorted(
        key
        for key in saved.keys() | desired.keys()
        if saved.get(key) != desired.get(key)
    )
    if different:
        raise ValueError("startup parameters differ: " + ", ".join(different))
