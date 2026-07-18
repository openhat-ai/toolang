"""Shared paths for the Toolang test suite."""

from pathlib import Path


TESTS_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_ROOT.parent
FIXTURES_ROOT = TESTS_ROOT / "fixtures"
