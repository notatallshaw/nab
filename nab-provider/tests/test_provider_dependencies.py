"""Dependency interpretation for provider targets and artifact metadata."""

from __future__ import annotations

import pytest
from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from nab_provider._provider.metadata_resolver import (
    TargetDepSignature,
    target_dep_signature,
)
from nab_provider.metadata import WheelMetadata
from nab_provider.provider import (
    BuildPolicy,
    Provider,
    UnsupportedVcsError,
    VcsConfig,
    VcsPolicy,
)
from nab_provider.target import ResolveTarget
from nab_provider.testing import (
    FakeFetchPort,
    make_coordinator,
    make_wheel,
    pkg_override,
)

from .provider_fixtures import PYTHON_312 as _PY312

V = Version


class TestMembershipSetMarkerInMetadata:
    """A Requires-Dist marker testing extras/dependency_groups drops only the dep.

    The set variables are empty at resolve time, so the provider excludes the
    dependency. The candidate version itself must survive,
    matching the root-requirement handling (test_root_extras_set_marker_warns).
    """

    def test_extras_membership_marker_keeps_version(self) -> None:
        meta_text = (
            "Metadata-Version: 2.3\nName: foo\nVersion: 1.0\n"
            "Provides-Extra: docs\n"
            "Requires-Dist: realdep>=1.0\n"
            'Requires-Dist: somedep; "docs" in extras\n'
        )
        coordinator = make_coordinator(
            [make_wheel("1.0")], metadata_text=meta_text, package="foo"
        )
        provider = Provider(coordinator)
        deps = provider.get_dependencies("foo", V("1.0"))
        assert "realdep" in deps
        assert "somedep" not in deps

    def test_dependency_groups_membership_marker_keeps_version(self) -> None:
        meta_text = (
            "Metadata-Version: 2.3\nName: foo\nVersion: 1.0\n"
            "Requires-Dist: realdep>=1.0\n"
            'Requires-Dist: somedep; "dev" in dependency_groups\n'
        )
        coordinator = make_coordinator(
            [make_wheel("1.0")], metadata_text=meta_text, package="foo"
        )
        provider = Provider(coordinator)
        deps = provider.get_dependencies("foo", V("1.0"))
        assert "realdep" in deps
        assert "somedep" not in deps


