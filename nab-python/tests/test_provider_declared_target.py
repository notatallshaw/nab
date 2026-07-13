"""Tests for :class:`Provider` under a declared (non-host) target.

A declared target names the machine it resolves for, so its markers and
its wheel tags are synthesized rather than read off the host.  These
exercise the paths that only a declared target reaches: the wheel-tag
filter, cross-target preferences, the resolution strategy, and the
per-target Requires-Python patch level.  ``nab-python/tests/universal/``
drives the same provider through the matrix.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from nab_index.client import SdistFile, WheelFile
from nab_python._testing.coordinator_fake import make_coordinator
from nab_python._testing.overrides import pkg_override
from nab_python._vendor.packaging.ranges import VersionRange
from nab_python._vendor.packaging.requirements import Requirement
from nab_python._vendor.packaging.version import Version
from nab_python.config import NabProjectConfig
from nab_python.fetch import InMemoryIndex
from nab_python.provider import (
    BuildPolicy,
    DistPolicy,
    Provider,
    VcsConfig,
    VcsPolicy,
    VcsSource,
)
from nab_python.tags import PlatformSpec
from nab_python.target import ResolveTarget
from nab_resolver.resolver import Resolver

if TYPE_CHECKING:
    from pathlib import Path


def _done_event() -> threading.Event:
    """Return an already-set Event."""
    ev = threading.Event()
    ev.set()
    return ev


def _make_wheel(
    version: str,
    *,
    package: str = "pkg",
    requires_python: str | None = None,
) -> WheelFile:
    return WheelFile(
        filename=f"{package}-{version}-py3-none-any.whl",
        url=f"https://example.com/{package}-{version}.whl",
        version=version,
        requires_python=requires_python,
        has_metadata=True,
        upload_time=None,
    )


def _make_coordinator(
    wheels: Sequence[WheelFile | SdistFile],
    *,
    package: str = "pkg",
) -> MagicMock:
    """Mock FetchCoordinator with a pre-loaded listing for ``package``."""
    return make_coordinator(wheels, package=package, auto_metadata=True)


_LINUX_TARGET = ResolveTarget.for_declared(
    python_version="3.11", spec=PlatformSpec("linux_x86_64")
)


def _linux_target(spec: PlatformSpec) -> ResolveTarget:
    """A 3.11 linux target with declared tag knobs."""
    return ResolveTarget.for_declared(python_version="3.11", spec=spec)


class TestPerTupleRequiresPythonOverride:
    """A requires-python override participates in per-tuple python filtering."""

    def _provider(self, py: str) -> Provider:
        coordinator = make_coordinator(
            [_make_wheel("1.0", requires_python=">=3.6")],
            package="pkg",
            auto_metadata=True,
        )
        return Provider(
            coordinator,
            ResolveTarget.for_declared(
                python_version=py, spec=PlatformSpec("linux_x86_64")
            ),
            package_overrides=(pkg_override("pkg", requires_python=">=3.11"),),
        )

    def test_tuple_below_override_rejects(self) -> None:
        # Real >=3.6 admits 3.10, but the override narrows to >=3.11.
        provider = self._provider("3.10")
        assert provider.fetch_versions("pkg") == []

    def test_tuple_meeting_override_admits(self) -> None:
        provider = self._provider("3.11")
        assert [v for v, _ in provider.fetch_versions("pkg")] == [Version("1.0")]


class TestProvidesExtraDependenciesOverride:
    """A dependencies + provides-extra override gates deps behind the extra."""

    def _provider(self) -> Provider:
        coordinator = make_coordinator(
            [_make_wheel("1.0")],
            package="pkg",
            auto_metadata=True,
        )
        return Provider(
            coordinator,
            _LINUX_TARGET,
            package_overrides=(
                pkg_override(
                    "pkg",
                    dependencies=(Requirement('dep ; extra == "cli"'),),
                    provides_extra=("cli",),
                ),
            ),
        )

    def test_base_pkg_does_not_carry_extra_dep(self) -> None:
        """The extra-gated dep is absent from the base package's deps."""
        provider = self._provider()
        base_deps = provider.get_dependencies("pkg", Version("1.0"))
        assert "dep" not in base_deps

    def test_extra_proxy_carries_gated_dep(self) -> None:
        """Requesting ``pkg[cli]`` yields the extra-gated dep plus the base pin."""
        provider = self._provider()
        extra_deps = provider.get_dependencies("pkg[cli]", Version("1.0"))
        assert "dep" in extra_deps
        assert "pkg" in extra_deps


