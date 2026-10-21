"""Resolve new-launch inputs and explicit attachment requirements."""

import asyncio
from collections.abc import Mapping
from pathlib import Path

from toolang.base.types.model import ModelOverride
from toolang.common.layout import AgentLayout
from toolang.plugin.catalogs.models_dev.path import MODEL_CATALOG_ENV
from toolang.up import sandbox as sandbox_runtime
from toolang.up.contract import development_bindings
from toolang.up.logging import (
    configure_logging_plan,
    resolve_agent_logging,
)
from toolang.up.types import ExecutionDrivers, ServerLifetime
from .policy import (
    resolve_ceiling_overrides,
    resolve_compact_override,
    resolve_default_overrides,
    resolve_limit_overrides,
)
from .ports import agent_port


def resolve_attachment_requirements(
    *,
    sandbox: str,
    dev: Path | None = None,
    model_catalog: Path | None = None,
    compact_override: ModelOverride | None = None,
    workspace_additions: Mapping[str, str] | None = None,
    allows: list[str] | None = None,
    defaults: list[str] | None = None,
    limits: list[str] | None = None,
    host: str | None = None,
    endpoint_host: str | None = None,
    port: int | None = None,
) -> dict[str, object]:
    """Resolve explicit constraints without reading local startup defaults."""
    requirements: dict[str, object] = {
        name: value
        for name, value in (
            ("host", host),
            ("endpoint_host", endpoint_host),
            ("port", port),
            ("compact_override", compact_override),
        )
        if value is not None
    }
    if model_catalog is not None:
        requirements["model_catalog"] = str(model_catalog.expanduser().resolve())
    if workspace_additions:
        requirements["workspace_additions"] = workspace_additions
    if allows:
        requirements["ceiling_overrides"] = resolve_ceiling_overrides({}, allows)
    if defaults:
        requirements["default_overrides"] = resolve_default_overrides({}, defaults)
    if limits:
        requirements["limit_overrides"] = resolve_limit_overrides({}, limits)
    if dev is not None:
        artifact = sandbox_runtime.resolve_dev_artifact(dev, sandbox=sandbox)
        assert artifact is not None
        requirements.update(development_bindings(artifact))
    return requirements


def resolve_runtime_launch(
    layout: AgentLayout,
    *,
    toolang_root: Path,
    environ: Mapping[str, str],
    sandbox: str | None,
    dev: Path | None,
    lifetime: ServerLifetime,
    background: bool,
    drivers: ExecutionDrivers | None = None,
    model_catalog: Path | None = None,
    host: str = "127.0.0.1",
    endpoint_host: str | None = None,
    port: int | None = None,
    active_port: int | None = None,
    temporary_port: bool = False,
    allows: list[str] | None = None,
    defaults: list[str] | None = None,
    limits: list[str] | None = None,
    compact_override: ModelOverride | None = None,
    workspace_additions: Mapping[str, str] | None = None,
    workdir: str | None = None,
) -> sandbox_runtime.LaunchSpec:
    """Use the same environment, policy, logging, and launch resolution sequence."""
    environ = dict(environ)
    environ["TOOLANG_ROOT"] = str(toolang_root)
    if model_catalog is not None:
        environ[MODEL_CATALOG_ENV] = str(model_catalog)
    port = (
        None
        if temporary_port
        else agent_port(layout, port, environ=environ) or active_port
    )
    log_plan = resolve_agent_logging(
        mode="start" if background else "run",
        environ=environ,
        agent_log_path=layout.runtime_log,
    )
    if not background:
        configure_logging_plan(log_plan)
    return asyncio.run(
        sandbox_runtime.resolve_launch(
            layout=layout,
            toolang_root=toolang_root,
            sandbox=sandbox,
            dev=dev,
            lifetime=lifetime,
            drivers=drivers,
            host=host,
            endpoint_host=endpoint_host,
            port=port,
            temporary_port=temporary_port,
            environ=log_plan.environ,
            ceiling_overrides=resolve_ceiling_overrides(environ, allows),
            default_overrides=resolve_default_overrides(environ, defaults),
            limit_overrides=resolve_limit_overrides(environ, limits),
            compact_override=compact_override or resolve_compact_override(environ),
            log_spec=log_plan.spec,
            output="file" if background else "inherit",
            log_path=log_plan.path,
            workspace_additions=workspace_additions,
            workdir=workdir,
        )
    )
