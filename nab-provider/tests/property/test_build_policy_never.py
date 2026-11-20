"""Property test: no sdist build runs unless :attr:`BuildPolicy.BUILD_REMOTE`.

Fuzzes sdist PKG-INFO shapes (Metadata-Version, Dynamic field
case/content, Requires-Dist lines) and the static-pyproject fallback,
across BuildPolicy NEVER / BUILD_LOCAL / BUILD_REMOTE. The fetch port
records requests for a host metadata build.

Invariants:

* A host metadata build is requested only when the effective policy is
  BUILD_REMOTE (NEVER and BUILD_LOCAL must not build a remote sdist);
* under a non-building policy, dynamic deps without a static
  pyproject fallback raise UnsupportedSdistError (loud refusal, not a
  silent guess: PEP 643 untrusted deps must not be used);
* PEP 643-static PKG-INFO (2.2+, no Dynamic dependency field) never
  triggers a build under any policy: the metadata is authoritative.
"""

from __future__ import annotations

from typing import TypedDict

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_provider._vendor.packaging.version import Version
from nab_provider.metadata import WheelMetadata
from nab_provider.provider import (
    BuildPolicy,
    DistPolicy,
    Provider,
    UnsupportedSdistError,
)
from nab_provider.target import ResolveTarget
from nab_provider.testing import make_coordinator, make_sdist

from .settings import PROPERTY_SETTINGS

pytestmark = pytest.mark.property

BUILT = WheelMetadata(
    name="pkg",
    version=Version("1.0"),
    requires_python=None,
    requires_dist=[],
    provides_extra=[],
)

STATIC_PYPROJECT = {
    "project": {"name": "pkg", "version": "1.0", "dependencies": ["depz"]}
}


class MetadataFields(TypedDict):
    metadata_version: str
    dynamic: str | None
    deps: list[str]


pkg_info_shapes = st.fixed_dictionaries(
    {
        "metadata_version": st.sampled_from(["2.1", "2.2", "2.3", "2.4"]),
        "dynamic": st.sampled_from(
            [None, "Requires-Dist", "requires-dist", "REQUIRES-DIST", "Provides-Extra"]
        ),
        "deps": st.lists(
            st.sampled_from(["depa", "depb>=1.0", 'depc; python_version >= "3"']),
            max_size=2,
        ),
    }
)

policies = st.sampled_from(
    [BuildPolicy.NEVER, BuildPolicy.BUILD_LOCAL, BuildPolicy.BUILD_REMOTE]
)


def _pkg_info(shape: MetadataFields) -> str:
    lines = [
        f"Metadata-Version: {shape['metadata_version']}",
        "Name: pkg",
        "Version: 1.0",
    ]
    if shape["dynamic"] is not None:
        lines.append(f"Dynamic: {shape['dynamic']}")
    lines.extend(f"Requires-Dist: {d}" for d in shape["deps"])
    return "\n".join(lines) + "\n"


def _deps_are_static(shape: MetadataFields) -> bool:
    """PEP 643: 2.2+ metadata whose dependency fields are not Dynamic."""
    if shape["metadata_version"] == "2.1":
        return False
    dyn = (shape["dynamic"] or "").lower()
    return dyn not in {"requires-dist", "provides-extra"}


@PROPERTY_SETTINGS
@given(shape=pkg_info_shapes, policy=policies, with_fallback=st.booleans())
def test_build_only_under_build_remote(
    shape: MetadataFields,
    policy: BuildPolicy,
    with_fallback: bool,
) -> None:
    coordinator = make_coordinator(
        [make_sdist("1.0", package="pkg")],
        sdist_pkg_info=_pkg_info(shape),
        sdist_pyproject=STATIC_PYPROJECT if with_fallback else None,
        build_sdist=lambda *_args: BUILT,
    )
    provider = Provider(
        coordinator,
        target=ResolveTarget.for_host_python("3.12.0"),
        dist_policy=DistPolicy.WHEEL_OR_SDIST,
        build_policy=policy,
    )

    needs_build = not _deps_are_static(shape) and not with_fallback
    allows_build = policy is BuildPolicy.BUILD_REMOTE
    if needs_build and not allows_build:
        with pytest.raises(UnsupportedSdistError):
            provider.get_dependencies("pkg", Version("1.0"))
    else:
        provider.get_dependencies("pkg", Version("1.0"))

    calls = coordinator.calls_to("build_sdist_metadata")
    expected = [("pkg", "1.0")] if needs_build and allows_build else []
    assert [args[:2] for args in calls] == expected
