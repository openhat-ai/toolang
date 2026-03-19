from __future__ import annotations

from pathlib import Path

from toolang.agent_refs import resolve_agent_ref


def test_resolve_resident_shorthand(tmp_path: Path) -> None:
    root = tmp_path / "root"
    resolved = resolve_agent_ref("alice/bob", cwd=tmp_path, toolang_root=root)

    assert resolved.agent_kind == "resident"
    assert resolved.agent_uri == "agent://alice/bob.too"
    assert resolved.agent_home == root / "agents" / "alice"
    assert resolved.source_path == root / "agents" / "alice" / "bob.too"


def test_resolve_resident_path_normalizes_to_agent_uri(tmp_path: Path) -> None:
    root = tmp_path / "root"
    source = root / "agents" / "alice" / "alice.too"
    source.parent.mkdir(parents=True)
    source.write_text("thunk:\n    hello\n", encoding="utf-8")

    resolved = resolve_agent_ref(str(source), cwd=tmp_path, toolang_root=root)

    assert resolved.agent_kind == "resident"
    assert resolved.agent_uri == "agent://alice/alice.too"


def test_resolve_roaming_relative_path(tmp_path: Path) -> None:
    source = tmp_path / "bob.too"
    source.write_text("thunk:\n    hello\n", encoding="utf-8")

    resolved = resolve_agent_ref("bob.too", cwd=tmp_path, toolang_root=tmp_path / "root")

    assert resolved.agent_kind == "roaming"
    assert resolved.agent_uri == source.resolve().as_uri()
    assert resolved.agent_home == tmp_path
    assert resolved.source_path == source.resolve()


def test_resolve_visiting_uri_builds_guest_home(tmp_path: Path) -> None:
    root = tmp_path / "root"

    resolved = resolve_agent_ref(
        "https://a.com/alice.too",
        cwd=tmp_path,
        toolang_root=root,
    )

    assert resolved.agent_kind == "visiting"
    assert resolved.agent_uri == "https://a.com/alice.too"
    assert resolved.agent_home == root / "guests" / f"alice-{resolved.agent_id[:12]}"
    assert resolved.source_path == resolved.agent_home / "alice.too"


def test_resolve_guest_shorthand_uses_explicit_resolver(tmp_path: Path) -> None:
    root = tmp_path / "root"

    resolved = resolve_agent_ref(
        "guest:alice",
        cwd=tmp_path,
        toolang_root=root,
        guest_resolver=lambda name: f"https://abe.fun/{name}",
    )

    assert resolved.agent_kind == "visiting"
    assert resolved.agent_uri == "https://abe.fun/alice"


def test_resolve_hosted_shorthand_as_https(tmp_path: Path) -> None:
    resolved = resolve_agent_ref(
        "abe.fun/alice",
        cwd=tmp_path,
        toolang_root=tmp_path / "root",
    )

    assert resolved.agent_kind == "visiting"
    assert resolved.agent_uri == "https://abe.fun/alice"
    assert resolved.source_path.name == "alice.too"
