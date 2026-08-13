"""Property tests for the provider's wheel-tag filter.

The provider implements `PEP 425`_'s wheel-tag compatibility algorithm
and `PEP 517`_'s wheel-vs-sdist preference.  Each invariant the filter
must preserve gets a property test.

The oracle is built from the generated listing directly rather than
from a second provider: the filter is the base provider's, so the only
provider that does not apply it is one with no target, which is itself
one of the properties below.

.. _PEP 425: https://peps.python.org/pep-0425/
.. _PEP 517: https://peps.python.org/pep-0517/
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_index.client import SdistFile, WheelFile
from nab_project._testing.coordinator_fake import FakeFetchPort, make_coordinator
from nab_provider._vendor.packaging.version import Version
from nab_provider.provider import BuildPolicy, DistPolicy, Provider
from nab_provider.tags import PlatformSpec, TagSet
from nab_provider.target import ResolveTarget

from .strategies import PROPERTY_SETTINGS

pytestmark = pytest.mark.property

_SPEC = PlatformSpec("linux_x86_64")
_LINUX_TARGET = ResolveTarget.for_declared(python_version="3.11", spec=_SPEC)


def compatible(wheel: WheelFile, spec: PlatformSpec) -> bool:
    """True iff a CPython 3.11 target on ``spec`` would install ``wheel``."""
    return TagSet.for_spec(python_version="3.11", spec=spec).pick([wheel]) is not None


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
    """Generate a random ``cpXY-<abi>-<platform>`` tag."""
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


def coordinator(files: list[WheelFile | SdistFile]) -> FakeFetchPort:
    """Build a fetch port that serves ``files`` as ``pkg``'s listing."""
    return make_coordinator(files, package="pkg")


class TestNoTargetIsNoFilter:
    """With no target the provider keeps every wheel.

    Nothing has said which machine the resolve is for, so there is no
    tag set to filter by.
    """

    @given(files=listing())
    @PROPERTY_SETTINGS
    def test_no_target_keeps_every_file(
        self, files: list[WheelFile | SdistFile]
    ) -> None:
        """Every generated file survives when the provider has no target."""
        provider = Provider(coordinator(files))
        out = provider.filter_distributions("pkg", files)
        assert {dist.filename for _, dist in out} == {f.filename for f in files}
        assert provider.stats.excluded_by_wheel_tags == 0


class TestFilterOnlyRemoves:
    """The tag filter only removes entries; it never adds or rewrites.

    Another filter's verdict, such as a PEP 592 yank, therefore survives it.
    """

    @given(files=listing())
    @PROPERTY_SETTINGS
    def test_output_is_subset_of_input(
        self, files: list[WheelFile | SdistFile]
    ) -> None:
        provider = Provider(
            coordinator(files),
            target=_LINUX_TARGET,
            build_policy=BuildPolicy.NEVER,
        )
        out = provider.filter_distributions("pkg", files)
        assert {(v, d.filename) for v, d in out}.issubset(
            {(Version(f.version), f.filename) for f in files}
        )


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
        provider = Provider(
            coordinator(files),
            target=_LINUX_TARGET,
            build_policy=BuildPolicy.BUILD_REMOTE,
        )
        out = provider.filter_distributions("pkg", files)
        for _v, dist in out:
            if isinstance(dist, WheelFile):
                assert compatible(dist, _SPEC), (
                    f"Incompatible wheel survived: {dist.filename}"
                )


class TestVersionAdmissionPolicy:
    """A version survives the wheel-tag filter iff at least one usable
    artifact remains.  A compatible wheel OR an sdist keeps the version
    alive at every ``BuildPolicy`` level; look-ahead, not this filter,
    rejects an unreadable sdist under ``BuildPolicy.NEVER``.
    """

    @staticmethod
    def _admitted(files: list[WheelFile | SdistFile]) -> set[Version]:
        """The versions the listing leaves installable on the target."""
        return {
            Version(f.version)
            for f in files
            if not isinstance(f, WheelFile) or compatible(f, _SPEC)
        }

    @given(files=listing())
    @PROPERTY_SETTINGS
    def test_version_admitted_iff_wheel_or_allowed_sdist(
        self, files: list[WheelFile | SdistFile]
    ) -> None:
        """A version survives iff it has a compatible wheel or an sdist."""
        provider = Provider(
            coordinator(files),
            target=_LINUX_TARGET,
            build_policy=BuildPolicy.BUILD_REMOTE,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
        )
        out = provider.filter_distributions("pkg", files)
        assert {v for v, _ in out} == self._admitted(files)

    @given(files=listing())
    @PROPERTY_SETTINGS
    def test_never_policy_keeps_sdist_only_versions(
        self, files: list[WheelFile | SdistFile]
    ) -> None:
        """Under NEVER, sdist-only versions still survive the filter."""
        provider = Provider(
            coordinator(files),
            target=_LINUX_TARGET,
            build_policy=BuildPolicy.NEVER,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
        )
        out = provider.filter_distributions("pkg", files)
        assert {v for v, _ in out} == self._admitted(files)
