"""GitHub-backed capability ref resolution and artifact fetching."""

from __future__ import annotations

import tarfile
import tempfile
from io import BytesIO
from pathlib import Path

import httpx

from toolang.errors import ToolangError
from toolang.concepts.caps import CapKind, CapRef

CAP_REPO_CANDIDATES: dict[CapKind, tuple[tuple[str, str, str | None], ...]] = {
    "skill": (
        ("agent-skills", "skills/{name}", "SKILL.md"),
        ("skills", "{name}", "SKILL.md"),
    ),
    "service": (
        ("agent-services", "services/{name}.md", None),
        ("services", "{name}.md", None),
    ),
    "prompt": (
        ("agent-prompts", "prompts/{name}.md", None),
        ("prompts", "{name}.md", None),
    ),
    "psyche": (
        ("agent-psyches", "psyches/{name}.md", None),
        ("psyches", "{name}.md", None),
    ),
}


def resolve_github_cap_ref(kind: CapKind, ref: str) -> CapRef:
    owner, name = _parse_cap_ref(ref)
    with httpx.Client(
        follow_redirects=True,
        headers={"User-Agent": "toolang-sync"},
        timeout=20.0,
    ) as client:
        for repo_name, path_template, required_child in CAP_REPO_CANDIDATES[kind]:
            repo = f"{owner}/{repo_name}"
            default_branch = _repo_default_branch(client, repo)
            if default_branch is None:
                continue
            rev = _repo_branch_rev(client, repo, default_branch)
            path = path_template.format(name=name)
            check_path = f"{path}/{required_child}" if required_child is not None else path
            if _github_file_exists(client, repo, rev, check_path):
                return CapRef(
                    kind=kind,
                    name=name,
                    ref=ref,
                    repo=repo,
                    path=path,
                    rev=rev,
                )
    raise ToolangError(f"{kind.title()} ref could not be resolved from GitHub: {ref}")


def resolve_github_skill_ref(ref: str) -> CapRef:
    return resolve_github_cap_ref("skill", ref)


def fetch_github_artifact(resolved: CapRef) -> tuple[Path, list[str]]:
    archive = _download_repo_archive(resolved.repo, resolved.rev)
    temp_root = Path(tempfile.mkdtemp(prefix="toolang-cap-tree-"))
    _extract_repo_archive(archive, temp_root)
    source_path = _extracted_source_path(temp_root, resolved.path)
    if not source_path.exists():
        raise ToolangError(
            f"Resolved cap path was not found in the fetched archive: {resolved.repo}@{resolved.rev}:{resolved.path}"
        )
    materialized_root = temp_root / "materialized"
    files: list[str] = []
    if source_path.is_dir():
        materialized_path = materialized_root / resolved.name
        for source in source_path.rglob("*"):
            if not source.is_file():
                continue
            relative = source.relative_to(source_path)
            target = materialized_path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
            files.append(str(relative))
        return materialized_path, sorted(files)

    materialized_path = materialized_root / source_path.name
    materialized_path.parent.mkdir(parents=True, exist_ok=True)
    materialized_path.write_bytes(source_path.read_bytes())
    files.append(materialized_path.name)
    return materialized_path, files


def fetch_github_tree(resolved: CapRef) -> tuple[Path, list[str]]:
    return fetch_github_artifact(resolved)


def _parse_cap_ref(ref: str) -> tuple[str, str]:
    owner, sep, name = ref.partition("/")
    if not owner or not sep or not name:
        raise ToolangError(f"Capability ref must look like owner/name: {ref}")
    return owner, name


def _repo_default_branch(client: httpx.Client, repo: str) -> str | None:
    response = client.get(f"https://api.github.com/repos/{repo}")
    if response.status_code == 404:
        return None
    response.raise_for_status()
    payload = response.json()
    branch = payload.get("default_branch")
    return branch if isinstance(branch, str) and branch else None


def _repo_branch_rev(client: httpx.Client, repo: str, branch: str) -> str:
    response = client.get(f"https://api.github.com/repos/{repo}/branches/{branch}")
    response.raise_for_status()
    payload = response.json()
    commit = payload.get("commit") or {}
    sha = commit.get("sha")
    if not isinstance(sha, str) or not sha:
        raise ToolangError(f"GitHub branch response did not contain a commit sha: {repo}@{branch}")
    return sha


def _github_file_exists(client: httpx.Client, repo: str, rev: str, path: str) -> bool:
    response = client.get(f"https://raw.githubusercontent.com/{repo}/{rev}/{path}")
    return response.status_code == 200


def _download_repo_archive(repo: str, rev: str) -> bytes:
    with httpx.Client(follow_redirects=True, headers={"User-Agent": "toolang-sync"}, timeout=30.0) as client:
        response = client.get(f"https://github.com/{repo}/archive/{rev}.tar.gz")
        response.raise_for_status()
        return response.content


def _extract_repo_archive(archive: bytes, root: Path) -> None:
    with tarfile.open(fileobj=BytesIO(archive), mode="r:gz") as tar:
        tar.extractall(root)


def _extracted_source_path(root: Path, path: str) -> Path:
    extracted_roots = [item for item in root.iterdir() if item.is_dir()]
    if len(extracted_roots) != 1:
        raise ToolangError("Expected exactly one extracted repository root.")
    return extracted_roots[0] / path
