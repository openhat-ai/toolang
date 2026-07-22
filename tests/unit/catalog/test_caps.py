"""Unit tests for authored and wired cap collections."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from toolang.catalog.cap import AuthoredCaps, CapFile
from toolang.catalog.config import CapRef, WiredCaps
from toolang.catalog.errors import CatalogConflictError, CatalogNotFoundError
from toolang.catalog.types import CapKind


def _cap(kind, name: str, content: str) -> CapFile:
    return CapFile.parse(content, kind=kind, name=name)


def test_cap_file_parse_projects_original_source_fields() -> None:
    content = "---\ndescription: Review code\n---\nReview carefully.\n"

    cap = CapFile.parse(content, kind="skill", name="reviewer")

    assert cap.path is None
    assert cap.content == content
    assert cap.kind == "skill"
    assert cap.name == "reviewer"
    assert cap.meta == {"description": "Review code", "name": "reviewer"}
    assert cap.body == "Review carefully."


def test_cap_file_requires_identity_meta() -> None:
    with pytest.raises(ValueError, match="cap name is required"):
        CapFile(path=None, content="Prompt.\n", kind="prompt", meta={}, body="Prompt.")


def test_cap_file_rejects_mismatched_authored_name() -> None:
    with pytest.raises(ValueError, match="cap name does not match its path"):
        CapFile.parse(
            "---\nname: authored\n---\nPrompt.\n",
            kind="prompt",
            name="expected",
        )


@pytest.mark.parametrize(
    ("kind", "name", "content", "message"),
    [
        (
            "skill",
            "reviewer",
            "---\ndescription: ''\n---\nReview carefully.\n",
            "skill description is required",
        ),
        (
            "skill",
            "reviewer",
            "---\ndescription: Review code\n---\n",
            "skill body is required",
        ),
        (
            "skill",
            "reviewer",
            "---\ndescription: Review code\ntitle: Unsupported\n---\nReview.\n",
            "unsupported frontmatter fields",
        ),
        (
            "service",
            "linear",
            "---\ntransport: http\ntarget: https://example\n---\n",
            "service description is required",
        ),
        (
            "service",
            "linear",
            "---\ndescription: Linear\ntransport: grpc\ntarget: example\n---\n",
            "service transport must be http or stdio",
        ),
        (
            "service",
            "linear",
            "---\ndescription: Linear\ntransport: http\ntarget: ''\n---\n",
            "service target is required",
        ),
        (
            "service",
            "linear",
            "---\ndescription: Linear\ntransport: http\ntarget: https://example\nheaders: token\n---\n",
            "service headers must be a string map",
        ),
    ],
)
def test_cap_file_validates_kind_specific_fields(
    kind: CapKind,
    name: str,
    content: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        CapFile.parse(content, kind=kind, name=name)


def test_cap_ref_validates_identity_and_ref() -> None:
    with pytest.raises(ValueError, match="cap ref cannot be empty"):
        CapRef(kind="prompt", name="review", ref=" ")
    with pytest.raises(ValueError, match="invalid cap name"):
        CapRef(kind="prompt", name="../review", ref="https://example/review")
    with pytest.raises(ValueError, match="unsupported cap kind"):
        CapRef(
            kind=cast(CapKind, "tool"),
            name="review",
            ref="https://example/review",
        )


def test_authored_caps_loads_cap_files_without_auxiliary_skill_files(
    tmp_path: Path,
) -> None:
    skill = tmp_path / "skills" / "reviewer"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\ndescription: Review code\n---\nReview carefully.\n",
        encoding="utf-8",
    )
    (skill / "notes.txt").write_text("asset\n", encoding="utf-8")
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "rewrite.md").write_text(
        "Rewrite this text.\n", encoding="utf-8"
    )

    caps = AuthoredCaps(tmp_path).list()

    assert [(cap.kind, cap.name) for cap in caps] == [
        ("prompt", "rewrite"),
        ("skill", "reviewer"),
    ]
    assert caps[1].meta["description"] == "Review code"


def test_authored_caps_crud_returns_cap_files(tmp_path: Path) -> None:
    caps = AuthoredCaps(tmp_path)
    created = caps.create(_cap("prompt", "rewrite", "Rewrite this text.\n"))
    updated = caps.update(_cap("prompt", "rewrite", "Rewrite carefully.\n"))
    removed = caps.remove("prompt", "rewrite")

    assert created.path == tmp_path / "prompts" / "rewrite.md"
    assert updated.body == "Rewrite carefully."
    assert removed == updated
    assert removed.path is not None and not removed.path.exists()
    assert caps.list() == ()


def test_authored_caps_reports_conflicting_and_missing_mutations(
    tmp_path: Path,
) -> None:
    caps = AuthoredCaps(tmp_path)
    cap = _cap("prompt", "rewrite", "Rewrite this text.\n")

    caps.create(cap)
    with pytest.raises(CatalogConflictError, match="already exists"):
        caps.create(cap)
    with pytest.raises(CatalogNotFoundError, match="not found"):
        caps.update(_cap("prompt", "missing", "Missing.\n"))
    with pytest.raises(CatalogNotFoundError, match="not found"):
        caps.remove("prompt", "missing")


def test_authored_caps_removes_complete_skill_directory(tmp_path: Path) -> None:
    caps = AuthoredCaps(tmp_path)
    skill = caps.create(
        _cap(
            "skill",
            "reviewer",
            "---\ndescription: Review code\n---\nReview carefully.\n",
        )
    )
    assert skill.path is not None
    asset = skill.path.parent / "assets" / "guide.md"
    asset.parent.mkdir()
    asset.write_text("Guide.\n", encoding="utf-8")

    removed = caps.remove("skill", "reviewer")

    assert removed == skill
    assert not skill.path.parent.exists()


def test_authored_caps_rejects_service_env_map(tmp_path: Path) -> None:
    content = (
        "---\n"
        "description: Linear MCP\n"
        "transport: stdio\n"
        "target: uvx linear\n"
        "env:\n"
        "  LINEAR_API_KEY: $LINEAR_API_KEY\n"
        "---\n"
    )

    with pytest.raises(
        ValueError, match="service env must list environment variable names"
    ):
        AuthoredCaps(tmp_path).create(_cap("service", "linear", content))


def test_wired_caps_crud_preserves_other_config_and_returns_removed_ref(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[web]\nhost = "127.0.0.1"\n', encoding="utf-8")
    caps = WiredCaps(path)
    original = CapRef(
        kind="skill",
        name="reviewer",
        ref="github://acme/caps/skills/reviewer@main",
    )

    assert caps.create(original) == original
    updated = CapRef(kind="skill", name="reviewer", ref="https://example/reviewer")
    assert caps.update(updated) == updated
    assert caps.list() == (updated,)
    assert caps.remove("skill", "reviewer") == updated
    assert caps.list() == ()
    assert "[web]" in path.read_text(encoding="utf-8")


def test_wired_caps_reports_conflicting_and_missing_mutations(tmp_path: Path) -> None:
    caps = WiredCaps(tmp_path / "config.toml")
    cap = CapRef(kind="prompt", name="review", ref="https://example/review")

    assert caps.list() == ()
    caps.create(cap)
    with pytest.raises(CatalogConflictError, match="already exists"):
        caps.create(cap)
    with pytest.raises(CatalogNotFoundError, match="not found"):
        caps.update(CapRef(kind="prompt", name="missing", ref="https://example"))
    with pytest.raises(CatalogNotFoundError, match="not found"):
        caps.remove("prompt", "missing")


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("skills = 'invalid'\n", "invalid wired cap table"),
        ("[skills]\nreview = {}\n", "invalid wired cap config entry"),
        ("skills = 1\n", "invalid wired cap table"),
    ],
)
def test_wired_caps_rejects_invalid_config_entries(
    tmp_path: Path,
    content: str,
    message: str,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        WiredCaps(path).list()


def test_wired_caps_preserves_unrelated_config_comments(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    original = (
        "# Root comment\n"
        'model = "openai/gpt-5" # Inline comment\n\n'
        "[web]\n"
        'host = "127.0.0.1" # Web comment\n'
    )
    path.write_text(original, encoding="utf-8")
    caps = WiredCaps(path)

    caps.create(CapRef(kind="prompt", name="review", ref="https://example/review"))
    created_text = path.read_text(encoding="utf-8")
    caps.remove("prompt", "review")
    removed_text = path.read_text(encoding="utf-8")

    assert created_text.startswith(original)
    assert removed_text.startswith(original)
    assert "# Root comment" in removed_text
    assert "# Inline comment" in removed_text
    assert "# Web comment" in removed_text


def test_wired_caps_update_preserves_entry_comment_and_spacing(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[skills]\nreviewer = { ref = "old" } # Keep this comment\n',
        encoding="utf-8",
    )

    WiredCaps(path).update(
        CapRef(kind="skill", name="reviewer", ref="https://example/reviewer")
    )

    assert path.read_text(encoding="utf-8") == (
        '[skills]\nreviewer = { ref = "https://example/reviewer" }'
        " # Keep this comment\n"
    )
