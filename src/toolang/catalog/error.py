"""Catalog-specific errors."""

from __future__ import annotations

from pathlib import Path


class CatalogError(Exception):
    """Base error for authored catalog operations."""


class CatalogNotFoundError(CatalogError, FileNotFoundError):
    """An authored catalog entry was not found."""


class CatalogConflictError(CatalogError, FileExistsError):
    """An authored catalog mutation conflicts with an existing entry."""


class DuplicateJobIdError(CatalogConflictError):
    """Two authored job files declare the same stable id."""

    def __init__(self, job_id: str, *, path: Path, existing_path: Path) -> None:
        self.job_id = job_id
        self.path = path
        self.existing_path = existing_path
        super().__init__(
            f"duplicate job id {job_id!r} in {path}; already declared by {existing_path}"
        )
