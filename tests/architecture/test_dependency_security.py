"""Keep audited runtime dependencies above known vulnerable versions."""

import tomllib
from importlib.metadata import requires

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

from tests import PROJECT_ROOT


@pytest.mark.parametrize(
    ("name", "minimum_version"),
    [
        pytest.param("pyjwt", "2.15.0", id="CVE-2026-101918"),
        pytest.param("urllib3", "2.8.0", id="CVE-2026-97687-97688-97689"),
    ],
)
def test_locked_dependencies_include_security_fixes(
    name: str, minimum_version: str
) -> None:
    lock = tomllib.loads((PROJECT_ROOT / "uv.lock").read_text(encoding="utf-8"))
    versions = [
        Version(package["version"])
        for package in lock["package"]
        if package["name"] == name
    ]
    assert versions, f"Expected {name} in the runtime dependency lock"
    assert all(version >= Version(minimum_version) for version in versions), (
        f"{name} must be >= {minimum_version}; locked versions: {versions}"
    )


@pytest.mark.parametrize(
    ("name", "minimum_version", "vulnerable_version"),
    [
        ("pyjwt", "2.15.0", "2.14.0"),
        ("urllib3", "2.8.0", "2.7.0"),
    ],
)
def test_distribution_requires_security_fixes(
    name: str, minimum_version: str, vulnerable_version: str
) -> None:
    requirements = [Requirement(value) for value in requires("toolang") or []]
    matching = [
        requirement
        for requirement in requirements
        if canonicalize_name(requirement.name) == name
    ]
    assert matching, f"Published metadata must require {name} directly"
    for requirement in matching:
        assert requirement.marker is None
        assert minimum_version in requirement.specifier
        assert vulnerable_version not in requirement.specifier
