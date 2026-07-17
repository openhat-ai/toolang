"""Authored capability catalog and remote source resolution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
import re
import shutil
import tomllib
from typing import Literal, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

import frontmatter
import tomli_w
from tomli_w._writer import Context as TomlContext
from tomli_w._writer import format_inline_table, format_key_part, format_literal

from toolang.common.github import (
    GitHubRef,
    github_raw_url,
    parse_github_ref,
    parse_github_url,
)
from toolang.common.progress import ProgressSink, emit_progress

EntryKind = Literal["psyche", "skill", "service", "prompt"]
Visibility = Literal["shared", "private"]
EntryOrigin = Literal["local", "remote"]
EntryForm = Literal["wired", "file"]
EntryScope = Literal["root", "home"]

CAP_KINDS: tuple[EntryKind, ...] = ("psyche", "skill", "service", "prompt")
MANAGED_KINDS = frozenset(CAP_KINDS)
SKILL_FIELDS = frozenset({"description"})
SERVICE_FIELDS = frozenset(
    {"description", "transport", "protocol", "target", "headers", "env"}
)
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DIR_NAME_BY_KIND: dict[EntryKind, str] = {
    "psyche": "psyches",
    "skill": "skills",
    "service": "services",
    "prompt": "prompts",
}
KIND_BY_DIR_NAME: dict[str, EntryKind] = {
    directory: kind for kind, directory in DIR_NAME_BY_KIND.items()
}
CONFIG_SECTION_ORDER = {
    "psyches": 0,
    "skills": 1,
    "services": 2,
    "prompts": 3,
}


@dataclass(frozen=True, slots=True)
class AuthoredCapEntry:
    """One cap source authored in a file or config table."""

    kind: EntryKind
    name: str
    visibility: Visibility
    origin: EntryOrigin
    form: EntryForm
    ref: str
    path: str
    definition_file: str
    meta: dict[str, object]
    line: int | None = None

    @property
    def scope(self) -> EntryScope:
        return "root" if self.visibility == "shared" else "home"


class CapCatalog:
    """CRUD over one root or agent-home authored cap location."""

    def __init__(
        self,
        root: Path,
        agent: str,
        *,
        visibility: Visibility,
    ) -> None:
        self.root = root
        self.agent = agent
        self.visibility = visibility

    def list(
        self,
        *,
        kinds: set[EntryKind] | None = None,
    ) -> tuple[AuthoredCapEntry, ...]:
        """List authored file and wired entries without preparing effective state."""

        entries = [
            *_list_file_entries(
                self.root,
                self.agent,
                visibility=self.visibility,
                kinds=kinds,
            ),
            *list_wired_entries(
                self.root,
                self.agent,
                visibility=self.visibility,
                kinds=kinds,
            ),
        ]
        return tuple(sorted(entries, key=lambda item: (item.kind, item.name, item.ref)))

    def get(
        self,
        kind: EntryKind,
        name: str,
        *,
        form: EntryForm | None = "file",
    ) -> AuthoredCapEntry | None:
        return next(
            (
                entry
                for entry in self.list(kinds={kind})
                if entry.name == name and (form is None or entry.form == form)
            ),
            None,
        )

    def create(self, kind: EntryKind, name: str, text: str) -> Path:
        if self.get(kind, name) is not None:
            raise FileExistsError(f"local {kind} already exists: {name}")
        return _write_local_entry(
            self.root,
            self.agent,
            visibility=self.visibility,
            kind=kind,
            name=name,
            text=text,
        )

    def update(self, kind: EntryKind, name: str, text: str) -> Path:
        if self.get(kind, name) is None:
            raise FileNotFoundError(f"local {kind} not found: {name}")
        return _write_local_entry(
            self.root,
            self.agent,
            visibility=self.visibility,
            kind=kind,
            name=name,
            text=text,
        )

    def read(self, kind: EntryKind, name: str) -> str:
        _validate_kind(kind)
        entry_path = local_entry_file_path(
            self.root,
            self.agent,
            visibility=self.visibility,
            kind=kind,
            name=name,
        )
        if not entry_path.is_file():
            raise FileNotFoundError(f"local {kind} not found: {name}")
        return entry_path.read_text(encoding="utf-8")

    def remove(self, kind: EntryKind, name: str) -> bool:
        _validate_kind(kind)
        if kind == "skill":
            target = self.root / relative_definition_root(
                self.agent,
                visibility=self.visibility,
                kind=kind,
                name=name,
            )
            if not target.exists():
                return False
            shutil.rmtree(target)
            return True
        entry_path = local_entry_file_path(
            self.root,
            self.agent,
            visibility=self.visibility,
            kind=kind,
            name=name,
        )
        if not entry_path.exists():
            return False
        entry_path.unlink()
        return True

    def snapshot(self) -> tuple[AuthoredCapEntry, ...]:
        return self.list()


def add_remote_entry(
    toolang_root: Path,
    agent_name: str,
    *,
    visibility: Visibility,
    kind: EntryKind,
    ref: str,
    progress: ProgressSink | None = None,
) -> Path:
    """Resolve and add one remote cap ref to authored config."""

    _validate_kind(kind)
    canonical_ref = resolve_remote_ref(kind, ref, progress=progress)
    name = remote_entry_name(kind, canonical_ref)
    _ensure_name_available(
        toolang_root,
        agent_name,
        visibility=visibility,
        kind=kind,
        name=name,
        ref=canonical_ref,
    )
    authored_config_path = config_path(
        toolang_root,
        agent_name,
        visibility=visibility,
    )
    data = _load_optional_toml(authored_config_path)
    table = _config_kind_table(data, kind)
    table[name] = {"ref": canonical_ref}
    emit_progress(
        progress,
        id=f"cap.config:{kind}:{name}",
        phase="cap.config",
        label=f"Write {kind} config",
        status="running",
        detail=canonical_ref,
    )
    _write_config_data(authored_config_path, data)
    emit_progress(
        progress,
        id=f"cap.config:{kind}:{name}",
        phase="cap.config",
        label=f"Write {kind} config",
        status="ok",
        detail=str(authored_config_path),
    )
    return authored_config_path


def remove_remote_entry(
    toolang_root: Path,
    agent_name: str,
    *,
    visibility: Visibility,
    kind: EntryKind,
    name: str,
) -> bool:
    """Remove one wired remote cap ref from authored config."""

    _validate_kind(kind)
    path = config_path(toolang_root, agent_name, visibility=visibility)
    if not path.is_file():
        return False
    data = _load_optional_toml(path)
    key = DIR_NAME_BY_KIND[kind]
    table = _config_kind_table_optional(data, kind)
    if table is None or name not in table:
        return False
    table.pop(name, None)
    if not table:
        data.pop(key, None)
    _write_config_data(path, data)
    return True


def list_wired_entries(
    toolang_root: Path,
    agent_name: str,
    *,
    visibility: Visibility,
    kinds: set[EntryKind] | None = None,
) -> tuple[AuthoredCapEntry, ...]:
    """List remote refs authored in one config layer."""

    path = config_path(toolang_root, agent_name, visibility=visibility)
    if not path.is_file():
        return ()
    data = _load_optional_toml(path)
    relative_path = str(path.relative_to(toolang_root))
    entries: list[AuthoredCapEntry] = []
    for kind in CAP_KINDS:
        if kinds is not None and kind not in kinds:
            continue
        table = _config_kind_table_optional(data, kind)
        if table is None:
            continue
        for name, item in sorted(table.items()):
            ref = _config_ref(item)
            entries.append(
                AuthoredCapEntry(
                    kind=kind,
                    name=name,
                    visibility=visibility,
                    origin="remote",
                    form="wired",
                    ref=ref,
                    path=relative_path,
                    definition_file=relative_path,
                    meta={},
                )
            )
    return tuple(entries)


def resolve_remote_ref(
    kind: EntryKind,
    ref: str,
    *,
    progress: ProgressSink | None = None,
) -> str:
    """Resolve one authored remote ref or shorthand to a canonical ref."""

    text = ref.strip()
    emit_progress(
        progress,
        id=f"cap.resolve:{kind}:{text}",
        phase="cap.resolve",
        label=f"Resolve {kind}",
        status="running",
        detail=text,
    )
    if "://" in text:
        try:
            canonical_ref = canonicalize_remote_ref(kind, text)
            if canonical_ref.startswith("github://") and not _github_remote_exists(
                kind, canonical_ref
            ):
                raise ValueError(
                    f"remote {kind} not found or missing entry file: {ref}"
                )
        except Exception as exc:
            emit_progress(
                progress,
                id=f"cap.resolve:{kind}:{text}",
                phase="cap.resolve",
                label=f"Resolve {kind}",
                status="failed",
                detail=str(exc),
            )
            raise
        emit_progress(
            progress,
            id=f"cap.resolve:{kind}:{text}",
            phase="cap.resolve",
            label=f"Resolve {kind}",
            status="ok",
            detail=canonical_ref,
        )
        return canonical_ref
    candidates = _remote_ref_candidates(kind, text)
    if not candidates:
        emit_progress(
            progress,
            id=f"cap.resolve:{kind}:{text}",
            phase="cap.resolve",
            label=f"Resolve {kind}",
            status="failed",
            detail=f"invalid remote ref: {ref}",
        )
        raise ValueError(f"invalid remote ref: {ref}")
    for candidate in candidates:
        if _github_remote_exists(kind, candidate):
            emit_progress(
                progress,
                id=f"cap.resolve:{kind}:{text}",
                phase="cap.resolve",
                label=f"Resolve {kind}",
                status="ok",
                detail=candidate,
            )
            return candidate
    emit_progress(
        progress,
        id=f"cap.resolve:{kind}:{text}",
        phase="cap.resolve",
        label=f"Resolve {kind}",
        status="failed",
        detail=f"could not resolve remote {kind} shorthand: {ref}",
    )
    raise ValueError(f"could not resolve remote {kind} shorthand: {ref}")


def canonicalize_remote_ref(kind: EntryKind, ref: str) -> str:
    """Canonicalize one explicit or shorthand remote cap ref."""

    text = ref.strip()
    if "://" in text:
        github_ref = parse_github_url(text)
        if github_ref is None and text.startswith("github://"):
            github_ref = parse_github_ref(text)
        if github_ref is not None:
            if kind == "skill" and Path(github_ref.path).name == "SKILL.md":
                github_ref = GitHubRef(
                    owner=github_ref.owner,
                    repo=github_ref.repo,
                    path=str(Path(github_ref.path).parent),
                    rev=github_ref.rev,
                )
            return github_ref.render()
        return text
    candidates = _remote_ref_candidates(kind, text)
    if not candidates:
        raise ValueError(f"invalid remote ref: {ref}")
    return candidates[0]


def remote_entry_name(kind: EntryKind, ref: str) -> str:
    """Return the authored name derived from one remote cap ref."""

    canonical_ref = canonicalize_remote_ref(kind, ref)
    if canonical_ref.startswith("github://"):
        path = parse_github_ref(canonical_ref).path.rstrip("/")
        if not path:
            raise ValueError(f"invalid remote ref: {ref}")
        return Path(path).name if kind == "skill" else Path(path).stem
    parsed = urlparse(canonical_ref)
    path = parsed.path.rstrip("/")
    if not path:
        raise ValueError(f"invalid remote ref: {ref}")
    name = Path(path).name.split("@", 1)[0]
    return name if kind == "skill" else Path(name).stem


def config_path(
    toolang_root: Path,
    agent_name: str,
    *,
    visibility: Visibility,
) -> Path:
    """Return the authored config path for one visibility."""

    if visibility == "shared":
        return toolang_root / "config.toml"
    return toolang_root / "agents" / agent_name / "config.toml"


def local_entry_file_path(
    toolang_root: Path,
    agent_name: str,
    *,
    visibility: Visibility,
    kind: EntryKind,
    name: str,
) -> Path:
    """Return the authored local entry file path."""

    definition_root = relative_definition_root(
        agent_name,
        visibility=visibility,
        kind=kind,
        name=name,
    )
    relative_path = (
        definition_root / "SKILL.md"
        if kind == "skill"
        else definition_root.with_suffix(".md")
    )
    return toolang_root / relative_path


def relative_definition_root(
    agent_name: str,
    *,
    visibility: Visibility,
    kind: EntryKind,
    name: str,
) -> Path:
    """Return one authored definition root relative to Toolang root."""

    prefix = Path() if visibility == "shared" else Path("agents") / agent_name
    return prefix / DIR_NAME_BY_KIND[kind] / name


def local_cap_ref(
    *,
    visibility: Visibility,
    kind: EntryKind,
    name: str,
) -> str:
    """Return one canonical ref for an authored local cap."""

    scheme = "root" if visibility == "shared" else "home"
    return f"{scheme}://{DIR_NAME_BY_KIND[kind]}/{name}"


def _list_file_entries(
    toolang_root: Path,
    agent_name: str,
    *,
    visibility: Visibility,
    kinds: set[EntryKind] | None,
) -> tuple[AuthoredCapEntry, ...]:
    prefix = toolang_root if visibility == "shared" else toolang_root / "agents" / agent_name
    entries: list[AuthoredCapEntry] = []
    for kind in CAP_KINDS:
        if kinds is not None and kind not in kinds:
            continue
        directory = prefix / DIR_NAME_BY_KIND[kind]
        if not directory.is_dir():
            continue
        if kind == "skill":
            files = sorted(directory.glob("*/SKILL.md"))
        else:
            files = sorted(directory.glob("*.md"))
        for path in files:
            name = path.parent.name if kind == "skill" else path.stem
            relative_path = str(path.relative_to(toolang_root))
            entries.append(
                AuthoredCapEntry(
                    kind=kind,
                    name=name,
                    visibility=visibility,
                    origin="local",
                    form="file",
                    ref=local_cap_ref(visibility=visibility, kind=kind, name=name),
                    path=relative_path,
                    definition_file=relative_path,
                    meta=_load_meta(path),
                )
            )
    return tuple(entries)


def _write_local_entry(
    toolang_root: Path,
    agent_name: str,
    *,
    visibility: Visibility,
    kind: EntryKind,
    name: str,
    text: str,
) -> Path:
    _validate_kind(kind)
    _validate_authored_entry_text(kind=kind, text=text)
    path = local_entry_file_path(
        toolang_root,
        agent_name,
        visibility=visibility,
        kind=kind,
        name=name,
    )
    _ensure_name_available(
        toolang_root,
        agent_name,
        visibility=visibility,
        kind=kind,
        name=name,
        ref=local_cap_ref(visibility=visibility, kind=kind, name=name),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _ensure_name_available(
    toolang_root: Path,
    agent_name: str,
    *,
    visibility: Visibility,
    kind: EntryKind,
    name: str,
    ref: str,
) -> None:
    entries = CapCatalog(
        toolang_root,
        agent_name,
        visibility=visibility,
    ).list(kinds={kind})
    for entry in entries:
        if entry.name == name and entry.ref != ref:
            raise ValueError(
                f"conflicting entries in one visibility: kind={kind} name={name}"
            )


def _validate_kind(kind: EntryKind) -> None:
    if kind not in MANAGED_KINDS:
        raise ValueError(f"unsupported kind: {kind}")


def _validate_authored_entry_text(*, kind: EntryKind, text: str) -> None:
    if kind not in {"skill", "service"}:
        return
    post = frontmatter.loads(text)
    meta = dict(post.metadata)
    if kind == "skill":
        _require_exact_meta_fields(kind=kind, meta=meta, allowed=SKILL_FIELDS)
        description = meta.get("description")
        if not isinstance(description, str) or not description.strip():
            raise ValueError("skill description is required")
        if not post.content.strip():
            raise ValueError("skill body is required")
        return
    _require_exact_meta_fields(kind=kind, meta=meta, allowed=SERVICE_FIELDS)
    description = meta.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("service description is required")
    transport = meta.get("transport") or meta.get("protocol")
    if transport not in {"http", "stdio"}:
        raise ValueError("service transport must be http or stdio")
    target = meta.get("target")
    if not isinstance(target, str) or not target.strip():
        raise ValueError("service target is required")
    headers = meta.get("headers")
    if headers is not None and not _is_string_map(headers):
        raise ValueError("service headers must be a string map")
    env = meta.get("env")
    if env is not None and not _is_env_names(env):
        raise ValueError("service env must list environment variable names")


def _require_exact_meta_fields(
    *,
    kind: EntryKind,
    meta: Mapping[str, object],
    allowed: frozenset[str],
) -> None:
    unknown = sorted(set(meta) - set(allowed))
    if unknown:
        joined = ", ".join(repr(item) for item in unknown)
        raise ValueError(f"{kind} has unsupported frontmatter fields: {joined}")


def _is_string_map(value: object) -> bool:
    return isinstance(value, dict) and all(
        isinstance(key, str) and isinstance(item, str)
        for key, item in value.items()
    )


def _is_env_names(value: object) -> bool:
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",")]
    elif isinstance(value, list | tuple):
        items = [item.strip() for item in value if isinstance(item, str)]
        if len(items) != len(value):
            return False
    else:
        return False
    return bool(items) and all(ENV_NAME_RE.fullmatch(item) for item in items)


def _remote_ref_candidates(kind: EntryKind, ref: str) -> tuple[str, ...]:
    slash_count = ref.count("/")
    if slash_count == 2:
        owner, repo, name = ref.split("/", 2)
        if not owner or not repo or not name:
            return ()
        return _remote_ref_candidates_for_repo(kind, owner, repo, name)
    if slash_count != 1:
        return ()
    owner, name = ref.split("/", 1)
    if not owner or not name:
        return ()
    repositories: dict[EntryKind, tuple[tuple[str, str], ...]] = {
        "skill": (
            ("agents", f"skills/{name}"),
            ("agent-skills", name),
            ("agent-skills", f"skills/{name}"),
            ("skills", name),
            ("skills", f"skills/{name}"),
        ),
        "service": (
            ("agents", f"services/{name}.md"),
            ("agent-services", f"{name}.md"),
            ("services", f"{name}.md"),
        ),
        "prompt": (
            ("agents", f"prompts/{name}.md"),
            ("agent-prompts", f"{name}.md"),
            ("prompts", f"{name}.md"),
        ),
        "psyche": (
            ("agents", f"psyches/{name}.md"),
            ("agent-psyches", f"{name}.md"),
            ("psyches", f"{name}.md"),
        ),
    }
    return tuple(
        _github_remote_ref_with_default_branch(owner, repo, path)
        for repo, path in repositories[kind]
    )


def _remote_ref_candidates_for_repo(
    kind: EntryKind,
    owner: str,
    repo: str,
    name: str,
) -> tuple[str, ...]:
    return tuple(
        _github_remote_ref_with_default_branch(owner, repo, path)
        for path in _remote_path_candidates_for_repo(kind, repo, name)
    )


def _remote_path_candidates_for_repo(
    kind: EntryKind,
    repo: str,
    name: str,
) -> tuple[str, ...]:
    if kind == "skill":
        if repo in {"agent-skills", "skills"}:
            return (name, f"skills/{name}")
        return (f"skills/{name}", name)
    directory = DIR_NAME_BY_KIND[kind]
    if repo in {f"agent-{directory}", directory}:
        return (f"{name}.md",)
    return (f"{directory}/{name}.md", f"{name}.md")


def _github_remote_ref_with_default_branch(
    owner: str,
    repo: str,
    path: str,
) -> str:
    try:
        rev = _github_repo_default_branch(owner, repo)
    except ValueError:
        rev = "main"
    return GitHubRef(owner=owner, repo=repo, path=path, rev=rev).render()


def _github_remote_exists(kind: EntryKind, ref: str) -> bool:
    github_ref = parse_github_ref(ref)
    probe_ref = github_ref
    if kind == "skill":
        probe_ref = GitHubRef(
            owner=github_ref.owner,
            repo=github_ref.repo,
            path=str(Path(github_ref.path) / "SKILL.md"),
            rev=github_ref.rev,
        )
    request = Request(
        github_raw_url(probe_ref),
        method="HEAD",
        headers={"User-Agent": "toolang/0.1"},
    )
    try:
        with urlopen(request, timeout=30):
            return True
    except (HTTPError, URLError):
        return False


@lru_cache
def _github_repo_default_branch(owner: str, repo: str) -> str:
    api_url = (
        f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
    )
    request = Request(
        api_url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "toolang/0.1",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"could not resolve GitHub default branch: {owner}/{repo}"
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("default_branch"), str):
        raise ValueError(f"unexpected GitHub repository response: {owner}/{repo}")
    return data["default_branch"]


def _load_optional_toml(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    return cast(dict[str, object], tomllib.loads(path.read_text(encoding="utf-8")))


def _config_kind_table(data: dict[str, object], kind: EntryKind) -> dict[str, object]:
    key = DIR_NAME_BY_KIND[kind]
    table = data.get(key)
    if isinstance(table, dict):
        return cast(dict[str, object], table)
    new_table: dict[str, object] = {}
    data[key] = new_table
    return new_table


def _config_kind_table_optional(
    data: dict[str, object],
    kind: EntryKind,
) -> dict[str, object] | None:
    table = data.get(DIR_NAME_BY_KIND[kind])
    return cast(dict[str, object], table) if isinstance(table, dict) else None


def _config_ref(item: object) -> str:
    if isinstance(item, dict):
        ref = cast(dict[str, object], item).get("ref")
        if isinstance(ref, str) and ref:
            return ref
    raise ValueError(f"invalid remote cap config entry: {item!r}")


def _write_config_data(path: Path, data: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_config(dict(data)), encoding="utf-8")


def _render_config(data: dict[str, object]) -> str:
    remote_sections = set(CONFIG_SECTION_ORDER)
    standard_data = {
        key: value for key, value in data.items() if key not in remote_sections
    }
    remote_data = {
        key: value for key, value in data.items() if key in remote_sections
    }
    parts: list[str] = []
    standard_text = tomli_w.dumps(standard_data).strip()
    if standard_text:
        parts.append(standard_text)
    ctx = TomlContext(allow_multiline=False, indent=4)
    lines: list[str] = []
    for key, value in sorted(
        remote_data.items(),
        key=lambda item: (CONFIG_SECTION_ORDER.get(item[0], 999), item[0]),
    ):
        if isinstance(value, dict):
            lines.append(f"[{format_key_part(key)}]")
            for entry_name, entry_value in sorted(value.items()):
                if not isinstance(entry_value, Mapping):
                    raise TypeError(
                        f"invalid config entry for {key}.{entry_name}: {entry_value!r}"
                    )
                rendered = format_inline_table(
                    cast(Mapping[str, object], entry_value),
                    ctx,
                )
                lines.append(f"{format_key_part(str(entry_name))} = {rendered}")
            lines.append("")
        else:
            lines.append(f"{format_key_part(key)} = {format_literal(value, ctx)}")
    remote_text = "\n".join(lines).rstrip()
    if remote_text:
        parts.append(remote_text)
    return "\n\n".join(parts).rstrip() + ("\n" if parts else "")


def _load_meta(path: Path) -> dict[str, object]:
    post = frontmatter.loads(path.read_text(encoding="utf-8"))
    return cast(dict[str, object], _json_compatible(dict(post.metadata)))


def _json_compatible(value: object) -> object:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list | tuple):
        return [_json_compatible(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_compatible(item) for key, item in value.items()}
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat())
    return str(value)
