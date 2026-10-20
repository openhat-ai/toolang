"""Source version inspection reads only its required repository metadata."""

from collections.abc import Iterator
import os
from pathlib import Path
import subprocess
import sys

import pytest
from dulwich import porcelain
from dulwich.refs import Ref
from dulwich.repo import Repo

from toolang.common._version_repo import VersionRepo
from toolang.common.version import repository_source_version

AUTHOR = b"Test Author <test@example.com>"


def _commit(repository: Repo, content: str, timestamp: int) -> bytes:
    tracked = Path(repository.path) / "tracked.txt"
    tracked.write_text(content, encoding="utf-8")
    porcelain.add(repository, tracked.name)
    return porcelain.commit(
        repository,
        message=content.encode(),
        author=AUTHOR,
        committer=AUTHOR,
        author_timestamp=timestamp,
        commit_timestamp=timestamp,
        sign=False,
        no_verify=True,
    )


@pytest.fixture
def repository(tmp_path: Path) -> Iterator[Repo]:
    with Repo.init(tmp_path) as repo:
        _commit(repo, "Initial commit.\n", 1_700_000_000)
        yield repo


@pytest.mark.parametrize("annotated", [False, True])
@pytest.mark.parametrize("commits", [0, 1, 3])
@pytest.mark.parametrize("state", ["clean", "staged", "unstaged", "untracked"])
def test_version_format_and_tracked_dirty_state(
    repository: Repo, annotated: bool, commits: int, state: str
) -> None:
    porcelain.tag_create(
        repository,
        "v0.3.0",
        annotated=annotated,
        author=AUTHOR,
        message=b"Release.",
        tag_time=1_700_000_000,
        sign=False,
    )
    for index in range(commits):
        _commit(repository, f"Commit {index}.\n", 1_700_000_001 + index)
    head = repository.head().decode()
    expected = "v0.3.0" if commits == 0 else f"v0.3.0-{commits}-g{head[:8]}"
    if state == "untracked":
        (Path(repository.path) / "untracked.txt").write_text("Ignored.\n")
    elif state != "clean":
        (Path(repository.path) / "tracked.txt").write_text("Modified.\n")
        if state == "staged":
            porcelain.add(repository, "tracked.txt")
        expected += "*"

    assert repository_source_version(Path(repository.path)) == expected


@pytest.mark.parametrize("packed", [False, True])
def test_describe_exposes_only_tag_refs_and_preserves_the_real_head(
    repository: Repo, packed: bool
) -> None:
    porcelain.tag_create(repository, "nested/v0.3.0")
    head = repository.head()
    repository.refs[Ref(b"refs/remotes/origin/unrelated")] = head
    repository.refs[Ref(b"refs/codex/unrelated")] = head
    repository.refs[Ref(b"refs/tags-not-a-namespace/unrelated")] = head
    if packed:
        porcelain.pack_refs(repository, all=True)

    with VersionRepo.discover(repository.path) as snapshot:
        assert snapshot.get_refs() == {b"refs/tags/nested/v0.3.0": head}
        assert snapshot.head() == head
        assert snapshot.refs[Ref(b"refs/remotes/origin/unrelated")] == head
        assert porcelain.describe(snapshot, abbrev=8) == "v0.3.0"
    assert b"refs/remotes/origin/unrelated" in repository.get_refs()


def test_no_tag_uses_abbreviated_head_instead_of_a_branch_with_tags_in_its_name(
    repository: Repo,
) -> None:
    repository.refs[Ref(b"refs/heads/tags-are-not-a-tag")] = repository.head()

    assert repository_source_version(Path(repository.path)) == (
        "g" + repository.head().decode()[:8]
    )


def test_configuration_is_reused_only_for_one_repository_snapshot(
    repository: Repo,
) -> None:
    config = repository.get_config()
    config.set((b"version-test",), b"marker", b"before")
    config.write_to_path()

    with VersionRepo.discover(repository.path) as snapshot:
        local = snapshot.get_config()
        stack = snapshot.get_config_stack()
        assert snapshot.get_config() is local
        assert snapshot.get_config_stack() is stack
        config.set((b"version-test",), b"marker", b"after")
        config.write_to_path()
        assert snapshot.get_config().get(b"version-test", b"marker") == b"before"
        assert snapshot.get_config_stack().get(b"version-test", b"marker") == b"before"
        assert porcelain.status(snapshot, untracked_files="no").unstaged == []

    with VersionRepo.discover(repository.path) as fresh:
        assert fresh.get_config().get(b"version-test", b"marker") == b"after"
        assert fresh.get_config_stack().get(b"version-test", b"marker") == b"after"


def test_linked_worktree_keeps_its_head_and_dirty_state(
    repository: Repo, tmp_path: Path
) -> None:
    porcelain.tag_create(repository, "v0.3.0")
    worktree = tmp_path / "linked"
    porcelain.worktree_add(repository, path=worktree, detach=True)

    assert repository_source_version(worktree) == "v0.3.0"
    (worktree / "tracked.txt").write_text("Modified.\n")
    assert repository_source_version(worktree) == "v0.3.0*"
    assert repository_source_version(Path(repository.path)) == "v0.3.0"


def test_snapshot_includes_worktree_configuration(
    repository: Repo, tmp_path: Path
) -> None:
    config = repository.get_config()
    config.set((b"extensions",), b"worktreeConfig", True)
    config.set((b"version-test",), b"marker", b"shared")
    config.write_to_path()
    worktree = tmp_path / "linked"
    porcelain.worktree_add(repository, path=worktree, detach=True)
    with Repo.discover(worktree) as linked:
        local = linked.get_worktree_config()
        local.set((b"version-test",), b"worktree-only", b"worktree")
        local.write_to_path()

    with VersionRepo.discover(worktree) as snapshot:
        assert snapshot.get_config().get(b"version-test", b"marker") == b"shared"
        assert (
            snapshot.get_config_stack().get(b"version-test", b"worktree-only")
            == b"worktree"
        )


def test_shallow_history_does_not_walk_beyond_its_boundary(repository: Repo) -> None:
    porcelain.tag_create(repository, "v0.3.0")
    head = _commit(repository, "Shallow head.\n", 1_700_000_001)
    (Path(repository.controldir()) / "shallow").write_bytes(head + b"\n")

    assert repository_source_version(Path(repository.path)) == "g" + head.decode()[:8]


def test_deleted_tracked_files_still_mark_the_version_dirty(repository: Repo) -> None:
    porcelain.tag_create(repository, "v0.3.0")
    (Path(repository.path) / "tracked.txt").unlink()

    assert repository_source_version(Path(repository.path)) == "v0.3.0*"


def test_missing_repository_and_unborn_head_keep_the_unknown_fallback(
    tmp_path: Path,
) -> None:
    assert repository_source_version(tmp_path) is None
    with Repo.init(tmp_path):
        assert repository_source_version(tmp_path) is None


def test_more_help_does_not_import_the_git_version_reader() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from toolang.cli.toolang.main import main; "
            "assert main(['more']) == 0; assert 'dulwich' not in sys.modules; "
            "assert 'toolang.common._version_repo' not in sys.modules",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={
            **{
                key: value
                for key, value in os.environ.items()
                if not key.startswith("TOOLANG_")
            },
            "NO_COLOR": "1",
        },
    )

    assert result.returncode == 0, result.stderr
    assert "Language commands:" in result.stdout
