from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]


def main() -> int:
    reference_dir = Path(__file__).resolve().parent
    repo_root = reference_dir.parent
    config = tomllib.loads((reference_dir / "config.toml").read_text(encoding="utf-8"))

    modules = config["modules"]
    output_directory = reference_dir / config["output_directory"]
    template_directory = config.get("template_directory")
    edit_urls: dict[str, str] = config.get("edit_url", {})

    if output_directory.exists():
        shutil.rmtree(output_directory)
    output_directory.mkdir(parents=True, exist_ok=True)

    command = [
        sys.executable,
        "-m",
        "pdoc",
        "--output-directory",
        str(output_directory),
        "--docformat",
        str(config.get("docformat", "google")),
        "--search" if bool(config.get("search", True)) else "--no-search",
        "--show-source" if bool(config.get("show_source", True)) else "--no-show-source",
        (
            "--include-undocumented"
            if bool(config.get("include_undocumented", False))
            else "--no-include-undocumented"
        ),
    ]
    if template_directory:
        command.extend(
            ["--template-directory", str(reference_dir / str(template_directory))]
        )
    for module_name, url in edit_urls.items():
        command.extend(["--edit-url", f"{module_name}={url}"])
    command.extend(str(module) for module in modules)

    subprocess.run(command, cwd=repo_root, check=True)
    print(f"Generated reference docs in {output_directory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
