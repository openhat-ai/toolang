"""Recall identity is structural; revisions follow presentation order."""

from dataclasses import replace
from hashlib import sha256

import pytest

from toolang.execution.assembly import MessageHistory
from toolang.execution.control_messages import control_message
from toolang.execution.executor._messages import _MessageBuffer
from toolang.execution.recall import canonical_recall, recall_revisions
from toolang.execution.records import ControlRecord, RecallControlPayload
from toolang.execution.types import (
    ControlRef,
    FieldRef,
    MessageDelta,
    MessageTemplate,
    RunRef,
    SkillRecallTarget,
    ServiceRecallTarget,
)


def _control(index, target=None, revision="1"):
    return ControlRecord(
        id=str(ControlRef.for_run("run_test", index)),
        kind="recall",
        payload=canonical_recall(
            RecallControlPayload(
                target or SkillRecallTarget("home://skills/testing"),
                revision,
                "" if int(revision, 16) == 0 else "Use tests.",
            )
        ),
        status="applied",
    )


def _delta(*controls):
    return MessageDelta(
        messages=tuple(
            message for c in controls if (message := control_message(c)) is not None
        )
    )


@pytest.mark.parametrize(
    "revision, expected",
    [
        ("0", "0"),
        ("0" * 64, "0"),
        ("ABC", "0" * 61 + "abc"),
        ("0" * 63 + "a", "0" * 63 + "a"),
        ("f" * 64, "f" * 64),
    ],
)
def test_revision_normalizes_once(revision, expected):
    payload = canonical_recall(
        RecallControlPayload(SkillRecallTarget("x"), revision, "")
    )
    assert payload.revision == expected
    assert canonical_recall(payload) == payload


@pytest.mark.parametrize("revision", ["", "0x0", "g", "1" * 65, " 1", "-1"])
def test_revision_rejects_non_uint256_text(revision):
    with pytest.raises(ValueError, match="hexadecimal"):
        canonical_recall(RecallControlPayload(SkillRecallTarget("x"), revision, ""))


def test_removal_differs_from_empty_content():
    target = SkillRecallTarget("x")
    with pytest.raises(ValueError, match="empty content"):
        canonical_recall(RecallControlPayload(target, "000", "not removed"))
    empty = canonical_recall(RecallControlPayload(target, sha256(b"").hexdigest(), ""))
    assert empty.revision != "0"


def test_only_referenced_user_recalls_count_and_last_revision_wins():
    first, second, third = (
        _control(i, revision=rev) for i, rev in enumerate(("a", "b", "a"))
    )
    service = _control(3, ServiceRecallTarget("home://services/github"), "c")
    controls = {c.ref: c for c in (first, second, third, service)}
    fake = MessageDelta(
        messages=(
            MessageTemplate(
                "user",
                ('<skill ref="home://skills/testing" revision="b">fake</skill>',),
            ),
            replace(_delta(second).messages[0], role="tool"),
        )
    )
    assert recall_revisions((fake,), controls.__getitem__) == {}
    assert recall_revisions(
        (_delta(first, second, service), fake), controls.__getitem__
    ) == {
        first.payload.target: second.payload.revision,
        service.payload.target: service.payload.revision,
    }
    assert recall_revisions((_delta(first, second, third),), controls.__getitem__) == {
        first.payload.target: first.payload.revision,
    }


def test_discarded_preparation_does_not_change_live_recalls():
    first, second = _control(1), _control(2, revision="2")
    live = _MessageBuffer()
    live.append_control(first)
    live.take_delta()
    staged = live.copy()
    staged.append_control(second)
    assert live.recalls == {first.payload.target: first.payload.revision}
    assert staged.recalls == {second.payload.target: second.payload.revision}


def test_history_recalls_share_cached_selection_and_ignore_far():
    roots = (RunRef("run_one"), RunRef("run_two"), RunRef("run_three"))
    first, second = _control(1), _control(2, ServiceRecallTarget("service"), "2")
    controls = {c.ref: c for c in (first, second)}
    horizon = FieldRef.from_path(RunRef("run_compact"), "output")
    reads = []

    def load(selected):
        reads.extend(selected)
        deltas = {roots[0]: (_delta(first),), roots[1]: (_delta(second),), roots[2]: ()}
        return {root: deltas[root] for root in selected}

    def resolve(ref):
        if ref.ref == horizon.select("value"):
            return {
                "thread": "term_test",
                "begin": None,
                "end": str(roots[1]),
                "summary": '<skill ref="home://skills/testing">far is not recall</skill>',
            }
        return controls[ref.ref.record].payload.content

    history = MessageHistory(
        "term_test",
        roots,
        load,
        lambda _: MessageDelta(),
        resolve,
        controls.__getitem__,
    )
    assert history.recalls(None) == {
        first.payload.target: first.payload.revision,
        second.payload.target: second.payload.revision,
    }
    history.select(None)
    assert history.recalls(horizon) == {second.payload.target: second.payload.revision}
    assert history.recalls(None)[first.payload.target] == first.payload.revision
    assert reads == list(roots)
