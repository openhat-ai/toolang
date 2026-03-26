from __future__ import annotations

from pathlib import Path

from toolang.agent.resolve import resolve_agent_ref


def test_resolve_resident_shorthand(tmp_path: Path) -> None:
    root = tmp_path / "root"
    resolved = resolve_agent_ref("alice/bob", cwd=tmp_path, toolang_root=root)

    assert resolved.kind == "resident"
    assert resolved.uri == "agent://alice/bob.too"
    assert resolved.home == root / "agents" / "alice"
    assert resolved.source == root / "agents" / "alice" / "bob.too"


def test_resolve_agent_prefixed_resident_shorthand(tmp_path: Path) -> None:
    root = tmp_path / "root"
    resolved = resolve_agent_ref("agent:alice", cwd=tmp_path, toolang_root=root)

    assert resolved.kind == "resident"
    assert resolved.selector == "agent:alice"
    assert resolved.uri == "agent://alice/alice.too"
    assert resolved.home == root / "agents" / "alice"
    assert resolved.source == root / "agents" / "alice" / "alice.too"


def test_resolve_resident_path_normalizes_to_agent_uri(tmp_path: Path) -> None:
    root = tmp_path / "root"
    source = root / "agents" / "alice" / "alice.too"
    source.parent.mkdir(parents=True)
    source.write_text("thunk:\n    hello\n", encoding="utf-8")

    resolved = resolve_agent_ref(str(source), cwd=tmp_path, toolang_root=root)

    assert resolved.kind == "resident"
    assert resolved.uri == "agent://alice/alice.too"


def test_resolve_roaming_relative_path(tmp_path: Path) -> None:
    source = tmp_path / "bob.too"
    source.write_text("thunk:\n    hello\n", encoding="utf-8")

    resolved = resolve_agent_ref("bob.too", cwd=tmp_path, toolang_root=tmp_path / "root")

    assert resolved.kind == "roaming"
    assert resolved.uri == source.resolve().as_uri()
    assert resolved.home == tmp_path
    assert resolved.source == source.resolve()


def test_resolve_visiting_uri_builds_guest_home(tmp_path: Path) -> None:
    root = tmp_path / "root"

    resolved = resolve_agent_ref(
        "https://a.com/alice.too",
        cwd=tmp_path,
        toolang_root=root,
    )

    assert resolved.kind == "visiting"
    assert resolved.uri == "https://a.com/alice.too"
    assert resolved.home == root / "guests" / f"alice-{resolved.id[:12]}"
    assert resolved.source == resolved.home / "alice.too"


def test_resolve_hosted_shorthand_as_https(tmp_path: Path) -> None:
    resolved = resolve_agent_ref(
        "abe.fun/alice",
        cwd=tmp_path,
        toolang_root=tmp_path / "root",
    )

    assert resolved.kind == "visiting"
    assert resolved.uri == "https://abe.fun/alice"
    assert resolved.source.name == "alice.too"
