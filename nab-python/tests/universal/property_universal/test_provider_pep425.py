"""Property tests for the ``UniversalProvider`` wheel-tag filter.

The universal provider implements `PEP 425`_'s wheel-tag
compatibility algorithm and `PEP 517`_'s wheel-vs-sdist preference.
This file walks the relevant clauses paragraph by paragraph and
adds a property test for each invariant the filter must preserve.

.. _PEP 425: https://peps.python.org/pep-0425/
.. _PEP 517: https://peps.python.org/pep-0517/
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_index.client import SdistFile, WheelFile
from nab_python._testing.coordinator_fake import make_coordinator
from nab_python.provider import BuildPolicy, DistPolicy
from nab_python.universal.provider import UniversalProvider
from nab_python.universal.wheel_selection import (
    PlatformSpec,
    wheel_compatible_with_tuple,
)

from .strategies import LINUX_ENV, PROPERTY_SETTINGS

pytestmark = pytest.mark.property


_PLATFORM_TAGS = (
    "manylinux_2_17_x86_64",
    "manylinux_2_28_x86_64",
    "manylinux_2_34_x86_64",
    "manylinux2014_x86_64",
    "musllinux_1_2_x86_64",
    "linux_x86_64",
    "win_amd64",
    "macosx_11_0_arm64",
    "macosx_11_0_x86_64",
    "any",
)


@st.composite
def wheel_tags(draw: st.DrawFn) -> str:
    """Generate a random ``cpXY-cpXY-<platform>`` or ``py3-none-any`` tag."""
    py_minor = draw(st.sampled_from(("39", "310", "311", "312", "313")))
    abi = draw(st.sampled_from((f"cp{py_minor}", "abi3", "none")))
    plat = draw(st.sampled_from(_PLATFORM_TAGS))
    return f"cp{py_minor}-{abi}-{plat}"


@st.composite
def listing(draw: st.DrawFn) -> list[WheelFile | SdistFile]:
    """Generate a random listing of wheels (and optional sdist) for one version."""
    version = draw(st.sampled_from(("1.0", "1.1", "2.0")))
    n_wheels = draw(st.integers(min_value=0, max_value=4))
    files: list[WheelFile | SdistFile] = []
    for _ in range(n_wheels):
        tag = draw(wheel_tags())
        filename = f"pkg-{version}-{tag}.whl"
        files.append(
            WheelFile(
                filename=filename,
                url=f"https://example.com/{filename}",
                version=version,
                requires_python=None,
                has_metadata=True,
                upload_time=None,
            )
        )
    if draw(st.booleans()):
        files.append(
            SdistFile(
                filename=f"pkg-{version}.tar.gz",
                url=f"https://example.com/pkg-{version}.tar.gz",
                version=version,
                requires_python=None,
                upload_time=None,
            )
        )
    return files


def coordinator(files: list[WheelFile | SdistFile]) -> MagicMock:
    """Build a coordinator stub that returns ``files`` for ``pkg``."""
    return make_coordinator(files, package="pkg")


class TestNoPlatformSpecIsNoOp:
    """When ``platform_spec=None`` the universal provider must return
    the parent provider's full listing unchanged.

    The override delegates to ``super().filter_distributions`` and
    applies no extra filter; a regression here would silently change
    the candidate set for non-universal flows, where there is no
    platform pin to apply.
    """

    @given(files=listing())
    @PROPERTY_SETTINGS
    def test_no_platform_spec_is_noop(self, files: list[WheelFile | SdistFile]) -> None:
        """Without ``platform_spec`` the override matches super()'s output."""
        provider = UniversalProvider(
            coordinator(files),
            marker_environment=LINUX_ENV,
        )
        super_out = UniversalProvider.__mro__[1].filter_distributions(
            provider, "pkg", files
        )
        out = provider.filter_distributions("pkg", files)
        assert out == super_out


class TestFilterIsSubsetOfParent:
    """The universal filter only removes entries; it never adds or
    rewrites.  Stated formally: ``output ⊆ parent_output``.

    This invariant ensures that any other filter applied to
    ``parent_output`` (for example PEP 592 yanked-version filters or
    admission filters) cannot be undone by the universal filter.
    """

    @given(files=listing())
    @PROPERTY_SETTINGS
    def test_output_is_subset_of_parent(
        self, files: list[WheelFile | SdistFile]
    ) -> None:
        """The override never adds entries; it only removes."""
        provider = UniversalProvider(
            coordinator(files),
            marker_environment=LINUX_ENV,
            platform_spec=PlatformSpec("linux_x86_64"),
            build_policy=BuildPolicy.NEVER,
        )
        super_out = UniversalProvider.__mro__[1].filter_distributions(
            provider, "pkg", files
        )
        out = provider.filter_distributions("pkg", files)
        super_pairs = {(v, d.filename) for v, d in super_out}
        out_pairs = {(v, d.filename) for v, d in out}
        assert out_pairs.issubset(super_pairs)


