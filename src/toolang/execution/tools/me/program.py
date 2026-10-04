"""Validated declaration and whole-source edits to the current main program."""

from __future__ import annotations

from contextlib import AbstractContextManager
from hashlib import sha256
from pathlib import Path
from typing import Any

from toolang.catalog.errors import CatalogConflictError, CatalogNotFoundError
from toolang.common.files import atomic_write_text
from toolang.common.layout import AgentLayout
from toolang.lang.source_edit import (
    SourceDeclaration,
    declaration_fragment,
    replace_declaration,
    source_declarations,
)

from .flows import AuthoredFlows, DigestMismatchError, validate_program_candidate
from .storage import UnsafeAuthoringPathError, program_write_lock, require_regular_file


class AuthoredProgram:
    """Edit main source using the same lock as authored flow composition."""

    def __init__(self, layout: AgentLayout, run_digest: str | None = None) -> None:
        self.layout = layout
        self.run_digest = run_digest

    def list(self) -> dict[str, Any]:
        with self._lock():
            source, digest = self._read()
            return {
                "kind": "program",
                "version": self._version(digest),
                "items": [
                    self._item(source, digest, item, content=False)
                    for item in source_declarations(source)
                ],
            }

    def get(self, key: str | None) -> dict[str, Any]:
        with self._lock():
            source, digest = self._read()
            declaration = self._find(source, key) if key is not None else None
            return {
                "kind": "program",
                "version": self._version(digest),
                "item": self._item(source, digest, declaration),
            }

    def write(
        self,
        operation: str,
        key: str | None,
        source: str | None,
        if_digest: str,
    ) -> dict[str, Any]:
        with self._lock():
            path = self._path()
            encoded = path.read_bytes()
            digest = sha256(encoded).hexdigest()
            if digest != if_digest:
                raise DigestMismatchError(
                    kind="program", key=key or "", expected=if_digest, actual=digest
                )
            if key is None:
                assert operation == "update" and source is not None
                candidate = source
            else:
                current = encoded.decode("utf-8")
                if operation == "create":
                    if any(item.key == key for item in source_declarations(current)):
                        raise CatalogConflictError(
                            f"program declaration already exists: {key}"
                        )
                    assert source is not None
                    fragment = declaration_fragment(source, key)
                    separator = (
                        ""
                        if not current or current.endswith("\n\n")
                        else ("\n" if current.endswith("\n") else "\n\n")
                    )
                    candidate = current + separator + fragment
                else:
                    declaration = self._find(current, key)
                    replacement = (
                        ""
                        if operation == "delete"
                        else declaration_fragment(
                            source if source is not None else "", key
                        )
                    )
                    candidate = replace_declaration(current, declaration, replacement)
            candidate_bytes = candidate.encode("utf-8")
            # Reuse flow storage checks, including rejection of linked flow files.
            AuthoredFlows(self.layout).validate_source_storage()
            validate_program_candidate(
                self.layout, self.layout.program, candidate_bytes
            )
            changed = candidate_bytes != encoded
            result: dict[str, Any] = {
                "kind": "program",
                "version": self._version(sha256(candidate_bytes).hexdigest()),
            }
            if operation == "delete":
                result.update(key=key, deleted=True)
            else:
                declaration = self._find(candidate, key) if key is not None else None
                result["item"] = self._item(
                    candidate, sha256(candidate_bytes).hexdigest(), declaration
                )
                result["created" if operation == "create" else "changed"] = (
                    True if operation == "create" else changed
                )
            if changed:
                atomic_write_text(path, candidate)
            return result

    def _path(self) -> Path:
        path = self.layout.program
        if path.is_symlink():
            expected = self.layout.root.parent / f"{self.layout.name}.too"
            if (
                self.layout.placement != "roaming"
                or expected.is_symlink()
                or path.resolve() != expected
            ):
                raise UnsafeAuthoringPathError(
                    "program source must not be an arbitrary symbolic link"
                )
            path = expected
        require_regular_file(path, "program source")
        if not path.is_file():
            raise CatalogNotFoundError("current main program not found")
        return path

    def _read(self) -> tuple[str, str]:
        encoded = self._path().read_bytes()
        return encoded.decode("utf-8"), sha256(encoded).hexdigest()

    def _find(self, source: str, key: str) -> SourceDeclaration:
        for declaration in source_declarations(source):
            if declaration.key == key:
                return declaration
        raise CatalogNotFoundError(f"program declaration not found: {key}")

    def _item(
        self,
        source: str,
        digest: str,
        declaration: SourceDeclaration | None,
        *,
        content: bool = True,
    ) -> dict[str, Any]:
        selected = declaration.source if declaration is not None else source
        item: dict[str, Any] = {
            "key": declaration.key if declaration is not None else None,
            "path": "agent.too",
            "digest": digest,
            "bytes": len(selected.encode("utf-8")),
        }
        if declaration is not None:
            item["line"] = declaration.line
        if content:
            item["content"] = {"source": selected}
        return item

    def _lock(self) -> AbstractContextManager[None]:
        return program_write_lock(self.layout.home)

    def _version(self, authored_digest: str) -> dict[str, Any]:
        return {
            "run_digest": self.run_digest,
            "authored_digest": authored_digest,
            "matches_run": (authored_digest == self.run_digest)
            if self.run_digest is not None
            else None,
        }
