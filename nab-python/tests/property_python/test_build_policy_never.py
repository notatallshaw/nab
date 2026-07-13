"""Property test: no sdist build runs unless :attr:`BuildPolicy.BUILD_REMOTE`.

Fuzzes sdist PKG-INFO shapes (Metadata-Version, Dynamic field
case/content, Requires-Dist lines) and the static-pyproject fallback,
across BuildPolicy NEVER / BUILD_LOCAL / BUILD_REMOTE.  Oracle is a
monkeypatched ``build_remote_sdist`` that records invocations.

Invariants:

* ``build_remote_sdist`` is invoked only when the effective policy is
  BUILD_REMOTE (NEVER and BUILD_LOCAL must not build a remote sdist);
* under a non-building policy, dynamic deps without a static
  pyproject fallback raise UnsupportedSdistError (loud refusal, not a
  silent guess: PEP 643 untrusted deps must not be used);
* PEP 643-static PKG-INFO (2.2+, no Dynamic dependency field) never
  triggers a build under any policy: the metadata is authoritative.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_index.client import SdistFile
from nab_python._provider import build_remote
from nab_python._testing.coordinator_fake import make_coordinator
from nab_python._vendor.packaging.version import Version
from nab_python.metadata import WheelMetadata
from nab_python.provider import (
    BuildPolicy,
    DistPolicy,
    Provider,
    UnsupportedSdistError,
)
from nab_python.target import ResolveTarget

from .strategies import PROPERTY_SETTINGS

pytestmark = pytest.mark.property

BUILT = WheelMetadata(
    name="pkg",
    version=Version("1.0"),
    requires_python=None,
    requires_dist=[],
    provides_extra=[],
)

STATIC_PYPROJECT = '[project]\nname = "pkg"\nversion = "1.0"\ndependencies = ["depz"]\n'


def _sdist(version: str = "1.0") -> SdistFile:
    return SdistFile(
        filename=f"pkg-{version}.tar.gz",
        url=f"https://example.com/pkg-{version}.tar.gz",
        version=version,
        requires_python=None,
        upload_time=None,
    )


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


def _pkg_info(shape: dict) -> str:
    lines = [
        f"Metadata-Version: {shape['metadata_version']}",
        "Name: pkg",
        "Version: 1.0",
    ]
    if shape["dynamic"] is not None:
        lines.append(f"Dynamic: {shape['dynamic']}")
    lines.extend(f"Requires-Dist: {d}" for d in shape["deps"])
    return "\n".join(lines) + "\n"


def _deps_are_static(shape: dict) -> bool:
    """PEP 643: 2.2+ metadata whose dependency fields are not Dynamic."""
    if shape["metadata_version"] == "2.1":
        return False
    dyn = (shape["dynamic"] or "").lower()
    return dyn not in {"requires-dist", "provides-extra"}


@PROPERTY_SETTINGS
@given(shape=pkg_info_shapes, policy=policies, with_fallback=st.booleans())
def test_build_only_under_build_remote(
    shape: dict,
    policy: BuildPolicy,
    with_fallback: bool,
) -> None:
    calls: list[tuple[str, str]] = []

    def _fake_build(provider: object, package: str, version: object) -> WheelMetadata:
        calls.append((package, str(version)))
        return BUILT

    with patch.object(build_remote, "build_remote_sdist", _fake_build):
        _run_case(shape, policy, with_fallback, calls)


def _run_case(
    shape: dict, policy: BuildPolicy, with_fallback: bool, calls: list[tuple[str, str]]
) -> None:
    coordinator = make_coordinator(
        [_sdist("1.0")],
        sdist_pkg_info=_pkg_info(shape),
        sdist_pyproject_toml=STATIC_PYPROJECT if with_fallback else None,
    )
    provider = Provider(
        coordinator,
        target=ResolveTarget.for_host_python("3.12.0"),
        dist_policy=DistPolicy.WHEEL_OR_SDIST,
        build_policy=policy,
    )

    static = _deps_are_static(shape)
    try:
        deps = provider.get_dependencies("pkg", Version("1.0"))
    except UnsupportedSdistError:
        deps = None
        assert not static, "static PEP 643 metadata must not be refused"
        assert policy is not BuildPolicy.BUILD_REMOTE
        assert not with_fallback, "static pyproject fallback should have applied"

    if policy is not BuildPolicy.BUILD_REMOTE:
        assert calls == [], f"sdist build ran under {policy}: {calls}"
    if static:
        assert calls == [], "PEP 643-static metadata must never trigger a build"
        assert deps is not None
    if not static and deps is not None and not with_fallback:
        # Dynamic deps with no fallback: only a build can have produced deps.
        assert policy is BuildPolicy.BUILD_REMOTE
        assert calls == [("pkg", "1.0")]
