"""Declared-source Python admission uses effective metadata and the target."""

from __future__ import annotations

from pathlib import Path

import pytest

from nab_provider._vendor.packaging.specifiers import SpecifierSet
from nab_provider._vendor.packaging.version import Version
from nab_provider.errors import IncompatiblePythonError
from nab_provider.metadata import WheelMetadata
from nab_provider.policy import (
    ArchiveSource,
    LocalSource,
    SourceMaterialization,
    VcsSource,
)
from nab_provider.provider import Provider
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget
from nab_provider.testing import make_coordinator, pkg_override
from nab_provider.vcs_admission import VcsConfig, VcsPolicy


@pytest.mark.parametrize("kind", ["local", "vcs", "archive"])
@pytest.mark.parametrize(
    ("requires_python", "override", "selector", "python", "admitted"),
    [
        (">=3.12", None, "foo", "3.11", False),
        (">=3.12", None, "foo", "3.12", True),
        (None, None, "foo", "3.11", True),
        (">=3.12", "", "foo", "3.11", True),
        (">=3.12", ">=3.11", "foo", "3.11", True),
        (">=3.11", ">=3.12", "foo", "3.11", False),
        (">=3.12", "", "foo==1.0", "3.11", False),
        (">=3.11.5", None, "foo", "3.11", True),
    ],
)
def test_source_python_admission(
    tmp_path: Path,
    kind: str,
    requires_python: str | None,
    override: str | None,
    selector: str,
    python: str,
    *,
    admitted: bool,
) -> None:
    metadata = WheelMetadata(
        name="foo",
        version=Version("1.0"),
        requires_python=(
            SpecifierSet(requires_python) if requires_python is not None else None
        ),
    )
    coordinator = make_coordinator(
        materialize=lambda *_: SourceMaterialization(tmp_path, metadata, None)
    )
    provider = Provider(
        coordinator,
        target=ResolveTarget.for_declared(
            python_version=python, spec=PlatformSpec("linux_x86_64")
        ),
        local_sources=[LocalSource("foo", str(tmp_path))] if kind == "local" else [],
        vcs_sources=[VcsSource("foo", "git+https://example.com/foo.git@" + "a" * 40)]
        if kind == "vcs"
        else [],
        archive_sources=[
            ArchiveSource("foo", "https://example.com/foo.tar.gz#sha256=" + "a" * 64)
        ]
        if kind == "archive"
        else [],
        vcs_config=VcsConfig(
            policy=VcsPolicy.ALLOW,
            allowed_schemes=frozenset({"git+https"}),
            allowed_repos=("https://example.com/",),
        ),
        package_overrides=(pkg_override(selector, requires_python=override),)
        if override is not None
        else (),
    )
    key = ("foo", Version("1.0"))
    for _ in range(2):
        if admitted:
            assert provider.get_dependencies(*key) == {}
        else:
            with pytest.raises(
                IncompatiblePythonError, match="foo 1.0 requires Python"
            ):
                provider.get_dependencies(*key)
            assert key not in provider.deps_cache

    assert len(coordinator.calls_to("request_source_listing")) == 1
    assert metadata.requires_python == (
        SpecifierSet(requires_python) if requires_python is not None else None
    )
