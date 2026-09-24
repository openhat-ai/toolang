"""Portable cache document envelope and shared cache helpers."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from hashlib import sha256
from pathlib import Path
from typing import Any

import msgspec

from toolang.common.files import atomic_write_text, file_write_lock
from toolang.common.json import dumps

CACHE_SCHEMA = 10
_MAX_CACHE_BYTES = 128 * 1024 * 1024
_REVISION_RE = re.compile(r"^sha256:([0-9a-f]{64})$")
_SENSITIVE_HEADER_NAME_RE = re.compile(
    r"(?:authorization|cookie|credential|key|secret|token)",
    re.IGNORECASE,
)
_SECRET_VALUE_RE = re.compile(
    r'(?:bearer\s+[A-Za-z0-9._~+/-]{8,}|https?://[^/\s"@:]+:[^@\s"]+@|'
    r"[?&](?:api[-_]?key|access[-_]?token|credential|secret|token)="
    r'[^&\s"]+)',
    re.IGNORECASE,
)
_SECRET_FIELD_MARKERS = (
    '"api_key"',
    '"api-key"',
    '"apikey"',
    '"authorization"',
    '"cookie"',
    '"credential"',
    '"credentials"',
    '"header"',
    '"password"',
    '"proxy_authorization"',
    '"proxy-authorization"',
    '"secret"',
    '"token"',
    '"x_api_key"',
    '"x-api-key"',
    '_password"',
    '_key"',
    '_secret"',
    '_token"',
    '-password"',
    '-key"',
    '-secret"',
    '-token"',
)
_SECRET_FIELD_RE = re.compile(
    r'"(?:api[-_]?key|apikey|authorization|cookie|credential|credentials|'
    r'header|password|proxy[-_]?authorization|secret|token|x[-_]?api[-_]?key)"\s*:',
    re.IGNORECASE,
)


def store_document(
    path: Path,
    *,
    kind: str,
    key: str,
    document: Mapping[str, object],
) -> bool:
    """Write a canonical document; return False for unsafe or oversized payloads."""

    payload = {
        "schema": CACHE_SCHEMA,
        "kind": kind,
        "key": key,
        **document,
    }
    payload_content = dumps(payload, indent=None)
    checksum = text_digest(payload_content)
    content = f'{{"digest":"{checksum}","payload":{payload_content}}}'
    if len(content.encode("utf-8")) > _MAX_CACHE_BYTES:
        return False
    if _serialized_data_is_unsafe(content, payload):
        return False
    with file_write_lock(path.with_name(f".{path.name}.lock")):
        atomic_write_text(path, content)
    return True


def load_document(
    path: Path,
    *,
    kind: str,
    key: str,
    fast_json: bool = False,
) -> dict[str, object]:
    """Read and validate one canonical cache document."""

    if path.stat().st_size > _MAX_CACHE_BYTES:
        raise ValueError("model cache entry exceeds its size limit")
    content = path.read_text(encoding="utf-8")
    del fast_json  # All cache readers now use the same native decoder.
    raw = msgspec.json.decode(content)
    if not isinstance(raw, Mapping):
        raise TypeError("model cache entry must be an object")
    if _serialized_data_is_unsafe(content, raw):
        raise ValueError("model cache entry contains unsafe data")
    require_fields(raw, frozenset({"digest", "payload"}), label="cache envelope")
    checksum = raw.get("digest")
    payload = raw.get("payload")
    if not isinstance(checksum, str) or not isinstance(payload, Mapping):
        raise TypeError("model cache entry envelope is invalid")
    prefix = f'{{"digest":"{checksum}","payload":'
    if not content.startswith(prefix) or not content.endswith("}"):
        raise ValueError("model cache entry envelope is not canonical")
    payload_content = content[len(prefix) : -1]
    if checksum != text_digest(payload_content):
        raise ValueError("model cache entry digest does not match its payload")
    document = {str(name): value for name, value in payload.items()}
    if (
        document.get("schema") != CACHE_SCHEMA
        or document.get("kind") != kind
        or document.get("key") != key
    ):
        raise ValueError("model cache entry identity does not match its path")
    return document


def digest(value: object) -> str:
    """Return a portable content digest for any canonical value."""

    return text_digest(dumps(canonical_value(value), indent=None))


def text_digest(value: str) -> str:
    """Return a ``sha256:`` digest for one text payload."""

    return f"sha256:{sha256(value.encode('utf-8')).hexdigest()}"


def revision_hex(value: str) -> str:
    """Return the hex body of one ``sha256:`` revision."""

    match = _REVISION_RE.fullmatch(value)
    if match is None:
        raise ValueError(f"invalid model cache revision: {value!r}")
    return match.group(1)


def canonical_value(value: object) -> object:
    """Return a deterministic, JSON-safe projection of a cache identity value."""

    if isinstance(value, Mapping):
        return {
            str(key): canonical_value(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [canonical_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return repr(value)


def require_fields(
    data: Mapping[Any, object],
    expected: frozenset[str],
    *,
    label: str,
) -> None:
    """Require one decoded object to contain exactly the expected field names."""

    actual = {str(name) for name in data}
    if actual != expected:
        raise ValueError(f"{label} fields do not match schema")


def _serialized_data_is_unsafe(
    content: str,
    parsed: object | None = None,
) -> bool:
    if _SECRET_FIELD_RE.search(content) is not None:
        return True
    if _SECRET_VALUE_RE.search(content) is not None or _contains_url_userinfo(content):
        return True
    return _contains_unsafe_headers(parsed) if parsed is not None else False


def _json_field_occurs(content: str, marker: str) -> bool:
    start = 0
    while (index := content.find(marker, start)) >= 0:
        cursor = index + len(marker)
        while cursor < len(content) and content[cursor] in " \t\r\n":
            cursor += 1
        if cursor < len(content) and content[cursor] == ":":
            return True
        start = index + 1
    return False


def _contains_url_userinfo(content: str) -> bool:
    start = 0
    while (scheme := content.find("://", start)) >= 0:
        end = content.find('"', scheme)
        if end < 0:
            return True
        authority_end = content.find("/", scheme + 3, end)
        if authority_end < 0:
            authority_end = end
        if "@" in content[scheme + 3 : authority_end]:
            return True
        start = end + 1
    return False


def _contains_unsafe_headers(value: object) -> bool:
    if isinstance(value, Mapping):
        for raw_name, item in value.items():
            if isinstance(raw_name, str) and raw_name.casefold() == "headers":
                if not isinstance(item, Mapping) or any(
                    not isinstance(header_name, str)
                    or not isinstance(header_value, str)
                    or _SENSITIVE_HEADER_NAME_RE.search(header_name) is not None
                    or _SECRET_VALUE_RE.search(header_value) is not None
                    for header_name, header_value in item.items()
                ):
                    return True
                continue
            if isinstance(item, Mapping | list | tuple) and _contains_unsafe_headers(
                item
            ):
                return True
        return False
    if isinstance(value, list | tuple):
        return any(
            _contains_unsafe_headers(item)
            for item in value
            if isinstance(item, Mapping | list | tuple)
        )
    return False
