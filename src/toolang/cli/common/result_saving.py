"""Shared serialization and destination handling for durable Run results."""

from __future__ import annotations

from collections.abc import Sequence
import json
from pathlib import Path
from typing import TextIO

from toolang.base.types.message import Part, TextPart, message_text, parts_to_data
from toolang.common.files import atomic_write_text


def serialize_result(parts: Sequence[Part]) -> str:
    """Serialize one durable Run result without adding presentation whitespace."""

    if all(isinstance(part, TextPart) for part in parts):
        return message_text(parts)
    return json.dumps(
        parts_to_data(parts),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def save_result(
    parts: Sequence[Part],
    destination: str,
    *,
    stdout: TextIO,
) -> None:
    """Write one serialized Run result to stdout or an atomic file destination."""

    content = serialize_result(parts)
    if destination == "-":
        try:
            stdout.write(content)
            stdout.flush()
        except OSError as exc:
            raise OSError(f"could not save result to stdout: {exc}") from exc
        return

    path = Path(destination).expanduser()
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"result destination is not a regular file: {path}")
    if not path.parent.exists():
        raise ValueError(f"result destination parent does not exist: {path.parent}")
    if not path.parent.is_dir():
        raise ValueError(f"result destination parent is not a directory: {path.parent}")
    try:
        atomic_write_text(path, content)
    except OSError as exc:
        raise OSError(f"could not save result to {path}: {exc}") from exc
