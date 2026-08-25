from __future__ import annotations

from pathlib import Path

from toolang.base.types.sandbox import SandboxRef
from toolang.common.layout import AgentLayout
from toolang.up import process
from toolang.up.records import SandboxState


def test_runtime_identity_uses_environment_process_without_sandbox_state() -> None:
    assert process.runtime_identity_row({"pid": 1234}) == ("PID", "1234")


def test_runtime_identity_formats_named_docker_container(tmp_path: Path) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    SandboxState(
        sandbox="docker:python:3.13-slim",
        ref=SandboxRef(
            runtime_id="176191c1528b8e2861cc16422dee13ade59d4977c2148a9ebf5d36a06f090abb",
            endpoint="http://localhost:7001",
            runtime_kind="container",
            runtime_name="toolang-alice-launch",
        ),
    ).save(layout.sandbox_state)

    assert process.runtime_identity_row({"pid": 1}, layout=layout) == (
        "Container",
        "toolang-alice-launch (176191c1528b)",
    )


def test_runtime_identity_formats_legacy_docker_reference(tmp_path: Path) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    SandboxState(
        sandbox="docker:python:3.13-slim",
        ref=SandboxRef(
            runtime_id=(
                "176191c1528b8e2861cc16422dee13ade59d4977c2148a9ebf5d36a06f090abb"
            ),
            endpoint="http://localhost:7001",
            meta={"container_name": "toolang-alice-legacy"},
        ),
    ).save(layout.sandbox_state)

    assert process.runtime_identity_row({"pid": 1}, layout=layout) == (
        "Container",
        "toolang-alice-legacy (176191c1528b)",
    )


def test_runtime_identity_preserves_unknown_opaque_id(tmp_path: Path) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    SandboxState(
        sandbox="custom",
        ref=SandboxRef(
            runtime_id="opaque:identifier:that-must-not-be-shortened",
            endpoint="http://localhost:7001",
        ),
    ).save(layout.sandbox_state)

    assert process.runtime_identity_row({"pid": 1}, layout=layout) == (
        "Runtime",
        "workload:opaque:identifier:that-must-not-be-shortened",
    )
