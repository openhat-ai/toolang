"""Recall selection and visibility derived from structured message metadata."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
import re

from toolang.base.types.message import Message

from .records import RecallControlPayload
from .types import MessageTemplate, RecallTarget
from .types import (
    RulesRecallTarget,
    SkillRecallTarget,
    ServiceRecallTarget,
    SkillTriggerRecallTarget,
    ServiceTriggerRecallTarget,
    WorkspaceRecallTarget,
    PsycheRecallTarget,
)


def recall_sources(values: Sequence[str] = ()) -> tuple[str, ...]:
    """Resolve the agic's history policy before assembling its messages."""

    return ("far", "near") if not values or "auto" in values else tuple(values)


def history_variables(
    far: str, near: Sequence[Message], recall: Sequence[str]
) -> dict[str, object]:
    """Derive a runnable's view from the full root history snapshot."""
    sources = recall_sources(recall)
    summary = far if "far" in sources else ""
    recent = list(near) if "near" in sources else []
    past = ([Message.user(summary)] if summary else []) + recent
    return {
        "_far": summary,
        "_near": [message.to_data() for message in recent],
        "_past": [message.to_data() for message in past],
    }


def required_declarations(
    declarations: Sequence[RecallControlPayload],
    visible: Mapping[RecallTarget, str],
) -> tuple[RecallControlPayload, ...]:
    """Reconcile supplied current facts with selected history, without I/O."""
    current = {item.target: item for item in declarations}
    result: list[RecallControlPayload] = []
    for target, revision in visible.items():
        if revision == "0":
            continue
        if isinstance(target, SkillRecallTarget | ServiceRecallTarget):
            trigger = (
                SkillTriggerRecallTarget
                if isinstance(target, SkillRecallTarget)
                else ServiceTriggerRecallTarget
            )(target.ref)
            stale = trigger not in current or current[trigger].revision != revision
        elif isinstance(target, RulesRecallTarget):
            workspace = WorkspaceRecallTarget(target.workspace)
            stale = (
                workspace not in current
                or visible.get(workspace) != current[workspace].revision
            )
        else:
            stale = target not in current
        if stale:
            result.append(RecallControlPayload(target, "0", ""))
    result.extend(
        item for item in declarations if visible.get(item.target) != item.revision
    )
    return tuple(result)


def canonical_recall(payload: RecallControlPayload) -> RecallControlPayload:
    """Normalize an incoming revision once, before comparison or persistence."""

    if re.fullmatch(r"[0-9a-fA-F]{1,64}", payload.revision) is None:
        raise ValueError("recall revision must contain 1–64 hexadecimal digits")
    value = int(payload.revision, 16)
    revision = f"{value:064x}" if value else "0"
    if not value and payload.content:
        raise ValueError("a removed recall must have empty content")
    return replace(payload, revision=revision)


def recall_revisions(
    messages: Iterable[MessageTemplate | Message],
) -> dict[RecallTarget, str]:
    """Fold presented declarations by tag/ref, preserving removal tombstones."""

    targets = {
        "skill-guidance": SkillRecallTarget,
        "service-guidance": ServiceRecallTarget,
        "skill-trigger": SkillTriggerRecallTarget,
        "service-trigger": ServiceTriggerRecallTarget,
        "psyche": PsycheRecallTarget,
        "workspace-access": WorkspaceRecallTarget,
    }
    revisions: dict[RecallTarget, str] = {}
    for message in messages:
        if message.role != "user" or message.recall is None:
            continue
        ref = message.recall.ref
        if message.tag == "workspace-rules":
            workspace, separator, path = ref.partition("/")
            if not workspace or not separator:
                raise ValueError(f"invalid rules recall ref: {ref}")
            target = RulesRecallTarget(workspace, "/" + path)
        elif message.tag in targets:
            target = targets[message.tag](ref)
        else:
            raise ValueError(f"invalid recall tag: {message.tag}")
        revisions[target] = message.recall.revision
    return revisions