class TestTransitiveDirectUrlDep:
    """A direct-URL ``Requires-Dist`` is refused, not silently substituted.

    The root and constraint inputs and the universal per-tuple path already
    call ``admit_vcs_url`` then raise. Without the same check here a fetched
    package's ``bar @ https://...`` dep was recorded as a bare ``bar`` and
    resolved from the index, pinning the wrong artifact silently.
    """

    @staticmethod
    def _provider_for(url: str, vcs_config: VcsConfig | None = None) -> Provider:
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=(
                "Metadata-Version: 2.1\n"
                "Name: foo\n"
                "Version: 1.0\n"
                f"Requires-Dist: bar @ {url}\n"
            ),
            package="foo",
        )
        return Provider(coordinator, target=_PY312, vcs_config=vcs_config)

    def test_plain_url_dep_refused(self) -> None:
        """An applicable non-VCS URL dependency raises UnsupportedVcsError."""
        provider = self._provider_for("https://example.com/bar-9.9-py3-none-any.whl")
        with pytest.raises(UnsupportedVcsError, match="not a recognized VCS scheme"):
            provider.get_dependencies("foo", V("1.0"))

    def test_vcs_url_dep_blocked_by_default(self) -> None:
        """A VCS dep is refused under the default BLOCK policy."""
        provider = self._provider_for("git+https://example.com/bar.git@" + "a" * 40)
        with pytest.raises(UnsupportedVcsError, match='vcs.policy is "block"'):
            provider.get_dependencies("foo", V("1.0"))

    def test_admitted_vcs_url_dep_raises_not_implemented(self) -> None:
        """An admitted VCS dep raises NotImplementedError; support is deferred."""
        provider = self._provider_for(
            "git+https://example.com/bar.git@" + "a" * 40,
            vcs_config=VcsConfig(
                policy=VcsPolicy.ALLOW,
                allowed_schemes=frozenset({"git+https"}),
                allowed_repos=("https://example.com/",),
            ),
        )
        with pytest.raises(NotImplementedError, match="foo declares a Git URL for bar"):
            provider.get_dependencies("foo", V("1.0"))

    def test_marker_gated_url_dep_not_applying_is_skipped(self) -> None:
        """The provider skips a URL dependency when its marker is false."""
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=(
                "Metadata-Version: 2.1\n"
                "Name: foo\n"
                "Version: 1.0\n"
                "Requires-Dist: bar @ https://example.com/bar.whl ;"
                ' python_version < "3.0"\n'
                "Requires-Dist: baz>=1.0\n"
            ),
            package="foo",
        )
        provider = Provider(coordinator, target=_PY312)
        deps = provider.get_dependencies("foo", V("1.0"))
        assert "bar" not in deps
        assert "baz" in deps

    @staticmethod
    def _provider_with_extra_url(
        url: str,
        *,
        root_extras: set[tuple[str, str]] | None = None,
        vcs_config: VcsConfig | None = None,
    ) -> Provider:
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=(
                "Metadata-Version: 2.1\n"
                "Name: foo\n"
                "Version: 1.0\n"
                "Provides-Extra: uvloop\n"
                f'Requires-Dist: bar @ {url} ; extra == "uvloop"\n'
                "Requires-Dist: baz>=1.0\n"
            ),
            package="foo",
        )
        return Provider(
            coordinator,
            target=_PY312,
            root_extras=root_extras,
            vcs_config=vcs_config,
        )

    def test_unrequested_extra_url_dep_does_not_abort_base(self) -> None:
        """Base dependencies exclude a URL required only by an unrequested extra."""
        provider = self._provider_with_extra_url("https://example.com/bar.whl")
        deps = provider.get_dependencies("foo", V("1.0"))
        assert "bar" not in deps
        assert "baz" in deps

    def test_unrequested_extra_vcs_url_dep_does_not_abort_base(self) -> None:
        """A VCS url under an unrequested extra is also skipped at base."""
        provider = self._provider_with_extra_url(
            "git+https://example.com/bar.git@" + "a" * 40
        )
        deps = provider.get_dependencies("foo", V("1.0"))
        assert "bar" not in deps
        assert "baz" in deps

    def test_selected_extra_url_dep_is_refused(self) -> None:
        """Selecting the extra fires the deferred direct-URL refusal."""
        provider = self._provider_with_extra_url("https://example.com/bar.whl")
        provider.get_dependencies("foo", V("1.0"))
        with pytest.raises(UnsupportedVcsError, match="not a recognized VCS scheme"):
            provider.get_dependencies("foo[uvloop]", V("1.0"))

    def test_selected_extra_vcs_url_dep_is_refused(self) -> None:
        """Selecting the extra fires the deferred VCS refusal under BLOCK."""
        provider = self._provider_with_extra_url(
            "git+https://example.com/bar.git@" + "a" * 40
        )
        provider.get_dependencies("foo", V("1.0"))
        with pytest.raises(UnsupportedVcsError, match='vcs.policy is "block"'):
            provider.get_dependencies("foo[uvloop]", V("1.0"))

    def test_selected_extra_admitted_vcs_url_raises_not_implemented(self) -> None:
        """An admitted VCS url under a selected extra raises NotImplementedError."""
        provider = self._provider_with_extra_url(
            "git+https://example.com/bar.git@" + "a" * 40,
            vcs_config=VcsConfig(
                policy=VcsPolicy.ALLOW,
                allowed_schemes=frozenset({"git+https"}),
                allowed_repos=("https://example.com/",),
            ),
        )
        provider.get_dependencies("foo", V("1.0"))
        with pytest.raises(
            NotImplementedError, match=r"foo\[uvloop\] declares a Git URL for bar"
        ):
            provider.get_dependencies("foo[uvloop]", V("1.0"))

    def test_root_requested_extra_url_dep_refused_eagerly(self) -> None:
        """When the extra is a root extra, the url dep is refused at base parse."""
        provider = self._provider_with_extra_url(
            "https://example.com/bar.whl",
            root_extras={("foo", "uvloop")},
        )
        with pytest.raises(UnsupportedVcsError, match="not a recognized VCS scheme"):
            provider.get_dependencies("foo", V("1.0"))


