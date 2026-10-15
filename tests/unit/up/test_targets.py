"""Unit tests for agent selector and remote-source handling."""

from __future__ import annotations

from errno import ENOENT
import os
from pathlib import Path
import subprocess
import sys

import pytest

from toolang.up import source as agent_sources

from toolang.base.types.progress import ProgressEvent
from toolang.common.layout import AgentLayout
from toolang.common.github import GitHubRef


def test_agent_selector_parsing_supports_name_shorthand_and_ref() -> None:
    local = agent_sources.parse_agent_target("alice")
    explicit_local = agent_sources.parse_agent_target("agent:alice")
    github_short = agent_sources.parse_agent_target("gh:brice/alice")
    host_short = agent_sources.parse_agent_target("https://toolang.ai/alice.too")
    github_ref = agent_sources.parse_agent_target(
        "github://brice/agents/team/alice.too@main"
    )

    assert local.form == "name"
    assert local.name == "alice"
    assert explicit_local.form == "name"
    assert explicit_local.name == "alice"
    assert github_short.github_owner == "brice"
    assert github_short.name == "alice"
    assert host_short.resolved_ref().render() == "https://toolang.ai/alice.too"
    assert github_ref.resolved_ref().render() == (
        "github://brice/agents/team/alice.too@main"
    )


def test_agent_selector_parsing_supports_repo_shorthand() -> None:
    selector = agent_sources.parse_agent_target("gh:brice/project/alice")

    assert selector.github_owner == "brice"
    assert selector.github_repo == "project"
    assert selector.name == "alice"


def test_agent_selector_canonicalizes_raw_refs_heads_url() -> None:
    selector = agent_sources.parse_agent_target(
        "https://raw.githubusercontent.com/briceyan/agents/refs/heads/main/dev.too"
    )

    assert selector.resolved_ref().render() == (
        "github://briceyan/agents/dev.too@refs/heads/main"
    )


def test_agent_shorthand_falls_back_to_main_when_branch_probe_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probes: list[str] = []
    monkeypatch.setattr(
        agent_sources,
        "_github_repo_default_branch",
        lambda _owner, _repo: (_ for _ in ()).throw(ValueError("rate limited")),
    )

    def fake_exists(ref: GitHubRef) -> bool:
        probes.append(ref.render())
        return ref.path == "dev.too"

    monkeypatch.setattr(agent_sources, "_github_agent_ref_exists", fake_exists)

    ref = agent_sources.resolve_agent_source(
        agent_sources.parse_agent_target("gh:briceyan/dev")
    )

    assert ref.render() == "github://briceyan/agents/dev.too@main"
    assert probes == [
        "github://briceyan/agents/agents/dev.too@main",
        "github://briceyan/agents/dev.too@main",
    ]


