"""Discover authorized workspace rules; execution owns recall and adoption."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from hashlib import sha256
from pathlib import PurePosixPath

from toolang.base.types.tool import ToolContext, ToolPath
from toolang.base.utils.workspace_paths import authorize_workspace_path

from ..records import RecallControlPayload
from ..types import RecallTarget, RulesRecallTarget


class _HonorRequired(Exception):
    """A prepared original call must wait for a separate honor Tool Step."""

    def __init__(self, paths: tuple[ToolPath, ...]):
        self.paths = paths
        super().__init__("workspace rules require recall")


def check_rules(
    context: ToolContext,
    paths: tuple[ToolPath, ...],
    visible: Mapping[RecallTarget, str],
    pending: Collection[RecallTarget],
) -> None:
    """Require an honor Step unless every applicable revision is visible."""

    try:
        rules = load_rules(context, paths, set(pending) | set(visible))
    except Exception as exc:
        # The honor Step owns and records rule-loading failures.
        raise _HonorRequired(paths) from exc
    if any(
        rule.target in pending or visible.get(rule.target) != rule.revision
        for rule in rules
    ):
        raise _HonorRequired(paths)


def load_rules(
    context: ToolContext,
    paths: Sequence[ToolPath],
    known: Collection[RecallTarget],
) -> tuple[RecallControlPayload, ...]:
    """Read each applicable rule once, ancestor first within its logical anchor."""

    recalled: list[RecallControlPayload] = []
    seen: set[RulesRecallTarget] = set()
    for path in paths:
        if path.workspace is None:
            continue
        root = context.workspaces[path.workspace].resolve()
        relative = PurePosixPath(path.relative)
        scopes = (
            (relative, *relative.parents)
            if path.resolved.is_dir() or not path.resolved.exists()
            else relative.parents
        )
        for scope in reversed(scopes):
            target = RulesRecallTarget(path.workspace, str(scope))
            if target in seen:
                continue
            seen.add(target)
            try:
                file = authorize_workspace_path(
                    root / str(scope).lstrip("/") / "AGENTS.md",
                    root,
                )
                content = file.read_bytes().decode("utf-8")
            except FileNotFoundError:
                if target not in known:
                    continue
                content, revision = "", "0"
            else:
                revision = sha256(content.encode("utf-8")).hexdigest()
            recalled.append(RecallControlPayload(target, revision, content))
    return tuple(recalled)