class TestTargetDepSignature:
    """``target_dep_signature`` projects metadata to a comparable signature."""

    def _provider(self) -> Provider:
        coordinator = make_coordinator([make_wheel("1.0")], package="pkg")
        return Provider(coordinator, target=_PY312)

    @staticmethod
    def _md(
        requires_dist: list[str],
        *,
        provides_extra: tuple[str, ...] = (),
        requires_python: str | None = None,
    ) -> WheelMetadata:
        return WheelMetadata(
            name="pkg",
            version=V("1.0"),
            requires_python=(
                SpecifierSet(requires_python) if requires_python is not None else None
            ),
            requires_dist=[Requirement(r) for r in requires_dist],
            provides_extra=list(provides_extra),
        )

    def _sig(self, provider: Provider, metadata: WheelMetadata) -> TargetDepSignature:
        """Project ``metadata`` under the release the wheel was served as."""
        return target_dep_signature(provider, ("pkg", V("1.0")), metadata)

    def test_permuted_lines_equal(self) -> None:
        """Line order does not change the signature."""
        provider = self._provider()
        a = self._md(["foo>=1", "bar<2"])
        b = self._md(["bar<2", "foo>=1"])
        assert self._sig(provider, a) == self._sig(provider, b)

    def test_specifier_whitespace_preserves_signature(self) -> None:
        """Whitespace variants of one specifier normalize equal."""
        provider = self._provider()
        a = self._md(["foo>=1,<2"])
        b = self._md(["foo >= 1, < 2"])
        assert self._sig(provider, a) == self._sig(provider, b)

    def test_target_equal_marker_equal(self) -> None:
        """Markers that evaluate the same for the target project equal."""
        provider = self._provider()
        a = self._md(['foo; python_version >= "3.0"'])
        b = self._md(['foo; python_version >= "3.5"'])
        assert self._sig(provider, a) == self._sig(provider, b)

    def test_genuine_dep_difference_unequal(self) -> None:
        """A real base-dep range difference is unequal."""
        provider = self._provider()
        a = self._md(["foo>=1"])
        b = self._md(["foo>=2"])
        assert self._sig(provider, a) != self._sig(provider, b)

    def test_extra_only_difference_unequal(self) -> None:
        """A difference confined to an extra-conditioned dep is unequal."""
        provider = self._provider()
        a = self._md(['foo; extra == "e"'], provides_extra=("e",))
        b = self._md(['foo>=2; extra == "e"'], provides_extra=("e",))
        assert self._sig(provider, a) != self._sig(provider, b)

    def test_url_dep_only_difference_unequal(self) -> None:
        """A difference confined to a URL dep is unequal."""
        provider = self._provider()
        a = self._md(["foo @ https://example.com/a.whl"])
        b = self._md(["foo @ https://example.com/b.whl"])
        assert self._sig(provider, a) != self._sig(provider, b)

    def test_requires_python_difference_folds(self) -> None:
        """A Requires-Python difference alone does not change the signature.

        Requires-Python determines admission. Two wheels with the same deps but
        different Python floors therefore project to equal lock edges.
        """
        provider = self._provider()
        a = self._md(["foo"], requires_python=">=3.8")
        b = self._md(["foo"], requires_python=">=3.9")
        assert self._sig(provider, a) == self._sig(provider, b)

    def test_marker_dropped_dep_absent(self) -> None:
        """A dep whose marker fails for the target is not projected."""
        provider = self._provider()
        base_deps = self._sig(provider, self._md(['foo; python_version < "3.0"']))[0]
        assert base_deps == {}

    def test_duplicate_name_intersected(self) -> None:
        """A name on two base lines folds to the range intersection."""
        provider = self._provider()
        base_deps = self._sig(provider, self._md(["foo>=1", "foo<5"]))[0]
        assert V("3") in base_deps["foo"]
        assert V("6") not in base_deps["foo"]

    def test_extra_gated_url_dep_bucketed_by_extra(self) -> None:
        """The signature lists the URL dependency under the extra that requires it."""
        provider = self._provider()
        url_buckets = self._sig(
            provider,
            self._md(
                ['foo @ https://example.com/a.whl ; extra == "e"'],
                provides_extra=("e",),
            ),
        )[2]
        assert "e" in url_buckets

    def test_base_url_dep_bucketed(self) -> None:
        """A base URL dep buckets under the base key, not an extra."""
        provider = self._provider()
        url_buckets = self._sig(
            provider, self._md(["foo @ https://example.com/a.whl"])
        )[2]
        assert list(url_buckets) == [None]

    def test_per_extra_dep_projected(self) -> None:
        """The signature lists the package dependency under the extra that requires it."""
        provider = self._provider()
        extra_map = self._sig(
            provider, self._md(['foo>=2; extra == "e"'], provides_extra=("e",))
        )[1]
        assert V("3") in extra_map["e"]["foo"]
        assert V("1") not in extra_map["e"]["foo"]

    def test_extra_marker_matches_only_named_extra(self) -> None:
        """Only extras that match the marker include the dependency."""
        provider = self._provider()
        extra_map = self._sig(
            provider, self._md(['foo; extra == "e"'], provides_extra=("e", "f"))
        )[1]
        assert "foo" in extra_map["e"]
        assert extra_map["f"] == {}

    def test_provides_extra_override_applied(self) -> None:
        """A provides-extra override reshapes the projection before comparison.

        Two wheels declaring different raw extras project apart on their raw
        extras, but a provides-extra override normalizes both to the same
        declared set, so the overridden view they resolve from agrees.
        """
        coordinator = make_coordinator([make_wheel("1.0")], package="pkg")
        provider = Provider(
            coordinator,
            target=_PY312,
            package_overrides=(pkg_override("pkg", provides_extra=("e",)),),
        )
        a = self._md(['foo; extra == "e"'], provides_extra=("e",))
        b = self._md(['foo; extra == "e"'], provides_extra=("e", "f"))
        assert self._sig(provider, a) == self._sig(provider, b)


