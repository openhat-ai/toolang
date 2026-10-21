"""Runnable identity, kind metadata, and retained-name decoding contracts."""

import pytest

from toolang.base.types.policy import RunLimits
from toolang.execution.records import (
    LaunchContext,
    RunControlPayload,
    control_payload_from_data,
    control_payload_to_data,
)
from toolang.execution.runnables import resolve_runnable_reference
from toolang.execution.types import AgentResources, PromptSetting, RunnableSettings
from toolang.lang import Program
from toolang.lang.input import CallInput
from toolang.lang.types import display_runnable_ref


@pytest.mark.parametrize(
    ("legacy", "full_name", "kind"),
    [
        ("agic:chat", "agent::chat", "agic"),
        ("agent::flow:chat", "agent::chat", "flow"),
        ("runnables::research::flow:research", "research::research", "flow"),
        ("runtime::talk::agic:talk", "_::talk", "agic"),
        ("_:compact", "_::compact", "compact"),
        ("runtime::talk::_:compact", "_::compact", "compact"),
    ],
)
def test_retained_names_decode_to_fqn_and_independent_kind(legacy, full_name, kind):
    payload = RunControlPayload(
        resources=AgentResources(),
        limits=RunLimits(),
        state=None,
        runnable=legacy,
        input=CallInput({}),
    )
    assert payload.runnable == full_name
    assert payload.runnable_kind == kind
    data = control_payload_to_data(payload)
    assert data["runnable"] == full_name
    assert data["runnable_kind"] == kind
    assert control_payload_from_data("run", data) == payload


def test_kind_change_does_not_change_runnable_identity():
    agic = resolve_runnable_reference(
        Program.from_source("agic work:\n  pass\n"), "work"
    )
    flow = resolve_runnable_reference(
        Program.from_source("flow work:\n  pass\n"), "work"
    )
    assert agic.qualified == flow.qualified == "agent::work"
    assert agic.ref == flow.ref == "work"
    assert agic.executable.kind == "agic" and flow.executable.kind == "flow"


@pytest.mark.parametrize("kind", ["agic", "flow"])
def test_interface_labels_use_explicit_kind_and_allow_empty_names(kind):
    assert (
        display_runnable_ref("agent::work", kind=kind, surface="chat") == f"{kind}:work"
    )
    assert (
        display_runnable_ref("agent::<entry:3>", kind=kind, surface="progress")
        == f"{kind}:"
    )


def test_retained_launch_context_normalizes_owning_and_inherited_modules():
    context = LaunchContext(
        settings=RunnableSettings(
            module="runnables::research",
            instruct=PromptSetting("runnables::research", "instructions"),
            context=PromptSetting("runtime::talk", "default"),
        ),
        workspaces={},
        iterations={},
        result=None,
    )
    assert context.settings.module == "research"
    assert context.settings.instruct == PromptSetting("research", "instructions")
    assert context.settings.context == PromptSetting("_", "default")
    payload = RunControlPayload(
        resources=AgentResources(),
        limits=RunLimits(),
        state=None,
        runnable="runnables::research::agic:research",
        input=CallInput({}),
        launch_context=context,
        runtime_sources={"runtime::talk": "agic talk:\n  pass\n"},
    )
    assert tuple(payload.runtime_sources) == ("_",)
    assert control_payload_from_data("run", control_payload_to_data(payload)) == payload