class TestEnvironmentOverlay:
    """The provider overlays user-supplied marker keys on the host env."""

    def test_user_keys_override_host_defaults(self) -> None:
        """The target's marker overrides take precedence over the host env."""
        coordinator = _make_coordinator([])
        provider = Provider(
            coordinator,
            ResolveTarget.for_host().with_marker_overrides(
                {"sys_platform": "win32", "python_version": "3.12"}
            ),
        )
        assert provider.environment["sys_platform"] == "win32"
        assert provider.environment["python_version"] == "3.12"

    def test_keys_not_supplied_keep_default(self) -> None:
        """Markers the user did not supply fall back to ``default_environment()``."""
        coordinator = _make_coordinator([])
        provider = Provider(
            coordinator,
            ResolveTarget.for_host().with_marker_overrides({"python_version": "3.11"}),
        )
        # implementation_name is not in our supplied dict; the host
        # default fills it in (cpython on standard CPython runs).
        assert "implementation_name" in provider.environment

    def test_env_with_extra_is_refreshed(self) -> None:
        """The hot-path ``env_with_extra`` cache reflects the overlay."""
        coordinator = _make_coordinator([])
        provider = Provider(
            coordinator,
            ResolveTarget.for_host().with_marker_overrides({"sys_platform": "win32"}),
        )
        assert provider.env_with_extra["sys_platform"] == "win32"


class TestTrustUnverifiedSdistDeps:
    """The trust-unverified flag is taken from ``build_config``."""

    def test_defaults_false_without_build_config(self) -> None:
        coordinator = _make_coordinator([])
        provider = Provider(coordinator, _LINUX_TARGET)
        assert provider.trust_unverified_sdist_deps is False

    def test_taken_from_build_config(self) -> None:
        coordinator = _make_coordinator([])
        provider = Provider(
            coordinator,
            _LINUX_TARGET,
            build_config=NabProjectConfig(trust_unverified_sdist_deps=True),
        )
        assert provider.trust_unverified_sdist_deps is True


class TestStrategyValidation:
    """Constructor rejects invalid strategy names."""

    def test_unknown_strategy_raises(self) -> None:
        """Anything other than highest/lowest/lowest-direct is a user error."""
        coordinator = _make_coordinator([])
        with pytest.raises(ValueError, match="resolution_strategy"):
            Provider(
                coordinator,
                _LINUX_TARGET,
                resolution_strategy="middle",
            )


class TestPreferences:
    """The preferences dict is canonicalized and used in ``choose_version``."""

    def test_preferences_keys_are_canonicalized(self) -> None:
        """Lookups by canonical name should hit a preference."""
        coordinator = _make_coordinator([])
        provider = Provider(
            coordinator,
            _LINUX_TARGET,
            preferences={"My-Cool_Pkg": Version("1.0")},
        )
        assert "my-cool-pkg" in provider._preferences

    def test_preference_used_when_in_range(self) -> None:
        """The preferred version wins over highest if the range allows it."""
        wheels = [_make_wheel(v) for v in ("1.0", "2.0", "3.0")]
        coordinator = _make_coordinator(wheels)
        provider = Provider(
            coordinator,
            _LINUX_TARGET,
            preferences={"pkg": Version("2.0")},
        )
        chosen = provider.choose_version("pkg", VersionRange.full())
        assert chosen == Version("2.0")

    def test_preference_skipped_when_outside_range(self) -> None:
        """An out-of-range preference falls back to the strategy default."""
        wheels = [_make_wheel(v) for v in ("1.0", "2.0", "3.0")]
        coordinator = _make_coordinator(wheels)
        provider = Provider(
            coordinator,
            _LINUX_TARGET,
            preferences={"pkg": Version("5.0")},
        )
        result = provider.choose_version("pkg", VersionRange.full())
        assert result == Version("3.0")