class TestOnlyCompatibleWheelsKept:
    """`PEP 425`_ defines the wheel compatibility-tag triple
    ``(python_tag, abi_tag, platform_tag)`` and the rules that
    determine whether a wheel is installable on a given environment.

    Every wheel surviving the filter must be tag-compatible with
    the platform spec.  Otherwise we would lock a wheel that cannot
    be installed on the target environment.

    .. _PEP 425: https://peps.python.org/pep-0425/
    """

    @given(files=listing())
    @PROPERTY_SETTINGS
    def test_no_incompatible_wheels_kept(
        self, files: list[WheelFile | SdistFile]
    ) -> None:
        """Every kept wheel is tag-compatible with the platform spec."""
        spec = PlatformSpec("linux_x86_64")
        provider = UniversalProvider(
            coordinator(files),
            marker_environment=LINUX_ENV,
            platform_spec=spec,
            build_policy=BuildPolicy.BUILD_REMOTE,
        )
        out = provider.filter_distributions("pkg", files)
        for _v, dist in out:
            if isinstance(dist, WheelFile):
                assert wheel_compatible_with_tuple(
                    dist, python_version="3.11", spec=spec
                ), f"Incompatible wheel survived: {dist.filename}"


class TestVersionAdmissionPolicy:
    """A version survives the wheel-tag filter iff at least one usable
    artifact remains.  Under ``BuildPolicy.BUILD_REMOTE`` an sdist
    (per `PEP 517`_'s build-from-sdist contract) counts as usable;
    under ``BuildPolicy.NEVER`` only a tag-compatible wheel counts.

    The two parameter combinations correspond to "wheel-only" and
    "wheel-or-sdist" install modes; the filter must distinguish
    between them.

    .. _PEP 517: https://peps.python.org/pep-0517/
    """

    @given(files=listing())
    @PROPERTY_SETTINGS
    def test_version_admitted_iff_wheel_or_allowed_sdist(
        self, files: list[WheelFile | SdistFile]
    ) -> None:
        """A version survives iff it has a compatible wheel or allowed sdist."""
        spec = PlatformSpec("linux_x86_64")
        provider = UniversalProvider(
            coordinator(files),
            marker_environment=LINUX_ENV,
            platform_spec=spec,
            build_policy=BuildPolicy.BUILD_REMOTE,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
        )
        super_out = UniversalProvider.__mro__[1].filter_distributions(
            provider, "pkg", files
        )
        out = provider.filter_distributions("pkg", files)
        out_versions = {v for v, _ in out}
        versions_with_compat_wheel: set = set()
        versions_with_sdist: set = set()
        for v, d in super_out:
            if isinstance(d, WheelFile):
                if wheel_compatible_with_tuple(d, python_version="3.11", spec=spec):
                    versions_with_compat_wheel.add(v)
            else:
                versions_with_sdist.add(v)
        expected_admitted = versions_with_compat_wheel | versions_with_sdist
        assert out_versions == expected_admitted

    @given(files=listing())
    @PROPERTY_SETTINGS
    def test_never_policy_drops_sdist_only_versions(
        self, files: list[WheelFile | SdistFile]
    ) -> None:
        """Under NEVER, a version is admitted iff a compatible wheel exists."""
        spec = PlatformSpec("linux_x86_64")
        provider = UniversalProvider(
            coordinator(files),
            marker_environment=LINUX_ENV,
            platform_spec=spec,
            build_policy=BuildPolicy.NEVER,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
        )
        super_out = UniversalProvider.__mro__[1].filter_distributions(
            provider, "pkg", files
        )
        out = provider.filter_distributions("pkg", files)
        out_versions = {v for v, _ in out}
        expected = {
            v
            for v, d in super_out
            if isinstance(d, WheelFile)
            and wheel_compatible_with_tuple(d, python_version="3.11", spec=spec)
        }
        assert out_versions == expected
