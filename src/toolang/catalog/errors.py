"""Catalog-specific errors."""

from __future__ import annotations


class CatalogError(Exception):
    """Base error for authored catalog operations."""


class CatalogNotFoundError(CatalogError, FileNotFoundError):
    """An authored catalog entry was not found."""


class CatalogConflictError(CatalogError, FileExistsError):
    """An authored catalog mutation conflicts with an existing entry."""