class TestExtrasProxyPreference:
    """A cross-tuple preference for an extras proxy respects Provides-Extra."""

    _NO_EXTRA = "Metadata-Version: 2.1\nName: foo\nVersion: {ver}\n\n"
    _WITH_EXTRA = (
        "Metadata-Version: 2.1\nName: foo\nVersion: {ver}\n"
        'Provides-Extra: bar\nRequires-Dist: cryptography; extra == "bar"\n\n'
    )

    def _provider(
        self,
        *,
        versions: Sequence[str],
        metadata_by_version: dict[str, str | None],
        preferred: str,
    ) -> Provider:
        wheels = [_make_wheel(v, package="foo") for v in versions]
        coordinator = make_coordinator(
            wheels,
            package="foo",
            metadata_by_version=metadata_by_version,
        )
        return Provider(
            coordinator,
            _LINUX_TARGET,
            root_extras={("foo", "bar")},
            preferences={"foo": Version(preferred)},
        )

    def test_non_providing_preference_falls_through(self) -> None:
        """A preferred base version lacking the extra yields the providing one."""
        provider = self._provider(
            versions=("1.5", "2.0"),
            metadata_by_version={
                "1.5": self._NO_EXTRA.format(ver="1.5"),
                "2.0": self._WITH_EXTRA.format(ver="2.0"),
            },
            preferred="1.5",
        )
        chosen = provider.choose_version("foo[bar]", VersionRange.full())
        assert chosen == Version("2.0")
        deps = provider.get_dependencies("foo[bar]", Version("2.0"))
        assert "cryptography" in deps

    def test_unreadable_preference_falls_through(self) -> None:
        """A preferred version whose metadata cannot be read is not honored."""
        provider = self._provider(
            versions=("1.5", "2.0"),
            metadata_by_version={
                "1.5": None,
                "2.0": self._WITH_EXTRA.format(ver="2.0"),
            },
            preferred="1.5",
        )
        chosen = provider.choose_version("foo[bar]", VersionRange.full())
        assert chosen == Version("2.0")

    def test_providing_preference_wins_over_highest(self) -> None:
        """A preferred version that declares the extra beats the highest one."""
        provider = self._provider(
            versions=("2.0", "3.0"),
            metadata_by_version={
                "2.0": self._WITH_EXTRA.format(ver="2.0"),
                "3.0": self._WITH_EXTRA.format(ver="3.0"),
            },
            preferred="2.0",
        )
        chosen = provider.choose_version("foo[bar]", VersionRange.full())
        assert chosen == Version("2.0")


class TestExtrasProxyPreferenceAdmission:
    """A preferred pre-release survives the extras-proxy preference gate."""

    _WITH_EXTRA = (
        "Metadata-Version: 2.1\nName: foo\nVersion: {ver}\n"
        'Provides-Extra: bar\nRequires-Dist: cryptography; extra == "bar"\n\n'
    )

    def _provider(self, *, versions: Sequence[str], preferred: str) -> Provider:
        wheels = [_make_wheel(v, package="foo") for v in versions]
        metadata = {v: self._WITH_EXTRA.format(ver=v) for v in versions}
        coordinator = make_coordinator(
            wheels, package="foo", metadata_by_version=metadata
        )
        return Provider(
            coordinator,
            _LINUX_TARGET,
            root_extras={("foo", "bar")},
            preferences={"foo": Version(preferred)},
        )

    def test_prerelease_honored_for_proxy_with_base_admission(self) -> None:
        """A base admission range lets the preferred pre-release win for the proxy."""
        provider = self._provider(
            versions=("2.0.0a2", "2.0.0a1", "1.0.0"), preferred="2.0.0a1"
        )
        admit = Requirement("foo>=0.5a1").specifier.to_range()
        provider.solution_ranges = {"foo": admit}
        chosen = provider.choose_version("foo[bar]", VersionRange.full())
        assert chosen == Version("2.0.0a1")

    def test_base_node_honors_same_prerelease(self) -> None:
        """The base node honors the same preference identically to the proxy."""
        provider = self._provider(
            versions=("2.0.0a2", "2.0.0a1", "1.0.0"), preferred="2.0.0a1"
        )
        admit = Requirement("foo>=0.5a1").specifier.to_range()
        provider.solution_ranges = {"foo": admit}
        assert provider.choose_version("foo", admit) == Version("2.0.0a1")

    def test_prerelease_skipped_without_base_admission(self) -> None:
        """Without a base admission entry the preferred pre-release is skipped."""
        provider = self._provider(versions=("2.0.0a1", "1.0.0"), preferred="2.0.0a1")
        chosen = provider.choose_version("foo[bar]", VersionRange.full())
        assert chosen == Version("1.0.0")


