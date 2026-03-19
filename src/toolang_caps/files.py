from __future__ import annotations

import shutil
from pathlib import Path

from toolang_caps.models import CAP_KINDS, SyncedCap


def sync_caps_tree(root: Path, caps: list[SyncedCap]) -> None:
    expected: dict[Path, SyncedCap] = {}

    root.mkdir(parents=True, exist_ok=True)
    for kind in CAP_KINDS:
        (root / kind).mkdir(parents=True, exist_ok=True)

    for cap in caps:
        path = _cap_path(root, cap)
        expected[path] = cap
        path.write_text(cap.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")

    for kind in CAP_KINDS:
        kind_dir = root / kind
        for existing in kind_dir.iterdir():
            if existing not in expected:
                _remove_path(existing)


def _cap_path(root: Path, cap: SyncedCap) -> Path:
    return root / cap.kind / f"{cap.name}.json"


def _remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
        return
    path.unlink()
