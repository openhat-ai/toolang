"""Opt-in live comparison of two installed Toolang checkouts (never run by pytest).

Example:
    python scripts/benchmark_web_search.py --before /path/to/before/.venv/bin/python \
        --after /path/to/after/.venv/bin/python --output /tmp/web-search.jsonl

Uses public queries only. Alternates order, retains failures, and spaces calls.
The caller must install each checkout's locked environment first.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
from pathlib import Path
import subprocess
import time
from typing import Any
from urllib.parse import urlparse

CASES = (
    (
        "fly",
        {"query": "Fly.io official documentation Machines deployments"},
        ["fly.io"],
    ),
    (
        "fly-domain",
        {
            "query": "Fly.io official documentation Machines deployments",
            "domains": ["fly.io"],
        },
        ["fly.io"],
    ),
    (
        "python",
        {"query": "Python asyncio documentation", "domains": ["docs.python.org"]},
        ["docs.python.org"],
    ),
    (
        "sqlite",
        {"query": "SQLite write ahead logging official documentation"},
        ["sqlite.org"],
    ),
    (
        "chinese",
        {"query": "Python 异步编程 asyncio 官方文档", "domains": ["docs.python.org"]},
        ["docs.python.org"],
    ),
    (
        "multi-domain",
        {
            "query": "deploy web application documentation",
            "domains": ["fly.io", "render.com"],
        },
        ["fly.io", "render.com"],
    ),
)


def worker(arguments: str) -> None:
    from toolang.base.types.tool import ToolContext
    from toolang.plugin.toolsets.web import create_toolset

    async def search() -> None:
        started = time.monotonic()
        try:
            result = (
                await create_toolset({})
                .tools()["search"]
                .invoke(json.loads(arguments), ToolContext(Path.cwd(), Path.cwd()))
            )
            output, error = result.output, result.error
        except Exception as exc:
            output, error = {}, f"{type(exc).__name__}: {exc}"
        print(
            json.dumps(
                {
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                    "ddgs": importlib.metadata.version("ddgs"),
                    "output": output,
                    "error": error,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    asyncio.run(search())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before")
    parser.add_argument("--after")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--pause", type=float, default=2)
    parser.add_argument("--worker", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        worker(args.worker)
        return
    if (
        not args.before
        or not args.after
        or not args.output
        or args.rounds < 1
        or args.pause < 0
    ):
        parser.error(
            "provide --before, --after, --output, positive rounds, and a non-negative pause"
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as stream:
        for repeat in range(args.rounds):
            for index, (name, arguments, expected) in enumerate(CASES):
                versions = [("before", args.before), ("after", args.after)]
                if (repeat + index) % 2:
                    versions.reverse()
                for version, python in versions:
                    row: dict[str, Any]
                    try:
                        completed = subprocess.run(
                            [
                                python,
                                str(Path(__file__).resolve()),
                                "--worker",
                                json.dumps(arguments),
                            ],
                            capture_output=True,
                            text=True,
                            timeout=25,
                            check=True,
                        )
                        row = json.loads(completed.stdout)
                    except (subprocess.SubprocessError, ValueError) as exc:
                        row = {"output": {}, "error": str(exc), "elapsed_ms": None}
                    results = row["output"].get("results", [])
                    official = sum(
                        any(
                            (urlparse(result["url"]).hostname or "").rstrip(".")
                            == domain
                            or (urlparse(result["url"]).hostname or "")
                            .rstrip(".")
                            .endswith("." + domain)
                            for domain in expected
                        )
                        for result in results
                    )
                    row.update(
                        case=name,
                        round=repeat + 1,
                        version=version,
                        arguments=arguments,
                        result_count=len(results),
                        expected_domain_hits=official,
                    )
                    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                    stream.flush()
                    print(
                        json.dumps(
                            {
                                key: row[key]
                                for key in (
                                    "case",
                                    "round",
                                    "version",
                                    "elapsed_ms",
                                    "result_count",
                                    "expected_domain_hits",
                                    "error",
                                )
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    time.sleep(args.pause)


if __name__ == "__main__":
    main()