class TestMarkerEnvironment:
    def test_overlay_overrides_host(self) -> None:
        """Keys in the target's marker overrides overlay the host environment."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(
            coordinator,
            target=ResolveTarget.for_host().with_marker_overrides(
                {
                    "platform_system": "Windows",
                    "sys_platform": "win32",
                    "platform_machine": "AMD64",
                }
            ),
            build_policy=BuildPolicy.NEVER,
        )
        assert provider.environment["platform_system"] == "Windows"
        assert provider.environment["sys_platform"] == "win32"
        assert provider.environment["platform_machine"] == "AMD64"

    def test_overlay_does_not_replace_unspecified_keys(self) -> None:
        """Keys not in the overlay keep the host value (``os_name`` etc)."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(
            coordinator,
            target=ResolveTarget.for_host().with_marker_overrides(
                {"platform_system": "Darwin"}
            ),
            build_policy=BuildPolicy.NEVER,
        )
        assert "os_name" in provider.environment
        assert provider.environment["platform_system"] == "Darwin"

    def test_marker_evaluates_against_overlay(self) -> None:
        """A ``sys_platform == 'win32'`` dep activates under impersonation."""
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=(
                "Metadata-Version: 2.1\n"
                "Name: foo\n"
                "Version: 1.0\n"
                'Requires-Dist: pywin32 ; sys_platform == "win32"\n'
                'Requires-Dist: only-linux ; sys_platform == "linux"\n'
            ),
            package="foo",
        )
        provider = Provider(
            coordinator,
            target=ResolveTarget.for_host_python("3.12").with_marker_overrides(
                {"sys_platform": "win32"}
            ),
            build_policy=BuildPolicy.NEVER,
        )
        deps = provider.get_dependencies("foo", V("1.0"))
        assert "pywin32" in deps
        assert "only-linux" not in deps

    def test_python_version_overlay_runs_after_python_version_arg(self) -> None:
        """The marker overrides apply after the target Python."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(
            coordinator,
            target=ResolveTarget.for_host_python("3.11.5").with_marker_overrides(
                {"python_version": "3.13"}
            ),
            build_policy=BuildPolicy.NEVER,
        )
        assert provider.environment["python_version"] == "3.13"

    def test_two_part_python_version_expands_full_version(self) -> None:
        """A 2-part python_version yields a 3-part python_full_version."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(
            coordinator,
            target=ResolveTarget.for_host_python("3.10"),
            build_policy=BuildPolicy.NEVER,
        )
        assert provider.environment["python_version"] == "3.10"
        assert provider.environment["python_full_version"] == "3.10.0"

    def test_full_python_version_preserved(self) -> None:
        """A 3-part python_version keeps its patch component."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(
            coordinator,
            target=ResolveTarget.for_host_python("3.11.5"),
            build_policy=BuildPolicy.NEVER,
        )
        assert provider.environment["python_full_version"] == "3.11.5"


class TestTargetEnvironment:
    def test_target_python_moves_implementation_version(self) -> None:
        """A target Python moves implementation_version too."""
        provider = Provider(
            make_coordinator(), target=ResolveTarget.for_host_python("3.9.0")
        )
        assert provider.environment["implementation_version"] == "3.9.0"
        assert (
            provider.environment["implementation_version"]
            == provider.environment["python_full_version"]
        )

    def test_marker_env_patch_python_version_normalizes(self) -> None:
        """A marker-override python_version like 3.10.5 normalizes to major.minor."""
        provider = Provider(
            make_coordinator(),
            target=ResolveTarget.for_host_python("3.12.3").with_marker_overrides(
                {"python_version": "3.10.5"}
            ),
            build_policy=BuildPolicy.NEVER,
        )
        assert provider.environment["python_version"] == "3.10"
        assert provider.environment["python_full_version"] == "3.10.5"


class TestConsultedMarkers:
    """The lock's ``environments`` declaration is built from the dependency
    markers the resolve read, so the provider has to record every marker it
    evaluates.
    """

    @staticmethod
    def _coordinator(requires_dist: str) -> FakeFetchPort:
        return make_coordinator(
            [make_wheel("1.0")],
            metadata_text=(
                "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n"
                f"Requires-Dist: {requires_dist}\n"
            ),
            package="foo",
        )

    def test_an_evaluated_dependency_marker_is_recorded(self) -> None:
        provider = Provider(
            self._coordinator('colorama ; platform_system == "Windows"'),
            target=_PY312,
        )
        provider.get_dependencies("foo", V("1.0"))
        assert provider.consulted_markers == {Marker('platform_system == "Windows"')}

    def test_a_marker_that_holds_is_recorded_too(self) -> None:
        """The provider records the marker that admitted the dependency."""
        # _PY312 pins python_version to 3.12, so this marker holds on every
        # host the suite runs on, unlike a platform marker like sys_platform.
        provider = Provider(
            self._coordinator('bar ; python_version == "3.12"'), target=_PY312
        )
        deps = provider.get_dependencies("foo", V("1.0"))
        assert "bar" in deps
        assert provider.consulted_markers == {Marker('python_version == "3.12"')}

    def test_an_unmarked_dependency_records_nothing(self) -> None:
        provider = Provider(self._coordinator("bar"), target=_PY312)
        provider.get_dependencies("foo", V("1.0"))
        assert provider.consulted_markers == set()
