"""Validated, versioned model catalog updates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile

import httpx

from toolang.common.files import atomic_write_text, file_write_lock

from .catalog import DEFAULT_MAX_CATALOG_BYTES, parse_model_catalog_data

DEFAULT_MODELS_DEV_URL = "https://models.dev/api.json"


@dataclass(frozen=True, slots=True)
class ModelCatalogUpdate:
    """Result of one successful or identical catalog update."""

    active: Path
    version: Path
    revision: str
    changed: bool


def download_model_catalog(
    url: str = DEFAULT_MODELS_DEV_URL,
    *,
    max_bytes: int = DEFAULT_MAX_CATALOG_BYTES,
) -> bytes:
    """Download one bounded complete catalog document."""

    with httpx.stream("GET", url, follow_redirects=True, timeout=30.0) as response:
        response.raise_for_status()
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > max_bytes:
                raise ValueError(f"downloaded model catalog exceeds {max_bytes} bytes")
            chunks.append(chunk)
    return b"".join(chunks)


def install_model_catalog(
    directory: Path,
    content: bytes,
    *,
    downloaded_at: datetime | None = None,
    max_bytes: int = DEFAULT_MAX_CATALOG_BYTES,
) -> ModelCatalogUpdate:
    """Validate, version, and atomically activate one complete catalog."""

    text, revision = _validated_content(content, max_bytes=max_bytes)
    digest = revision.removeprefix("sha256:")
    downloaded = (downloaded_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    timestamp = downloaded.strftime("%Y%m%dT%H%M%SZ")
    directory = directory.expanduser().resolve(strict=False)
    active = directory / "models.json"
    versions = directory / "models"
    filename = f"models-{timestamp}-{digest[:12]}.json"
    version = versions / filename
    with file_write_lock(directory / ".models.lock"):
        current_revision = _active_revision(active, max_bytes=max_bytes)
        if current_revision == revision and _managed_active_version(active, versions):
            current = active.resolve(strict=False) if active.is_symlink() else active
            return ModelCatalogUpdate(
                active=active,
                version=current,
                revision=revision,
                changed=False,
            )
        directory.mkdir(parents=True, exist_ok=True)
        versions.mkdir(parents=True, exist_ok=True)
        if active.exists() and not active.is_symlink() and active.is_file():
            _archive_regular_file(active, versions, max_bytes=max_bytes)
        if not version.exists():
            atomic_write_text(version, text)
        else:
            existing = version.read_bytes()
            if sha256(existing).hexdigest() != digest:
                raise FileExistsError(f"catalog version already exists: {version}")
        _activate(active, version, text)
    return ModelCatalogUpdate(
        active=active,
        version=version,
        revision=revision,
        changed=True,
    )


def _managed_active_version(active: Path, versions: Path) -> bool:
    if not active.is_symlink():
        return False
    try:
        return active.resolve(strict=True).parent == versions.resolve(strict=True)
    except OSError:
        return False


def update_model_catalog(
    directory: Path,
    *,
    url: str = DEFAULT_MODELS_DEV_URL,
) -> ModelCatalogUpdate:
    """Download and install one complete catalog update."""

    return install_model_catalog(directory, download_model_catalog(url))


def _validated_content(content: bytes, *, max_bytes: int) -> tuple[str, str]:
    if len(content) > max_bytes:
        raise ValueError(f"model catalog exceeds {max_bytes} bytes")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("model catalog must be UTF-8") from exc
    try:
        data = json.loads(text, parse_float=Decimal, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid model catalog JSON: {exc}") from exc
    parse_model_catalog_data(data)
    return text, f"sha256:{sha256(content).hexdigest()}"


def _active_revision(active: Path, *, max_bytes: int) -> str | None:
    try:
        content = active.read_bytes()
        _, revision = _validated_content(content, max_bytes=max_bytes)
        return revision
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return None


def _archive_regular_file(active: Path, versions: Path, *, max_bytes: int) -> None:
    content = active.read_bytes()
    try:
        text, revision = _validated_content(content, max_bytes=max_bytes)
    except (TypeError, ValueError):
        return
    modified = datetime.fromtimestamp(active.stat().st_mtime, timezone.utc)
    digest = revision.removeprefix("sha256:")
    archived = versions / (
        f"models-{modified.strftime('%Y%m%dT%H%M%SZ')}-{digest[:12]}.json"
    )
    if not archived.exists():
        atomic_write_text(archived, text)


def _activate(active: Path, version: Path, text: str) -> None:
    relative = version.relative_to(active.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=active.parent,
        prefix=f".{active.name}.",
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    temporary.unlink()
    try:
        temporary.symlink_to(relative)
        os.replace(temporary, active)
    except (NotImplementedError, OSError):
        temporary.unlink(missing_ok=True)
        atomic_write_text(active, text)


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON numeric constant: {value}")