class TestCrossTupleProxyAlignment:
    """Two tuples with divergent pools pin the same extras-proxy pre-release."""

    _A_META = (
        "Metadata-Version: 2.1\nName: a\nVersion: 1.0\nRequires-Dist: c[bar]>=0.5a1\n\n"
    )
    _C_META = "Metadata-Version: 2.1\nName: c\nVersion: {v}\nProvides-Extra: bar\n\n"

    def _resolve(
        self, c_versions: Sequence[str], preferences: dict[str, Version] | None
    ) -> dict[str, Version]:
        listings = {
            "a": [_make_wheel("1.0", package="a")],
            "c": [_make_wheel(v, package="c") for v in c_versions],
        }
        metadata = {"1.0": self._A_META}
        metadata.update({v: self._C_META.format(v=v) for v in c_versions})
        coordinator = make_coordinator(listings=listings, metadata_by_version=metadata)
        root_reqs = {"a": VersionRange.full(admit_arbitrary=False)}
        provider = Provider(
            coordinator,
            _LINUX_TARGET,
            root_requirements=root_reqs,
            preferences=preferences,
        )
        resolver = Resolver(provider, range_type=VersionRange, root_version="0")
        return resolver.resolve(root_reqs)

    def test_tuple_two_aligns_to_tuple_one_prerelease(self) -> None:
        """The second tuple honors the first tuple's pre-release pin."""
        pins1 = self._resolve(["2.0.0a1", "1.0.0"], None)
        assert pins1["c"] == Version("2.0.0a1")
        pins2 = self._resolve(["2.0.0a2", "2.0.0a1", "1.0.0"], dict(pins1))
        assert pins2["c"] == Version("2.0.0a1")


class TestStrategyChoiceVersion:
    """``choose_version`` honors the resolution_strategy flag."""

    def test_lowest_picks_minimum(self) -> None:
        """``lowest`` returns the smallest candidate."""
        wheels = [_make_wheel(v) for v in ("1.0", "2.0", "3.0")]
        coordinator = _make_coordinator(wheels)
        provider = Provider(
            coordinator,
            _LINUX_TARGET,
            resolution_strategy="lowest",
        )
        chosen = provider.choose_version("pkg", VersionRange.full())
        assert chosen == Version("1.0")

    def test_lowest_returns_none_for_empty_range(self) -> None:
        """``lowest`` returns None when the range admits nothing."""
        coordinator = _make_coordinator([])
        provider = Provider(
            coordinator,
            _LINUX_TARGET,
            resolution_strategy="lowest",
        )
        assert provider.choose_version("pkg", VersionRange.full()) is None

    def test_lowest_direct_picks_min_for_direct(self) -> None:
        """``lowest-direct`` returns minimum when the package is direct."""
        wheels = [_make_wheel(v) for v in ("1.0", "2.0", "3.0")]
        coordinator = _make_coordinator(wheels)
        provider = Provider(
            coordinator,
            _LINUX_TARGET,
            resolution_strategy="lowest-direct",
            direct_packages=frozenset({"pkg"}),
        )
        chosen = provider.choose_version("pkg", VersionRange.full())
        assert chosen == Version("1.0")

    def test_lowest_direct_picks_max_for_transitive(self) -> None:
        """``lowest-direct`` returns highest for transitive packages."""
        wheels = [_make_wheel(v) for v in ("1.0", "2.0", "3.0")]
        coordinator = _make_coordinator(wheels)
        provider = Provider(
            coordinator,
            _LINUX_TARGET,
            resolution_strategy="lowest-direct",
            direct_packages=frozenset(),
        )
        chosen = provider.choose_version("pkg", VersionRange.full())
        assert chosen == Version("3.0")


def _platform_wheel(version: str, tag: str, *, package: str = "pkg") -> WheelFile:
    """Build a wheel with an explicit ``cpXY-cpXY-<platform>`` tag."""
    filename = f"{package}-{version}-{tag}.whl"
    return WheelFile(
        filename=filename,
        url=f"https://example.com/{filename}",
        version=version,
        requires_python=None,
        has_metadata=True,
        upload_time=None,
    )


def _sdist(version: str, *, package: str = "pkg") -> SdistFile:
    """Build a tar.gz sdist for a specific version."""
    filename = f"{package}-{version}.tar.gz"
    return SdistFile(
        filename=filename,
        url=f"https://example.com/{filename}",
        version=version,
        requires_python=None,
        upload_time=None,
    )


