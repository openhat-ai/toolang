"""Exercise CI change routing and the required gate with real shell steps."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest
import yaml


WORKFLOW = yaml.safe_load(
    (Path(__file__).parents[2] / ".github/workflows/ci.yml").read_text()
)


def run_step(
    job: str, name: str, root: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    step = next(step for step in WORKFLOW["jobs"][job]["steps"] if step["name"] == name)
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
        cwd=root,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=10,
    )


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args],
        cwd=root,
        text=True,
        stderr=subprocess.PIPE,
    ).strip()


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "CI Test")
    git(root, "config", "user.email", "ci@example.invalid")
    plan = root / "docs/plans/plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("Existing plan.\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "docs: add plan")
    return root, git(root, "rev-parse", "HEAD")


def change_env(root: Path, base: str, event: str = "pull_request") -> dict[str, str]:
    return {
        "GITHUB_EVENT_NAME": event,
        "BASE_SHA": base,
        "HEAD_SHA": git(root, "rev-parse", "HEAD"),
        "RUNNER_TEMP": str(root.parent),
        "GITHUB_OUTPUT": str(root.parent / "outputs"),
    }


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        (["docs/plans/new.md"], True),
        (["docs/plans/with space\nand newline.md"], True),
        (["docs/plans/new.md", "src/module.py"], False),
        (["src/prompts/instructions.md"], False),
        (["docs/usage.md"], False),
        (["docs/plans/config.yml"], False),
        ([".github/workflows/ci.yml"], False),
        ([], False),
    ],
)
def test_change_routing(
    repository: tuple[Path, str], paths: list[str], expected: bool
) -> None:
    root, base = repository
    for path in paths:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("New content.\n")
    git(root, "add", ".")
    git(root, "commit", "--allow-empty", "-qm", "test: change files")
    env = change_env(root, base)
    result = run_step("changes", "Identify plan-only pull requests", root, env)
    assert result.returncode == 0, result.stderr
    assert (
        Path(env["GITHUB_OUTPUT"]).read_text() == f"docs_only={str(expected).lower()}\n"
    )


@pytest.mark.parametrize("rename", [False, True])
def test_deleted_or_renamed_plan(repository: tuple[Path, str], rename: bool) -> None:
    root, base = repository
    if rename:
        git(root, "mv", "docs/plans/plan.md", "runtime.md")
    else:
        git(root, "rm", "docs/plans/plan.md")
    git(root, "commit", "-qm", "docs: move or remove plan")
    env = change_env(root, base)
    result = run_step("changes", "Identify plan-only pull requests", root, env)
    assert result.returncode == 0, result.stderr
    assert (
        Path(env["GITHUB_OUTPUT"]).read_text()
        == f"docs_only={str(not rename).lower()}\n"
    )


def test_change_routing_uses_merge_base(repository: tuple[Path, str]) -> None:
    root, base = repository
    (root / "module.py").write_text("value = 1\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "feat: advance base")
    advanced_base = git(root, "rev-parse", "HEAD")
    git(root, "checkout", "--detach", base)
    (root / "docs/plans/plan.md").write_text("Updated plan.\n")
    git(root, "commit", "-am", "docs: update plan")
    env = change_env(root, advanced_base)
    result = run_step("changes", "Identify plan-only pull requests", root, env)
    assert result.returncode == 0, result.stderr
    assert Path(env["GITHUB_OUTPUT"]).read_text() == "docs_only=true\n"


@pytest.mark.parametrize("event", ["pull_request", "push", "workflow_dispatch"])
def test_missing_history_fails_closed(tmp_path: Path, event: str) -> None:
    env = {
        "GITHUB_EVENT_NAME": event,
        "BASE_SHA": "missing-base",
        "HEAD_SHA": "missing-head",
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(tmp_path / "outputs"),
    }
    result = run_step("changes", "Identify plan-only pull requests", tmp_path, env)
    if event == "pull_request":
        assert result.returncode != 0
        assert not Path(env["GITHUB_OUTPUT"]).exists()
    else:
        assert result.returncode == 0, result.stderr
        assert Path(env["GITHUB_OUTPUT"]).read_text() == "docs_only=false\n"


def test_plan_formatting_rejects_whitespace_errors(
    repository: tuple[Path, str],
) -> None:
    root, base = repository
    (root / "docs/plans/plan.md").write_text("Trailing whitespace. \n")
    git(root, "commit", "-am", "docs: update plan")
    result = run_step("changes", "Check plan formatting", root, change_env(root, base))
    assert result.returncode != 0
    assert "trailing whitespace" in result.stdout


def gate_env(docs_only: str, event: str = "pull_request") -> dict[str, str]:
    expected = "skipped" if docs_only == "true" else "success"
    return {
        "GITHUB_EVENT_NAME": event,
        "DOCS_ONLY": docs_only,
        "CHANGES_RESULT": "success",
        "QUALITY_RESULT": expected,
        "TESTS_RESULT": expected,
        "PACKAGE_RESULT": expected,
    }


@pytest.mark.parametrize(
    "event,docs_only",
    [
        ("pull_request", "false"),
        ("pull_request", "true"),
        ("push", "false"),
        ("workflow_dispatch", "false"),
    ],
)
def test_gate_accepts_successful_jobs(
    tmp_path: Path, event: str, docs_only: str
) -> None:
    result = run_step(
        "gate",
        "Require the expected CI jobs to pass",
        tmp_path,
        gate_env(docs_only, event),
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "docs_only,job,job_result",
    [
        pytest.param(docs_only, job, result, id=f"docs-{docs_only}-{job}-{result}")
        for docs_only in ("false", "true")
        for job in ("changes", "quality", "tests", "package")
        for result in ("success", "failure", "cancelled", "skipped")
        if result
        != ("success" if job == "changes" or docs_only == "false" else "skipped")
    ],
)
def test_gate_rejects_each_unexpected_job_result(
    tmp_path: Path, docs_only: str, job: str, job_result: str
) -> None:
    env = {**gate_env(docs_only), f"{job.upper()}_RESULT": job_result}
    result = run_step("gate", "Require the expected CI jobs to pass", tmp_path, env)
    assert result.returncode != 0


@pytest.mark.parametrize(
    "event,docs_only",
    [("push", "true"), ("workflow_dispatch", "true"), ("pull_request", "")],
)
def test_gate_rejects_invalid_change_classification(
    tmp_path: Path, event: str, docs_only: str
) -> None:
    result = run_step(
        "gate",
        "Require the expected CI jobs to pass",
        tmp_path,
        gate_env(docs_only, event),
    )
    assert result.returncode != 0