def test_github_agent_fetch_uses_raw_url(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    def fake_fetch(url: str) -> str:
        captured["url"] = url
        return "# Agent dev\n"

    monkeypatch.setattr(agent_sources, "_fetch_http_text", fake_fetch)

    text = agent_sources.fetch_agent_ref(
        GitHubRef(owner="briceyan", repo="agents", path="dev.too", rev="main")
    )

    assert text == "# Agent dev\n"
    assert captured["url"] == (
        "https://raw.githubusercontent.com/briceyan/agents/main/dev.too"
    )


def test_resolve_resident_layout_does_not_materialize_files(tmp_path: Path) -> None:
    layout = agent_sources.resolve_run_layout(tmp_path / "toolang", "alice")

    assert layout == AgentLayout.resident(tmp_path / "toolang", "alice")
    assert layout.placement == "resident"
    assert not layout.home.exists()


def test_visiting_layout_derivation_does_not_resolve_or_fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selector = "gh:brice/researcher"
    monkeypatch.setattr(
        agent_sources,
        "resolve_agent_source",
        lambda *_args, **_kwargs: pytest.fail("history layout must not resolve"),
    )
    monkeypatch.setattr(
        agent_sources,
        "fetch_agent_ref",
        lambda *_args, **_kwargs: pytest.fail("history layout must not fetch"),
    )

    assert agent_sources.visiting_layout(
        selector, toolang_root=tmp_path
    ) == AgentLayout.visiting(
        selector,
        "researcher",
        toolang_root=tmp_path,
    )


def test_resolve_visiting_layout_materializes_and_reuses_program(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = f"https://agents.example{tmp_path}/researcher.too"
    fetches: list[str] = []

    def fake_fetch(
        ref: agent_sources.AgentRef,
        *,
        progress=None,
        progress_id: str | None = None,
    ) -> str:
        del progress, progress_id
        fetches.append(ref.render())
        return "# Agent researcher\n"

    monkeypatch.setattr(agent_sources, "fetch_agent_ref", fake_fetch)

    first = agent_sources.resolve_visiting_layout(
        source, toolang_root=tmp_path / "other"
    )
    second = agent_sources.resolve_run_layout(tmp_path / "other", source)

    assert first is not second
    assert (
        first
        == second
        == AgentLayout.visiting(source, "researcher", toolang_root=tmp_path / "other")
    )
    assert first.program.read_text(encoding="utf-8") == "# Agent researcher\n"
    assert fetches == [source]


@pytest.mark.parametrize("cached", [False, True])
def test_visiting_cache_survives_interrupted_write(
    tmp_path: Path, cached: bool
) -> None:
    layout = AgentLayout(tmp_path, "researcher", "visiting")
    layout.home.mkdir(parents=True)
    if cached:
        layout.program.write_text("agic:\n  Original source.\n", encoding="utf-8")
        os.utime(layout.program, (1, 1))
    before = layout.program.read_bytes() if cached else None

    # Limit only the child process: a real partial write must not publish a
    # truncated source or make an old cache look fresh.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import resource
import signal
import sys
from pathlib import Path
from toolang.common.layout import AgentLayout
from toolang.up import source as agent_sources

AgentLayout.visiting = classmethod(
    lambda cls, source, name, **kwargs: cls(Path(sys.argv[1]), name, "visiting")
)
signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
resource.setrlimit(resource.RLIMIT_FSIZE, (128, 128))
try:
    agent_sources.materialize_visiting_program(
        agent_sources.HttpRef("https://example.com/researcher.too"),
        "agic:\\n  " + "Long source. " * 1000,
        toolang_root=Path(sys.argv[1]),
    )
except OSError:
    sys.exit(0)
sys.exit("expected a partial-write failure")
""",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (layout.program.read_bytes() if layout.program.exists() else None) == before
    assert not agent_sources._visiting_program_cache_fresh(layout.program)
    assert set(layout.home.iterdir()) == ({layout.program} if cached else set())


def test_visiting_agent_keeps_one_progress_id_across_prepare_stages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selector = "gh:owner/researcher"
    monkeypatch.setattr(
        AgentLayout,
        "visiting",
        classmethod(
            lambda cls, source, name, **kwargs: cls(
                root=tmp_path, name=name, placement="visiting"
            )
        ),
    )
    monkeypatch.setattr(
        agent_sources,
        "_resolve_github_agent_shorthand",
        lambda *_args, **_kwargs: agent_sources.HttpRef(
            url="https://agents.example/researcher.too"
        ),
    )
    monkeypatch.setattr(
        agent_sources,
        "_fetch_http_text",
        lambda _url: "# Agent researcher\n",
    )
    events: list[ProgressEvent] = []

    agent_sources.resolve_visiting_layout(
        selector, toolang_root=tmp_path, progress=events.append
    )

    assert [event.stage for event in events] == [
        "resolve",
        "resolve",
        "fetch",
        "fetch",
        "parse",
        "parse",
        "materialize",
        "materialize",
    ]
    assert len({event.id for event in events}) == 1
    assert events[0].id == f"agent:{selector}"


def test_visiting_agent_closes_materialize_progress_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = f"https://agents.example{tmp_path}/broken.too"
    monkeypatch.setattr(
        agent_sources, "_visiting_program_cache_fresh", lambda _path: False
    )
    monkeypatch.setattr(
        agent_sources,
        "_fetch_http_text",
        lambda _url: "# Agent broken\n",
    )
    monkeypatch.setattr(
        agent_sources,
        "materialize_visiting_program",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("write failed")),
    )
    events: list[ProgressEvent] = []

    with pytest.raises(OSError, match="write failed"):
        agent_sources.resolve_visiting_layout(
            source, toolang_root=tmp_path, progress=events.append
        )

    assert [(event.stage, event.status) for event in events[-2:]] == [
        ("materialize", "running"),
        ("materialize", "failed"),
    ]
    assert len({event.id for event in events}) == 1


def test_materialize_roaming_program_links_source_and_config(tmp_path: Path) -> None:
    source = tmp_path / "demo.too"
    source.write_text("# Agent demo\n", encoding="utf-8")
    config = tmp_path / "toolang.toml"
    config.write_text("[models]\n", encoding="utf-8")

    layout = agent_sources.materialize_roaming_program(source)

    assert layout == AgentLayout.roaming(source)
    assert layout.program.is_symlink()
    assert layout.config.is_file()
    assert (layout.program.parent / os.readlink(layout.program)).resolve() == source
    from toolang.common.config_sources import config_sources

    assert config_sources(layout)[-1].path == config


def test_concurrent_roaming_materialization_accepts_the_same_program_link(
    tmp_path, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from toolang.up.source import materialize_roaming_program

    source = tmp_path / "aid.too"
    source.write_text("flow run():\n  pass\n")
    with ThreadPoolExecutor(max_workers=2) as pool:
        layouts = list(pool.map(materialize_roaming_program, [source, source]))
    assert layouts[0] == layouts[1]
    assert layouts[0].program.resolve() == source


@pytest.mark.parametrize("same_source", [True, False])
def test_roaming_link_published_after_missing_stat_is_checked_at_claim(
    tmp_path, monkeypatch, same_source
):
    source = tmp_path / "aid.too"
    source.write_text("flow main():\n  pass\n")
    competitor = source if same_source else tmp_path / "aid-.too"
    competitor.write_text(source.read_text())
    layout = AgentLayout.roaming(source)
    lstat = Path.lstat
    published = False

    def race(path, *args, **kwargs):
        nonlocal published
        if path == layout.program and not published:
            published = True
            path.parent.mkdir(parents=True)
            path.symlink_to(competitor)
            # The observation was missing just before another caller published.
            raise FileNotFoundError(ENOENT, "No such file or directory", str(path))
        return lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", race)
    if same_source:
        assert agent_sources.materialize_roaming_program(source) == layout
    else:
        with pytest.raises(FileExistsError, match="already belongs"):
            agent_sources.materialize_roaming_program(source)
    assert published
    assert layout.program.resolve() == competitor


@pytest.mark.parametrize("prefix", ["gh:", "github:", "github://"])
def test_visiting_aliases_share_a_root_and_normalized_name(tmp_path, prefix):
    canonical = "github://owner/repo/Daily-Report.v2.too@main"
    layout = agent_sources.visiting_layout(
        f"{prefix}owner/repo/Daily-Report.v2.too@main", toolang_root=tmp_path
    )
    assert layout == agent_sources.visiting_layout(canonical, toolang_root=tmp_path)
    assert layout.name == "daily_report_v2"


def test_roaming_normalization_rejects_source_collision_without_replacing_state(
    tmp_path,
):
    first = tmp_path / "daily-report.too"
    other = tmp_path / "daily_report.too"
    for source in (first, other):
        source.write_text("flow main():\n  pass\n")
    layout = agent_sources.materialize_roaming_program(first)
    marker = layout.home / "keep.txt"
    marker.write_text("history")
    before = {path.relative_to(layout.home) for path in layout.home.rglob("*")}
    with pytest.raises(FileExistsError, match="already belongs"):
        agent_sources.materialize_roaming_program(other)
    assert layout.program.resolve() == first
    assert marker.read_text() == "history"
    assert {path.relative_to(layout.home) for path in layout.home.rglob("*")} == before


@pytest.mark.parametrize("stem", ["---", "中文", "A" * 65])
def test_invalid_script_name_creates_no_agent_state(tmp_path, stem):
    source = tmp_path / f"{stem}.too"
    source.write_text("flow main():\n  pass\n")
    with pytest.raises(ValueError, match="invalid agent name"):
        agent_sources.materialize_roaming_program(source)
    assert not (tmp_path / ".toolang").exists()


@pytest.mark.parametrize(
    "name", ["gus ", "Gus", "gus-by3", "gh:owner/name", "https://example.com/gus.too"]
)
def test_explicit_agent_prefix_cannot_normalize_or_select_a_remote_source(name):
    with pytest.raises(ValueError, match="invalid agent name"):
        agent_sources.parse_agent_target(f"agent:{name}")


@pytest.mark.parametrize(
    "command", [["info"], ["top"], ["inspect", "runs"], ["workspace", "list"]]
)
def test_roaming_queries_reject_normalized_home_owned_by_another_source(
    tmp_path, capsys, command
):
    from toolang.cli.toolang.routing import dispatch_roaming

    original = tmp_path / "daily-report.too"
    other = tmp_path / "daily_report.too"
    for source in (original, other):
        source.write_text("flow main():\n  pass\n")
    layout = agent_sources.materialize_roaming_program(original)
    dispatched = []
    result = dispatch_roaming(
        [str(other), *command],
        prog_name="too",
        run_app=lambda *args: dispatched.append(args) or 0,
    )
    assert result == 1
    assert "already belongs" in " ".join(capsys.readouterr().err.split())
    assert dispatched == []
    assert layout.program.resolve() == original
