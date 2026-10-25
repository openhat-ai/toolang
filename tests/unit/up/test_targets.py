"""Unit tests for agent targets and remote-source handling."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from toolang.up import source as agent_sources

from toolang.base.types.progress import ProgressEvent
from toolang.common.layout import AgentLayout
from toolang.common.github import GitHubRef, GitHubShorthand


def test_agent_target_parsing_supports_name_shorthand_and_ref() -> None:
    local = agent_sources.parse_agent_target("alice")
    explicit_local = agent_sources.parse_agent_target("agent:alice")
    github_short = agent_sources.parse_agent_target("gh:brice/alice")
    http_ref = agent_sources.parse_agent_target("https://toolang.ai/alice.too")
    github_ref = agent_sources.parse_agent_target(
        "github://brice/agents/team/alice.too@main"
    )

    assert local.form == "name"
    assert local.name == "alice"
    assert explicit_local.form == "name"
    assert explicit_local.name == "alice"
    assert github_short.form == "shorthand"
    assert github_short.source == GitHubShorthand("brice", "alice")
    assert http_ref.form == "ref"
    assert http_ref.resolved_ref().render() == "https://toolang.ai/alice.too"
    assert github_ref.resolved_ref().render() == (
        "github://brice/agents/team/alice.too@main"
    )


def test_agent_target_parsing_supports_repo_shorthand() -> None:
    target = agent_sources.parse_agent_target("gh:brice/project/alice")

    assert target.source == GitHubShorthand("brice", "alice", "project")
    assert target.default_name() == "alice"


def test_agent_target_canonicalizes_raw_refs_heads_url() -> None:
    target = agent_sources.parse_agent_target(
        "https://raw.githubusercontent.com/briceyan/agents/refs/heads/main/dev.too"
    )

    assert target.resolved_ref().render() == (
        "github://briceyan/agents/dev.too@refs/heads/main"
    )


@pytest.mark.parametrize(
    "source",
    [
        "gh:owner/repo/report%2Etoo@main",
        "github://owner/repo/report%2Etoo@main",
        "https://github.com/owner/repo/blob/main/report%252Etoo",
        "https://raw.githubusercontent.com/owner/repo/main/report%252Etoo",
        "https://example.com/report%252Etoo",
    ],
)
def test_agent_target_rejects_suffixes_requiring_a_second_url_decode(source):
    with pytest.raises(ValueError, match="must point to a .too program"):
        agent_sources.parse_agent_target(source)


@pytest.mark.parametrize(
    "source",
    [
        "https://github.com/owner/repo/blob/main/report%2Etoo",
        "https://raw.githubusercontent.com/owner/repo/main/report%2Etoo",
        "https://example.com/report%2Etoo",
    ],
)
def test_agent_target_accepts_a_url_encoded_too_suffix(source):
    assert agent_sources.parse_agent_target(source).default_name() == "report"


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
        root=tmp_path,
    )


def test_resolve_visiting_layout_materializes_and_reuses_program(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "https://agents.example/researcher.too"
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
        == AgentLayout.visiting(source, "researcher", root=tmp_path / "other")
    )
    assert first.program.read_text(encoding="utf-8") == "# Agent researcher\n"
    assert fetches == [source]


@pytest.mark.parametrize("cached", [False, True])
def test_visiting_cache_survives_interrupted_write(
    tmp_path: Path, cached: bool
) -> None:
    from toolang.up.identity import visiting_home_guard

    layout = AgentLayout.visiting(
        "https://example.com/researcher.too", "researcher", root=tmp_path
    )
    with visiting_home_guard(layout):
        pass
    if cached:
        from toolang.common.files import atomic_write_text

        atomic_write_text(layout.program, "agic:\n  Original source.\n")
        os.utime(layout.program, (1, 1))
    before = layout.program.read_bytes() if cached else None
    entries = set(layout.home.iterdir())

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
from toolang.common import layout as paths
from toolang.up import source as agent_sources

paths.VISITING_BASE = Path(sys.argv[1]) / "visiting"
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
    assert set(layout.home.iterdir()) == entries


def test_visiting_agent_keeps_one_progress_id_across_prepare_stages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selector = "gh:owner/researcher"
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

    layout = agent_sources.materialize_roaming_program(source, toolang_root=tmp_path)

    assert layout == AgentLayout.roaming(
        source, place_code=layout.place_code, root=tmp_path
    )
    assert layout.program.is_symlink()
    assert layout.place_config.is_file()
    assert not layout.config.exists()
    assert (layout.program.parent / os.readlink(layout.program)).resolve() == source
    from toolang.common.config_sources import config_sources

    assert config_sources(layout)[-1].path == config


def test_concurrent_roaming_materialization_accepts_the_same_program_link(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from toolang.up.source import materialize_roaming_program

    source = tmp_path / "aid.too"
    source.write_text("flow run():\n  pass\n")
    with ThreadPoolExecutor(max_workers=2) as pool:
        layouts = list(
            pool.map(
                lambda path: materialize_roaming_program(path, toolang_root=tmp_path),
                [source, source],
            )
        )
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
    layout = agent_sources.roaming_layout(source, toolang_root=tmp_path)
    symlink = Path.symlink_to
    published = False

    def race(path, target, *args, **kwargs):
        nonlocal published
        if path == layout.program and not published:
            published = True
            symlink(path, competitor)
            raise FileExistsError(str(path))
        return symlink(path, target, *args, **kwargs)

    monkeypatch.setattr(Path, "symlink_to", race)
    if same_source:
        assert (
            agent_sources.materialize_roaming_program(source, toolang_root=tmp_path)
            == layout
        )
    else:
        with pytest.raises(FileExistsError, match="already belongs"):
            agent_sources.materialize_roaming_program(source, toolang_root=tmp_path)
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


def test_normalized_source_names_have_independent_homes(
    tmp_path,
):
    first = tmp_path / "daily-report.too"
    other = tmp_path / "daily_report.too"
    for source in (first, other):
        source.write_text("flow main():\n  pass\n")
    layout = agent_sources.materialize_roaming_program(first, toolang_root=tmp_path)
    marker = layout.home / "keep.txt"
    marker.write_text("history")
    before = {path.relative_to(layout.home) for path in layout.home.rglob("*")}
    second = agent_sources.materialize_roaming_program(other, toolang_root=tmp_path)
    assert second.home != layout.home and second.name == layout.name
    assert second.program.resolve() == other
    assert layout.program.resolve() == first
    assert marker.read_text() == "history"
    assert {path.relative_to(layout.home) for path in layout.home.rglob("*")} == before


@pytest.mark.parametrize("stem", ["---", "中文", "A" * 65])
def test_invalid_script_name_creates_no_agent_state(tmp_path, stem):
    source = tmp_path / f"{stem}.too"
    source.write_text("flow main():\n  pass\n")
    with pytest.raises(ValueError, match="invalid agent name"):
        agent_sources.materialize_roaming_program(source, toolang_root=tmp_path)
    assert not (tmp_path / ".toolang").exists()


@pytest.mark.parametrize(
    "name", ["gus ", "Gus", "gus-by3", "gh:owner/name", "https://example.com/gus.too"]
)
def test_explicit_agent_prefix_cannot_normalize_or_select_a_remote_source(name):
    with pytest.raises(ValueError, match="invalid agent name"):
        agent_sources.parse_agent_target(f"agent:{name}")


@pytest.mark.parametrize(
    "command", [["info"], ["inspect", "runs"], ["workspace", "list"]]
)
def test_roaming_queries_do_not_select_legacy_unnumbered_homes(
    tmp_path, capsys, command
):
    from toolang.cli.toolang import main as cli

    original = tmp_path / "daily-report.too"
    other = tmp_path / "daily_report.too"
    for source in (original, other):
        source.write_text("flow main():\n  pass\n")
    layout = AgentLayout.roaming(original)
    layout.home.mkdir(parents=True)
    layout.program.symlink_to(original)
    before = layout.program.read_bytes()
    result = cli.main([str(other), *command])
    assert result == (1 if command[0] == "inspect" else 0)
    output = capsys.readouterr()
    if command[0] == "inspect":
        assert "no retained instance" in output.err
    assert layout.program.read_bytes() == before
    assert layout.program.resolve() == original


def test_visiting_acquisition_pins_and_preserves_provenance(tmp_path, monkeypatch):
    import json
    from dataclasses import replace
    from toolang.common.github import pin_github_ref
    from toolang.state.cache import layer_revision_dir, validate_agent_revision
    from toolang.state.prepare import prepare_agent_state, load_agent_state
    from toolang.up.server import resolve_serve

    sha = "c" * 40
    requested = "gh:owner/agents/dev.too@main"
    fetched = []
    content = "# Original\r\nflow work():\r\n  pass\r\n"
    monkeypatch.setattr(
        agent_sources,
        "_resolve_github_commit",
        lambda ref: (
            pin_github_ref(
                ref,
                lambda url: json.dumps({"sha": sha}).encode(),
            ).rev
        ),
    )
    monkeypatch.setattr(
        agent_sources,
        "fetch_agent_ref",
        lambda ref, **kwargs: fetched.append(ref) or content,
    )
    first = agent_sources.resolve_visiting_layout(requested, toolang_root=tmp_path)
    assert fetched[0].rev == sha
    # The hidden server reconstructs paths without live source data.
    restored = resolve_serve(
        layout=replace(first, program_acquisition=None), port=9000
    ).layout
    assert restored.program_acquisition == first.program_acquisition
    state = prepare_agent_state(restored)
    directory = layer_revision_dir(restored, "home", state.home_revision)
    layer = json.loads((directory / "layer.json").read_bytes())
    item = layer["modules"][0]
    assert item["source"] == "agent.too"
    assert item["resolution"]["declared_ref"] == requested
    assert item["resolution"]["resolved_ref"] == f"github://owner/agents/dev.too@{sha}"
    entry = item["resolution"]["materialized"]
    assert (directory / "materialized" / entry).read_bytes() == content.encode()
    assert (directory / "source/agent.too").read_bytes() == content.encode()
    assert (
        agent_sources.resolve_visiting_layout(
            requested, toolang_root=tmp_path
        ).program_acquisition
        == first.program_acquisition
    )
    assert (
        agent_sources.resolve_visiting_layout(
            requested.replace("gh:", "github:"), toolang_root=tmp_path
        ).program_acquisition
        == first.program_acquisition
    )
    assert len(fetched) == 1
    assert prepare_agent_state(restored, force=True).revision == state.revision
    monkeypatch.setattr(
        agent_sources,
        "parse_agent_target",
        lambda *args: pytest.fail("historical source resolution"),
    )
    assert load_agent_state(restored, state.revision).modules == state.modules
    validate_agent_revision(restored, state.revision)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("resolved_ref", "invalid:cached"),
        ("resolved_ref", 42),
        ("declared_ref", None),
        ("declared_ref", "invalid:cached"),
        ("entry", "other.too"),
    ],
)
def test_invalid_visiting_receipt_is_refetched(tmp_path, monkeypatch, field, value):
    import json
    from toolang.state.prepare import prepare_agent_state

    url = "https://example.com/dev.too"
    content = "flow main():\n  pass\n"
    cached = agent_sources.materialize_visiting_program(
        agent_sources.HttpRef(url),
        content,
        toolang_root=tmp_path,
    )
    path = cached.home / "agent.source.json"
    receipt = json.loads(path.read_bytes())
    receipt[field] = value
    path.write_text(json.dumps(receipt))
    fetched = []
    monkeypatch.setattr(
        agent_sources,
        "fetch_agent_ref",
        lambda ref, **kwargs: fetched.append(ref) or content,
    )
    actual = agent_sources.resolve_visiting_layout(url, toolang_root=tmp_path)
    assert len(fetched) == 1
    assert actual.program_acquisition is not None
    assert actual.program_acquisition.declared_ref == url
    assert prepare_agent_state(actual).runnables["main"]


def test_visiting_reuse_honors_an_explicit_commit():
    requested = f"github://owner/repo/dev.too@{'b' * 40}"
    target = agent_sources.parse_agent_target(requested)
    assert not agent_sources.reusable_agent_source(
        target, f"github://owner/repo/dev.too@{'a' * 40}"
    )
    assert agent_sources.reusable_agent_source(target, requested)


def test_visiting_identity_and_acquisition_keep_full_urls_while_progress_hides_credentials(
    tmp_path, monkeypatch
):
    from dataclasses import asdict
    import json

    url = "https://secret-user:secret-pass@example.com/alice.too?token=secret-token#secret-fragment"
    fetched = []

    def fetch(value):
        fetched.append(value)
        return "flow:\n  pass\n"

    monkeypatch.setattr(agent_sources, "_fetch_http_text", fetch)
    events = []
    layout = agent_sources.resolve_visiting_layout(
        url, toolang_root=tmp_path, progress=events.append
    )
    assert fetched == [url]
    repeated = agent_sources.resolve_visiting_layout(
        url, toolang_root=tmp_path, progress=events.append
    )
    assert repeated == layout and fetched == [url]
    assert layout == agent_sources.visiting_layout(url, toolang_root=tmp_path)
    assert layout.program_acquisition is not None
    assert layout.program_acquisition.declared_ref == url
    output = json.dumps([asdict(event) for event in events])
    assert "secret-" not in output
    assert "secret-" not in layout.address
    assert "https://example.com/alice.too" in output


def test_visiting_fetch_failure_does_not_print_url_credentials(monkeypatch):
    from urllib.error import URLError

    def denied(*_args, **_kwargs):
        raise URLError("denied")

    monkeypatch.setattr(agent_sources, "urlopen", denied)
    events = []
    with pytest.raises(ValueError) as error:
        agent_sources.fetch_agent_ref(
            agent_sources.HttpRef(
                "https://secret-user:secret-pass@example.com/alice.too?token=secret-token"
            ),
            progress=events.append,
        )
    assert "secret-" not in str(error.value)
    assert all("secret-" not in str(event) for event in events)