def _index_with_files(
    files: Sequence[WheelFile | SdistFile], *, package: str = "pkg"
) -> MagicMock:
    """Coordinator stub whose listing is exactly ``files``."""
    index = InMemoryIndex()
    index.store_listing(package, files)
    coordinator = MagicMock()
    coordinator.index = index
    coordinator.request_listing.side_effect = lambda _pkg: _done_event()
    return coordinator


class TestWheelTagFiltering:
    """A version whose wheels the target cannot install is not a candidate."""

    def test_no_target_keeps_every_wheel(self) -> None:
        """With no target nothing has said which machine to filter for."""
        wheels = [_platform_wheel("1.0", "cp311-cp311-win_amd64")]
        provider = Provider(_index_with_files(wheels))
        result = provider.filter_distributions("pkg", wheels)
        assert [v for v, _ in result] == [Version("1.0")]
        assert provider.stats.excluded_by_wheel_tags == 0

    def test_marker_overlay_keeps_every_wheel(self) -> None:
        """An overlay moves the markers off the tag axis, so the tags are unusable.

        The tag set still describes the machine the target was built from,
        not the one the markers now name, so filtering by it would drop
        wheels the impersonated target installs.
        """
        wheels = [_platform_wheel("1.0", "cp311-cp311-win_amd64")]
        provider = Provider(
            _index_with_files(wheels),
            _LINUX_TARGET.with_marker_overrides({"sys_platform": "win32"}),
        )
        result = provider.filter_distributions("pkg", wheels)
        assert [v for v, _ in result] == [Version("1.0")]
        assert provider.stats.excluded_by_wheel_tags == 0

    def test_overlay_of_the_targets_own_value_keeps_the_filter(self) -> None:
        """An overlay that moves nothing leaves the tag set describing the target."""
        wheels = [_platform_wheel("1.0", "cp311-cp311-win_amd64")]
        provider = Provider(
            _index_with_files(wheels),
            _LINUX_TARGET.with_marker_overrides({"sys_platform": "linux"}),
        )
        assert provider.filter_distributions("pkg", wheels) == []
        assert provider.stats.excluded_by_wheel_tags == 1

    def test_incompatible_wheel_dropped_when_alternative_exists(self) -> None:
        """A win wheel is dropped on a linux tuple that has a linux wheel too."""
        wheels = [
            _platform_wheel("1.0", "cp311-cp311-manylinux_2_17_x86_64"),
            _platform_wheel("1.0", "cp311-cp311-win_amd64"),
        ]
        provider = Provider(
            _index_with_files(wheels),
            _linux_target(PlatformSpec("linux_x86_64")),
        )
        result = provider.filter_distributions("pkg", wheels)
        kept_filenames = {dist.filename for _, dist in result}
        assert kept_filenames == {"pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl"}
        assert provider.stats.excluded_by_wheel_tags == 1

    def test_version_dropped_when_no_compatible_wheel_under_never(self) -> None:
        """All-incompatible wheels with NEVER build_policy makes version unavailable."""
        wheels = [
            _platform_wheel("2.0", "cp311-cp311-win_amd64"),
            _platform_wheel("1.0", "cp311-cp311-manylinux_2_17_x86_64"),
        ]
        provider = Provider(
            _index_with_files(wheels),
            _linux_target(PlatformSpec("linux_x86_64")),
            build_policy=BuildPolicy.NEVER,
        )
        result = provider.filter_distributions("pkg", wheels)
        kept_versions = {v for v, _ in result}
        assert kept_versions == {Version("1.0")}
        assert provider.stats.excluded_versions_no_compatible_wheel == 1

    def test_version_kept_via_sdist_under_allow(self) -> None:
        """Under ALLOW, a version with only an sdist survives the filter."""
        files: list[WheelFile | SdistFile] = [
            _platform_wheel("2.0", "cp311-cp311-win_amd64"),
            _sdist("2.0"),
        ]
        provider = Provider(
            _index_with_files(files),
            _linux_target(PlatformSpec("linux_x86_64")),
            build_policy=BuildPolicy.BUILD_REMOTE,
        )
        result = provider.filter_distributions("pkg", files)
        kept_kinds = {(v, isinstance(d, WheelFile)) for v, d in result}
        # Version 2.0 survives via the sdist (False = not a wheel).
        assert (Version("2.0"), False) in kept_kinds
        # The win wheel is dropped.
        assert (Version("2.0"), True) not in kept_kinds

    def test_pure_python_wheel_compatible_with_every_platform(self) -> None:
        """``py3-none-any`` wheels match every tuple."""
        wheels = [_platform_wheel("1.0", "py3-none-any")]
        provider = Provider(
            _index_with_files(wheels),
            _linux_target(PlatformSpec("linux_x86_64")),
        )
        result = provider.filter_distributions("pkg", wheels)
        assert [v for v, _ in result] == [Version("1.0")]
        assert provider.stats.excluded_by_wheel_tags == 0

    def test_higher_glibc_wheel_dropped_below_floor(self) -> None:
        """A manylinux 2.34 wheel is incompatible with a 2.17 floor."""
        wheels = [
            _platform_wheel("1.0", "cp311-cp311-manylinux_2_34_x86_64"),
        ]
        provider = Provider(
            _index_with_files(wheels),
            _linux_target(PlatformSpec("linux_x86_64", libc_version=(2, 17))),
            build_policy=BuildPolicy.NEVER,
        )
        result = provider.filter_distributions("pkg", wheels)
        assert result == []
        assert provider.stats.excluded_by_wheel_tags == 1

    def test_higher_glibc_wheel_admitted_when_floor_raised(self) -> None:
        """The same wheel passes if the user declares a 2.34 floor."""
        wheels = [
            _platform_wheel("1.0", "cp311-cp311-manylinux_2_34_x86_64"),
        ]
        provider = Provider(
            _index_with_files(wheels),
            _linux_target(PlatformSpec("linux_x86_64", libc_version=(2, 34))),
        )
        result = provider.filter_distributions("pkg", wheels)
        assert [v for v, _ in result] == [Version("1.0")]
        assert provider.stats.excluded_by_wheel_tags == 0

    def test_sdist_only_under_no_dist_policy_drops_version(self) -> None:
        """``WHEEL_ONLY`` plus no compatible wheel removes the version."""
        files: list[WheelFile | SdistFile] = [
            _platform_wheel("2.0", "cp311-cp311-win_amd64"),
            _sdist("2.0"),
        ]
        provider = Provider(
            _index_with_files(files),
            _linux_target(PlatformSpec("linux_x86_64")),
            dist_policy=DistPolicy.WHEEL_ONLY,
            build_policy=BuildPolicy.BUILD_REMOTE,
        )
        result = provider.filter_distributions("pkg", files)
        # Parent already drops sdist under WHEEL_ONLY; the only file left
        # is the incompatible win wheel, so the version disappears.
        assert result == []

    def test_incompatible_wheel_with_sdist_under_never_kept_via_sdist(self) -> None:
        """Incompatible wheel + sdist + NEVER -> version stays alive.

        An sdist keeps the version alive at every build policy level: a
        PEP 643 static sdist is read without a backend, so look-ahead,
        not this filter, rejects the dynamic-no-fallback case.
        """
        files: list[WheelFile | SdistFile] = [
            _platform_wheel("2.0", "cp311-cp311-win_amd64"),
            _sdist("2.0"),
        ]
        provider = Provider(
            _index_with_files(files),
            _linux_target(PlatformSpec("linux_x86_64")),
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            build_policy=BuildPolicy.NEVER,
        )
        result = provider.filter_distributions("pkg", files)
        kept_kinds = {(v, isinstance(d, WheelFile)) for v, d in result}
        assert (Version("2.0"), False) in kept_kinds
        assert (Version("2.0"), True) not in kept_kinds
        assert provider.stats.excluded_versions_no_compatible_wheel == 0

    def test_fetch_versions_applies_wheel_tag_filter(self) -> None:
        """The resolver path runs the filter, not just a direct call.

        ``versions_cache`` is the single funnel: candidate selection,
        metadata sourcing, every prefetch path, look-ahead, and the
        emitted wheel list all read what ``fetch_versions`` stored.  The
        unit tests above call ``filter_distributions`` directly, so this
        one pins the path production takes.
        """
        files: list[WheelFile | SdistFile] = [
            _platform_wheel("2.0", "cp311-cp311-win_amd64"),
        ]
        provider = Provider(
            _index_with_files(files),
            _linux_target(PlatformSpec("linux_x86_64")),
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
        )
        result = provider.fetch_versions("pkg")
        assert result == []
        assert provider.stats.excluded_versions_no_compatible_wheel == 1


