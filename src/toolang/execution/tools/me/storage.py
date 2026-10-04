"""Whitelisted current-home file storage with exact-byte concurrency checks."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from hashlib import sha256
from pathlib import Path

from toolang.catalog.types import CAP_KIND_BY_DIR
from toolang.common.files import atomic_write_bytes, file_lock_path, file_write_lock
from toolang.common.layout import AgentLayout

from .errors import DigestMismatchError, UnsafeAuthoringPathError
from .types import HomeFile, HomeFileCategory

_SINGLE_FILES: dict[str, HomeFileCategory] = {
    "agent.too": "program",
    "config.toml": "config",
}
_DIRECTORIES: dict[str, tuple[str, HomeFileCategory]] = {
    "flows": (".too", "program"),
    **{
        directory: (".md", "cap")
        for directory, kind in CAP_KIND_BY_DIR.items()
        if kind != "skill"
    },
    "tasks": (".md", "job"),
    "chores": (".md", "job"),
}


def classify(key: str) -> HomeFile:
    """Classify a canonical key; request decoding has already rejected traversal."""
    path = Path(key)
    parts = path.parts
    if category := _SINGLE_FILES.get(key):
        return HomeFile(key, category)
    if len(parts) == 2 and (rule := _DIRECTORIES.get(parts[0])):
        suffix, category = rule
        if path.suffix == suffix and path.stem:
            return HomeFile(key, category)
    if len(parts) >= 3 and parts[0] == "skills":
        if len(parts) == 3 and parts[2] == "SKILL.md":
            return HomeFile(key, "cap")
        if len(parts) >= 4 and parts[2] == "assets":
            return HomeFile(key, "asset")
    raise ValueError("key is outside the supported home file paths")


class HomeFiles:
    def __init__(self, layout: AgentLayout) -> None:
        self.layout = layout
        self.home = layout.home
        self._names: dict[Path, set[str]] = {}

    def path(self, file: HomeFile) -> Path:
        self._directory(self.home)
        current = self.home
        for part in Path(file.key).parts[:-1]:
            current /= part
            self._directory(current)
            self._exact_name(current)
        target = self.home / file.key
        self._exact_name(target)
        if (
            target.is_symlink()
            and file.key == "agent.too"
            and self.layout.placement == "roaming"
        ):
            try:
                source = target.resolve()
                # Placement owns the mapping from source names to home identity.
                # Reconstructing a filename loses whitespace normalized by it.
                if (
                    source.suffix == ".too"
                    and AgentLayout.roaming(source) == self.layout
                ):
                    target = source
            except RuntimeError as exc:
                # Python 3.11/3.12 raise RuntimeError for a symlink loop.
                raise UnsafeAuthoringPathError(
                    "roaming program must link to its own source file"
                ) from exc
        self._regular(target)
        return target

    def keys(self) -> tuple[str, ...]:
        """Walk only allowed locations, never following directory links."""
        keys: list[str] = []
        for key in _SINGLE_FILES:
            path = self.path(classify(key))
            if path.is_file():
                keys.append(key)
        for directory, (suffix, _category) in _DIRECTORIES.items():
            path = self.home / directory
            self._directory(path)
            if not path.exists():
                continue
            for entry in sorted(path.iterdir()):
                if entry.suffix == suffix:
                    key = entry.relative_to(self.home).as_posix()
                    self.path(classify(key))
                    keys.append(key)
        skills = self.home / "skills"
        self._directory(skills)
        if skills.exists():
            for skill in sorted(skills.iterdir()):
                if skill.is_symlink():
                    raise UnsafeAuthoringPathError(
                        "skill directory must not be a symbolic link"
                    )
                if not skill.is_dir():
                    continue
                definition = skill / "SKILL.md"
                self._regular(definition)
                if definition.is_file():
                    keys.append(definition.relative_to(self.home).as_posix())
                assets = skill / "assets"
                self._directory(assets)
                if assets.exists():
                    keys.extend(self._asset_keys(assets))
        return tuple(sorted(keys))

    def _asset_keys(self, directory: Path) -> Iterator[str]:
        for path in sorted(directory.iterdir()):
            if path.is_symlink():
                raise UnsafeAuthoringPathError("asset must not be a symbolic link")
            if path.is_dir():
                yield from self._asset_keys(path)
            else:
                self._regular(path)
                yield path.relative_to(self.home).as_posix()

    def read(self, file: HomeFile) -> bytes:
        path = self.path(file)
        if not path.is_file():
            raise FileNotFoundError(f"home file not found: {file.key}")
        return path.read_bytes()

    def metadata(self, file: HomeFile) -> dict[str, object]:
        digest = sha256()
        size = 0
        with self.path(file).open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        return {"key": file.key, "digest": digest.hexdigest(), "bytes": size}

    def before_write(
        self, file: HomeFile, *, create: bool, expected: str | None
    ) -> bytes | None:
        path = self.path(file)
        if create:
            # Portable identity: do not create a case-only sibling on Linux.
            if path.exists() or (
                path.parent.is_dir()
                and any(
                    sibling.name.casefold() == path.name.casefold()
                    for sibling in path.parent.iterdir()
                )
            ):
                raise FileExistsError("home file already exists")
            return None
        current = self.read(file)
        actual = sha256(current).hexdigest()
        if actual != expected:
            raise DigestMismatchError(expected or "", actual)
        return current

    def save(self, file: HomeFile, content: bytes | None) -> None:
        path = self.path(file)
        if content is None:
            path.unlink()
        else:
            atomic_write_bytes(path, content)

    @contextmanager
    def lock(self, file: HomeFile | None = None) -> Iterator[None]:
        targets = {
            "program": ("agent.too",)
            if file and file.key == "agent.too"
            else ("flows",),
            "config": ("config.toml",),
            "cap": ("caps",),
            "asset": ("caps",),
            "job": ("jobs",),
        }
        names = (
            targets[file.category]
            if file is not None
            else ("agent.too", "flows", "config.toml", "caps", "jobs")
        )
        self._directory(self.home)
        with ExitStack() as stack:
            for name in names:
                path = file_lock_path(self.home / name)
                self._regular(path)
                stack.enter_context(file_write_lock(path))
            try:
                yield
            finally:
                self._names.clear()

    def _exact_name(self, path: Path) -> None:
        if not path.exists() and not path.is_symlink():
            return
        # Cache directory spellings only for this locked operation. Listing many
        # assets must not rescan their parent for every file and path check.
        names = self._names.get(path.parent)
        if names is None:
            names = {entry.name for entry in path.parent.iterdir()}
            self._names[path.parent] = names
        if path.name not in names:
            raise ValueError(
                "key must use exact file and directory spelling; use list to find it"
            )

    @staticmethod
    def _regular(path: Path) -> None:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise UnsafeAuthoringPathError(
                "home files and locks must be regular files, not symbolic links"
            )

    @staticmethod
    def _directory(path: Path) -> None:
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise UnsafeAuthoringPathError(
                "home source directories must not be symbolic links or files"
            )
