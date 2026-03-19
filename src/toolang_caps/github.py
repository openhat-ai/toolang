from __future__ import annotations

import tarfile
import tempfile
from io import BytesIO
from pathlib import Path

import httpx

from toolang.errors import ToolangError
from toolang_caps.models import ResolvedCapRef

SKILL_REPO_CANDIDATES = (
    ("agent-skills", "skills/{name}"),
    ("skills", "{name}"),
)


def resolve_github_skill_ref(ref: str) -> ResolvedCapRef:
    owner, name = _parse_cap_ref(ref)
    with httpx.Client(
        follow_redirects=True,
        headers={"User-Agent": "toolang-sync"},
        timeout=20.0,
    ) as client:
        for repo_name, path_template in SKILL_REPO_CANDIDATES:
            repo = f"{owner}/{repo_name}"
            default_branch = _repo_default_branch(client, repo)
            if default_branch is None:
                continue
            rev = _repo_branch_rev(client, repo, default_branch)
            path = path_template.format(name=name)
            if _github_file_exists(client, repo, rev, f"{path}/SKILL.md"):
                return ResolvedCapRef(
                    kind="skill",
                    name=name,
                    ref=ref,
                    repo=repo,
                    path=path,
                    rev=rev,
                )
    raise ToolangError(f"Skill ref could not be resolved from GitHub: {ref}")


def fetch_github_tree(resolved: ResolvedCapRef) -> tuple[Path, list[str]]:
    archive = _download_repo_archive(resolved.repo, resolved.rev)
    temp_root = Path(tempfile.mkdtemp(prefix="toolang-cap-tree-"))
    _extract_repo_archive(archive, temp_root)
    source_dir = _extracted_source_dir(temp_root, resolved.path)
    if not source_dir.exists():
        raise ToolangError(
            f"Resolved cap path was not found in the fetched archive: {resolved.repo}@{resolved.rev}:{resolved.path}"
        )
    materialized_root = temp_root / "materialized" / resolved.name
    files: list[str] = []
    for source in source_dir.rglob("*"):
        if not source.is_file():
            continue
        relative = source.relative_to(source_dir)
        target = materialized_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        files.append(str(relative))
    return materialized_root, sorted(files)


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


def _extracted_source_dir(root: Path, path: str) -> Path:
    extracted_roots = [item for item in root.iterdir() if item.is_dir()]
    if len(extracted_roots) != 1:
        raise ToolangError("Expected exactly one extracted repository root.")
    return extracted_roots[0] / path
