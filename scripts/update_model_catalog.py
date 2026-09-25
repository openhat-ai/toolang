"""Refresh all bundled provider models and apply maintained model preferences."""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import Any
from urllib.request import Request, urlopen

import msgspec

from toolang.common.json import dumps
from toolang.plugin.catalogs.models_dev.path import DEFAULT_MAX_CATALOG_BYTES

REPOSITORY = Path(__file__).resolve().parents[1]
PREFERENCES = REPOSITORY / "scripts/catalog-preferences.json"
OUTPUT = REPOSITORY / "src/toolang/plugin/catalogs/models_dev/data/catalog.json"
SOURCE_URL = "https://models.dev/catalog.json"


def reorder_catalog(
    data: dict[str, Any], preferences: dict[str, list[str]]
) -> dict[str, Any]:
    """Keep all selected models; only move reviewed preferences to the front."""
    if not preferences or set(data) != set(preferences):
        raise ValueError("exported provider IDs must match the non-empty preferences")
    # Canonicalize metadata and the remaining models for stable update diffs.
    result = json.loads(dumps(data))
    for provider, preferred in preferences.items():
        if not preferred or len(set(preferred)) != len(preferred):
            raise ValueError(f"{provider}: preferences must be non-empty and unique")
        models = result[provider]["models"]
        for name in preferred:
            if name not in models:
                raise ValueError(f"{provider}/{name}: preferred model missing upstream")
            model = models[name]
            if model.get("status") == "deprecated" or any(
                "text" not in model.get("modalities", {}).get(direction, [])
                for direction in ("input", "output")
            ):
                raise ValueError(
                    f"{provider}/{name}: preferred model must support text and not be deprecated"
                )
            if any(m.get("tool_call") for m in models.values()) and not model.get(
                "tool_call"
            ):
                raise ValueError(f"{provider}/{name}: prefer a model with tool calls")
        result[provider]["models"] = {
            name: models[name]
            for name in (
                *preferred,
                *(name for name in models if name not in preferred),
            )
        }
    return result


def export_catalog(source: Path, providers: list[str], root: Path) -> dict[str, Any]:
    """Use the public provider-only CLI query with an isolated configuration."""
    command = [
        sys.executable,
        "-m",
        "toolang.cli.toolang.main",
        "--root",
        str(root),
        "models",
        "--catalog",
        str(source),
        "--all",
        "--json",
    ]
    for provider in providers:
        command.extend(("-q", f"{provider}/*"))
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env={"PATH": os.defpath, "TOOLANG_TMUX": "0"},
    )
    if result.returncode:
        raise ValueError(result.stderr.strip() or "catalog export failed")
    return json.loads(result.stdout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        help="Use a saved upstream snapshot instead of downloading",
    )
    parser.add_argument("--preferences", type=Path, default=PREFERENCES)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument(
        "--snapshot-date",
        type=date.fromisoformat,
        help="Source snapshot date (YYYY-MM-DD); required for an unrecorded local source",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail if the output would change; do not write",
    )
    args = parser.parse_args(argv)
    try:
        preferences = msgspec.json.decode(
            args.preferences.read_bytes(), type=dict[str, list[str]]
        )
        if not preferences:
            raise ValueError("preferences must contain at least one provider")
        with TemporaryDirectory(prefix="toolang-catalog-") as temporary:
            directory = Path(temporary)
            source = (
                args.source.resolve() if args.source else directory / "upstream.json"
            )
            if args.source is None:
                request = Request(
                    SOURCE_URL, headers={"User-Agent": "toolang-catalog-updater"}
                )
                with urlopen(request, timeout=60) as response:
                    payload = response.read(DEFAULT_MAX_CATALOG_BYTES + 1)
                if len(payload) > DEFAULT_MAX_CATALOG_BYTES:
                    raise ValueError("upstream catalog exceeds the loader size limit")
                source.write_bytes(payload)
            exported = export_catalog(source, list(preferences), directory / "root")
            data = reorder_catalog(exported, preferences)
            metadata = snapshot_metadata(
                source_sha256=sha256(source.read_bytes()).hexdigest(),
                snapshot_date=args.snapshot_date,
                output=args.output,
                downloaded=args.source is None,
            )
            content = dumps({"_meta": metadata, **data}, sort_keys=False)
            if len(content.encode("utf-8")) > DEFAULT_MAX_CATALOG_BYTES:
                raise ValueError("generated catalog exceeds the loader size limit")
            if args.check:
                if not args.output.exists() or args.output.read_text() != content:
                    parser.exit(1, "bundled catalog needs updating\n")
            else:
                # Validate everything before replacing an existing bundled catalog.
                with NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=args.output.parent, delete=False
                ) as output:
                    temporary_output = Path(output.name)
                    try:
                        output.write(content)
                        output.close()
                        temporary_output.replace(args.output)
                    finally:
                        temporary_output.unlink(missing_ok=True)
            print(
                f"{len(data)} providers, {sum(len(p['models']) for p in data.values())} models; snapshot: {metadata['snapshot_date']}; source SHA-256: {metadata['source_sha256']}"
            )
    except (OSError, ValueError, msgspec.DecodeError) as error:
        parser.error(str(error))
    return 0


def snapshot_metadata(
    *,
    source_sha256: str,
    snapshot_date: date | None,
    output: Path,
    downloaded: bool,
) -> dict[str, str]:
    """Retain a known snapshot date; never guess the age of a local source."""
    if snapshot_date is None and output.is_file():
        existing = json.loads(output.read_text())
        metadata = existing.get("_meta") if isinstance(existing, dict) else None
        if (
            isinstance(metadata, dict)
            and metadata.get("source_sha256") == source_sha256
        ):
            snapshot_date = date.fromisoformat(str(metadata.get("snapshot_date", "")))
    if snapshot_date is None:
        if not downloaded:
            raise ValueError(
                "--snapshot-date is required for an unrecorded local source"
            )
        snapshot_date = datetime.now(UTC).date()
    return {
        "snapshot_date": snapshot_date.isoformat(),
        "source_url": SOURCE_URL,
        "source_sha256": source_sha256,
    }


if __name__ == "__main__":
    raise SystemExit(main())
