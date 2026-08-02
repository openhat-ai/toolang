"""Filesystem-backed Content include resolution."""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

from toolang.base.errors import ToolangError
from toolang.base.types.message import (
    AudioPart,
    DocumentPart,
    ImagePart,
    PerceptPart,
    TextPart,
)

_TEXT_MEDIA_TYPES = frozenset(
    {
        "application/javascript",
        "application/json",
        "application/toml",
        "application/xml",
        "application/yaml",
        "application/x-ndjson",
        "application/x-sh",
        "application/x-yaml",
    }
)
_DOCUMENT_EXTENSIONS = frozenset(
    {
        ".csv",
        ".doc",
        ".docx",
        ".html",
        ".json",
        ".md",
        ".pdf",
        ".ppt",
        ".pptx",
        ".rtf",
        ".txt",
        ".xls",
        ".xlsx",
        ".xml",
    }
)


def resolve_file_include(reference: str, *, base: Path) -> PerceptPart:
    """Resolve one local Content reference relative to an explicit base."""

    path = Path(reference).expanduser()
    if not path.is_absolute():
        path = base / path
    path = path.resolve()
    if not path.is_file():
        raise ToolangError(f"included file not found: {reference}")
    media_type, _encoding = mimetypes.guess_type(path.name)
    if _is_text(media_type):
        try:
            return TextPart(path.read_text(encoding="utf-8"))
        except UnicodeDecodeError as exc:
            raise ToolangError(
                f"included text is not UTF-8: {reference}"
            ) from exc
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    if media_type is not None and media_type.startswith("image/"):
        return ImagePart(
            image_url=_data_url(media_type, encoded),
            filename=path.name,
            media_type=media_type,
        )
    if media_type in {"audio/mpeg", "audio/mp3"}:
        return AudioPart(
            data=encoded,
            format="mp3",
            filename=path.name,
            media_type=media_type,
        )
    if media_type in {"audio/wav", "audio/x-wav"}:
        return AudioPart(
            data=encoded,
            format="wav",
            filename=path.name,
            media_type=media_type,
        )
    if path.suffix.lower() in _DOCUMENT_EXTENSIONS:
        return DocumentPart(
            data=_data_url(media_type or "application/octet-stream", encoded),
            filename=path.name,
            media_type=media_type,
        )
    raise ToolangError(f"unsupported included file: {reference}")


def _is_text(media_type: str | None) -> bool:
    return bool(
        media_type
        and (
            media_type.startswith("text/")
            or media_type in _TEXT_MEDIA_TYPES
        )
    )


def _data_url(media_type: str, encoded: str) -> str:
    return f"data:{media_type};base64,{encoded}"