class TestRequiresPythonPatch:
    """Requires-Python is evaluated against the tuple's full patch version."""

    def test_dist_kept_when_patch_satisfies_requires_python(self) -> None:
        """python_full_version 3.13.4 keeps a dist that requires >=3.13.1."""
        provider = Provider(
            _make_coordinator([_make_wheel("1.0", requires_python=">=3.13.1")]),
            ResolveTarget.for_declared(
                python_version="3.13",
                spec=PlatformSpec("linux_x86_64"),
                python_full_version="3.13.4",
            ),
        )
        result = provider.fetch_versions("pkg")
        assert [v for v, _ in result] == [Version("1.0")]

    def test_dist_excluded_when_patch_below_requires_python(self) -> None:
        """A tuple targeting 3.13.0 excludes a >=3.13.1 dist."""
        provider = Provider(
            _make_coordinator([_make_wheel("1.0", requires_python=">=3.13.1")]),
            ResolveTarget.for_declared(
                python_version="3.13", spec=PlatformSpec("linux_x86_64")
            ),
        )
        assert provider.fetch_versions("pkg") == []


_FORTY_SHA = "0123456789abcdef0123456789abcdef01234567"


class TestVcsConfigPlumbing:
    """``vcs_config`` and ``vcs_cache_dir`` reach the underlying provider."""

    def test_block_policy_with_vcs_source_rejected_at_construction(self) -> None:
        """Declaring a VCS source under BLOCK policy raises a clear error.

        The check lives in ``_provider.sources.index_vcs_sources``; reaching
        it proves ``vcs_config`` was threaded through to the
        :class:`Provider` base instead of silently defaulting.
        """
        coordinator = _make_coordinator([])
        source = VcsSource(
            name="pkg",
            url=f"git+https://example.com/pkg.git@{_FORTY_SHA}",
        )
        with pytest.raises(ValueError, match="vcs_sources require VcsPolicy.ALLOW"):
            Provider(
                coordinator,
                _LINUX_TARGET,
                vcs_config=VcsConfig(policy=VcsPolicy.BLOCK),
                vcs_sources=[source],
                build_policy=BuildPolicy.NEVER,
            )

    def test_allow_policy_admits_vcs_source(self) -> None:
        """An ALLOW config with matching schemes accepts a declared source.

        If ``vcs_config`` were dropped on the universal layer, the
        default :class:`VcsConfig` (BLOCK) would raise here.  Reaching
        registration proves the kwarg is honored.
        """
        coordinator = _make_coordinator([])
        source = VcsSource(
            name="pkg",
            url=f"git+https://example.com/pkg.git@{_FORTY_SHA}",
        )
        provider = Provider(
            coordinator,
            _LINUX_TARGET,
            vcs_config=VcsConfig(
                policy=VcsPolicy.ALLOW,
                allowed_schemes=frozenset({"git+https"}),
                allowed_repos=("https://example.com/",),
            ),
            vcs_sources=[source],
            build_policy=BuildPolicy.NEVER,
        )
        assert provider.vcs_source_for("pkg") is source
        assert provider.vcs_config.policy is VcsPolicy.ALLOW

    def test_vcs_cache_dir_set_on_provider(self, tmp_path: Path) -> None:
        """``vcs_cache_dir`` is stored on the provider for later use.

        ``materialize_vcs_source`` reads ``provider.vcs_cache_dir`` when
        the resolver actually clones; passing ``None`` (the bug) raises
        ``UnsupportedSdistError`` at clone time.  Verifying the
        attribute is set proves the kwarg flows through ``super()``.
        """
        coordinator = _make_coordinator([])
        cache = tmp_path / "vcs"
        provider = Provider(
            coordinator,
            _LINUX_TARGET,
            vcs_cache_dir=cache,
            build_policy=BuildPolicy.NEVER,
        )
        assert provider.vcs_cache_dir == cache

    def test_vcs_cache_dir_defaults_to_none(self) -> None:
        """When omitted, ``vcs_cache_dir`` remains ``None`` on the provider."""
        coordinator = _make_coordinator([])
        provider = Provider(
            coordinator,
            _LINUX_TARGET,
            build_policy=BuildPolicy.NEVER,
        )
        assert provider.vcs_cache_dir is None
