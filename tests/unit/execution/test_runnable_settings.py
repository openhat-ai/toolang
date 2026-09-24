"""Configuration overrides remain independent of resource selection."""

from toolang.execution.settings import resolve_settings
from toolang.execution.types import PromptSetting
from toolang.lang.ast import AgicDecl, FlowDecl, Directive, Span


SPAN = Span(line=1)


def directive(name, *values):
    return Directive(name=name, operator="=", values=values, span=SPAN)


def test_settings_inherit_across_runnable_kinds_without_changing_siblings():
    parent = resolve_settings(
        AgicDecl(
            name="parent",
            span=SPAN,
            instruct="guidance",
            directives=(
                directive("lanes", "2"),
                directive("recall", "none"),
                directive("hands", "worker"),
            ),
        ),
        "parent",
    )
    flow = resolve_settings(FlowDecl(name="flow", span=SPAN), "module", parent)
    assert flow == parent
    child = resolve_settings(
        AgicDecl(
            name="child",
            span=SPAN,
            context="default",
            instruct="none",
            directives=(
                directive("lanes", "8"),
                directive("recall", "near"),
                directive("hands", "none"),
                directive("handoffs", "helper"),
            ),
        ),
        "module",
        flow,
    )
    assert child.lanes == 8 and child.recall == ("near",)
    assert child.hands == ("none",) and child.handoffs == ("helper",)
    assert child.context == PromptSetting("module", "default")
    assert child.instruct == PromptSetting("module", "none")
    assert parent.lanes == 2 and parent.hands == ("worker",)
    assert flow.instruct == PromptSetting("parent", "guidance")
    assert (
        resolve_settings(AgicDecl(name="sibling", span=SPAN), "module", flow) == parent
    )


def test_root_defaults_are_concrete():
    settings = resolve_settings(FlowDecl(name="root", span=SPAN), "module")
    assert settings.lanes == 4
    assert settings.recall == ("far", "near")
    assert settings.hands == settings.handoffs == ()
    assert settings.context == settings.instruct == PromptSetting("module")


def test_recall_views_are_selected_from_the_full_snapshot():
    from toolang.base.types.message import Message
    from toolang.execution.recall import history_variables

    near = (Message.user("recent"),)
    summary = Message.user("summary").to_data()
    for policy, far, recent, past in (
        (("default",), "summary", [near[0].to_data()], [summary, near[0].to_data()]),
        (("*",), "summary", [near[0].to_data()], [summary, near[0].to_data()]),
        (("far", "near"), "summary", [near[0].to_data()], [summary, near[0].to_data()]),
        (("far",), "summary", [], [summary]),
        (("near",), "", [near[0].to_data()], [near[0].to_data()]),
        (("none",), "", [], []),
    ):
        assert history_variables("summary", near, policy) == {
            "_far": far,
            "_near": recent,
            "_past": past,
        }
    assert history_variables("", (), ("far", "near")) == {
        "_far": "",
        "_near": [],
        "_past": [],
    }
