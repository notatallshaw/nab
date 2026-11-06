"""Prior locks supply contextual version hints, never trusted current artifacts."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from nab_project.inputs import ResolveInputs
from nab_project.lock_preferences import LockPreferences, read_lock_preferences
from nab_project.resolve import resolve_with_coordinator
from nab_provider._vendor.packaging.markers import Marker
from nab_provider._vendor.packaging.pylock import (
    Package,
    PackageArchive,
    PackageWheel,
    Pylock,
)
from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider._vendor.packaging.utils import canonicalize_name
from nab_provider._vendor.packaging.version import Version
from nab_provider.environment import marker_environment
from nab_provider.policy import BuildPolicy, DecisionOrder
from nab_provider.provider import Provider
from nab_provider.records import IndexConfig, SdistFile, WheelFile
from nab_provider.tags import PlatformSpec, TagSet
from nab_provider.target import ResolveTarget
from nab_provider.testing import (
    FakeFetchPort,
    make_coordinator,
    make_metadata,
    make_wheel,
)
from nab_provider.yanking import append_yanked_file


def wheel_package(
    name: str = "a",
    version: str = "1",
    *,
    marker: str | None = None,
    tag: str = "py3-none-any",
) -> Package:
    """Build a registry pin with one fully declared old wheel."""
    return Package(
        name=canonicalize_name(name),
        version=Version(version),
        marker=Marker(marker) if marker else None,
        wheels=(
            PackageWheel(
                name=f"{name}-{version}-{tag}.whl",
                url=f"https://old.example/{name}-{version}.whl",
                hashes={"sha256": "a" * 64},
            ),
        ),
    )


def prior(*packages: Package) -> Pylock:
    """Validate an old lock whose selector names can be combined in one context."""
    lock = Pylock(
        lock_version=Version("1.0"),
        created_by="nab",
        packages=packages,
        extras=(canonicalize_name("speed"),),
        dependency_groups=("lint",),
    )
    lock.validate()
    return lock


def target(platform: str = "linux_x86_64", python: str = "3.12") -> ResolveTarget:
    """Create a declared consumer without consulting the host machine."""
    return ResolveTarget.for_declared(
        python_version=python, spec=PlatformSpec(platform)
    )


def test_conditional_versions_are_selected_per_actual_target() -> None:
    preferences = LockPreferences(
        prior(
            wheel_package(version="1", marker='sys_platform == "linux"'),
            wheel_package(version="2", marker='sys_platform == "win32"'),
        )
    )

    assert preferences.versions_for(target()) == {"a": Version("1")}
    assert preferences.versions_for(target("windows_amd64")) == {"a": Version("2")}


def test_selector_conjunction_uses_one_complete_context() -> None:
    preferences = LockPreferences(
        prior(
            wheel_package(),
            wheel_package(
                "b", marker='"speed" in extras and "lint" in dependency_groups'
            ),
        )
    )

    assert preferences.versions_for(target(), selection=(("extra", "speed"),)) == {
        "a": Version("1")
    }
    assert preferences.versions_for(
        target(),
        selection=(("extra", "speed"), ("group", "lint")),
    ) == {"a": Version("1"), "b": Version("1")}


def test_new_members_preserve_the_old_contexts_known_preferences() -> None:
    preferences = LockPreferences(prior(wheel_package()))

    assert preferences.versions_for(
        target(),
        selection=(("extra", "new"), ("group", "new")),
    ) == {"a": Version("1")}


def test_ambiguity_is_not_hidden_by_one_incompatible_artifact() -> None:
    preferences = LockPreferences(
        prior(
            wheel_package(version="1"),
            wheel_package(version="2", tag="cp312-cp312-win_amd64"),
        )
    )

    assert preferences.versions_for(target()) == {}


def test_old_artifact_priority_cannot_change_the_package_version() -> None:
    lock = prior(
        wheel_package(), wheel_package("b", tag="cp312-cp312-manylinux_2_17_x86_64")
    )
    consumer = target()
    assert consumer.tags is not None
    expected = {
        str(package.name): package.version
        for package, _artifact in lock.select(
            environment=marker_environment(consumer.marker_env),
            tags=consumer.tags.ordered,
        )
    }

    assert LockPreferences(lock).versions_for(consumer) == expected


def test_old_pypy_wheel_supplies_the_open_abi_family() -> None:
    consumer = ResolveTarget.for_declared(
        python_version="3.11",
        spec=PlatformSpec("linux_x86_64"),
        implementation="pypy",
    )
    preferences = LockPreferences(
        prior(wheel_package(tag="pp311-pypy311_pp73-manylinux_2_17_x86_64"))
    )

    assert preferences.versions_for(consumer) == {"a": Version("1")}


def test_disowned_tag_context_refuses_the_hint() -> None:
    consumer = target().with_marker_overrides(
        {"python_version": "3.13", "python_full_version": "3.13.0"}
    )

    assert LockPreferences(prior(wheel_package())).versions_for(consumer) == {}


def test_direct_source_versions_are_not_registry_preferences() -> None:
    source = Package(
        name=canonicalize_name("source"),
        version=Version("9"),
        archive=PackageArchive(
            url="https://old.example/source.zip", hashes={"sha256": "a" * 64}
        ),
    )

    assert LockPreferences(prior(wheel_package(), source)).versions_for(target()) == {
        "a": Version("1")
    }


@pytest.mark.parametrize(
    "contents", ["invalid TOML", "lock-version = '1.0'\ncreated-by = false", ""]
)
def test_invalid_prior_locks_supply_no_preferences(
    tmp_path: Path, contents: str
) -> None:
    path = tmp_path / "pylock.toml"
    path.write_text(contents)

    assert read_lock_preferences(path) is None


def test_missing_prior_lock_supplies_no_preferences(tmp_path: Path) -> None:
    assert read_lock_preferences(tmp_path / "pylock.toml") is None


def test_valid_prior_read_shares_registry_summary_without_local_versions(
    tmp_path: Path,
) -> None:
    path = tmp_path / "pylock.toml"
    path.write_text(
        'lock-version="1.0"\ncreated-by="nab"\n'
        '[[packages]]\nname="a"\nversion="1"\n'
        '[[packages.wheels]]\nname="a-1-py3-none-any.whl"\n'
        'url="https://old.example/a-1-py3-none-any.whl"\n'
        f'hashes={{sha256="{"a" * 64}"}}\n'
        '[[packages]]\nname="local"\n[packages.directory]\npath="./local"\n'
    )

    preferences = read_lock_preferences(path)

    assert preferences is not None
    assert preferences.summary_versions() == {"a": Version("1")}
    assert preferences.versions_for(target()) == {"a": Version("1")}


def test_soft_and_checked_preferences_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError, match="pass preferences or lock_preferences"):
        resolve_with_coordinator(
            make_coordinator(auto_metadata=True),
            [target()],
            [Requirement("a")],
            preferences={"a": Version("1")},
            lock_preferences=LockPreferences(prior(wheel_package())),
        )


@pytest.mark.parametrize("base_group", [None, "lint"])
def test_current_base_group_controls_applicable_previous_versions(
    base_group: str | None,
) -> None:
    preferences = LockPreferences(
        prior(wheel_package(marker='"lint" in dependency_groups'))
    )
    port = make_coordinator(
        listings={"a": [make_wheel(str(version), package="a") for version in (1, 2)]},
        auto_metadata=True,
    )

    result = resolve_with_coordinator(
        port,
        [target()],
        [Requirement("a")],
        lock_preferences=preferences,
        inputs=ResolveInputs(build_policy=BuildPolicy.NEVER, base_group=base_group),
    )
    result.raise_for_failure()

    assert result.target_results[0].pins == {"a": Version("1" if base_group else "2")}


def test_new_root_skips_an_impossible_unchanged_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared: list[bool] = []
    original = Provider.prepare_preferred_releases

    def observe(
        self: Provider,
        preferred: Mapping[str, Version],
        *,
        unchanged_only: bool = False,
    ) -> None:
        prepared.append(unchanged_only)
        original(self, preferred, unchanged_only=unchanged_only)

    monkeypatch.setattr(Provider, "prepare_preferred_releases", observe)
    port = make_coordinator(
        listings={name: [make_wheel("1", package=name)] for name in ("a", "new")},
        auto_metadata=True,
    )

    result = resolve_with_coordinator(
        port,
        [target()],
        [Requirement("a"), Requirement("new")],
        lock_preferences=LockPreferences(prior(wheel_package())),
        inputs=ResolveInputs(build_policy=BuildPolicy.NEVER),
    )
    result.raise_for_failure()

    assert result.target_results[0].pins == {"a": Version("1"), "new": Version("1")}
    assert prepared == []


def test_default_and_explicit_groups_share_the_extra_context() -> None:
    condition = '"speed" in extras and "lint" in dependency_groups and "docs" in dependency_groups'
    complement = '"speed" not in extras or "lint" not in dependency_groups or "docs" not in dependency_groups'
    lock = Pylock(
        lock_version=Version("1.0"),
        created_by="nab",
        extras=(canonicalize_name("speed"),),
        dependency_groups=("lint", "docs"),
        default_groups=("lint",),
        packages=(
            wheel_package(version="1", marker=condition),
            wheel_package(version="2", marker=complement),
        ),
    )
    lock.validate()
    preferences = LockPreferences(lock)
    selected = (("extra", "speed"), ("group", "docs"))

    assert preferences.versions_for(target(), selection=selected) == {"a": Version("1")}
    assert preferences.versions_for(
        target(), selection=selected, default_groups=()
    ) == {"a": Version("2")}


def test_target_fork_members_are_used_when_no_selection_is_passed() -> None:
    preferences = LockPreferences(
        prior(
            wheel_package(version="1", marker='"speed" in extras'),
            wheel_package(version="2", marker='"speed" not in extras'),
        )
    )

    assert preferences.versions_for(target().with_selection((("extra", "speed"),))) == {
        "a": Version("1")
    }


def test_modified_prior_model_uses_the_ordinary_preference_search() -> None:
    """A new dependency must not commit a model built in a different decision order."""

    old = LockPreferences(prior(wheel_package("a"), wheel_package("b")))

    def registry() -> FakeFetchPort:
        """Make a1 newly require b2 while newer a releases can retain b1."""
        a_wheels = [
            make_wheel(str(v), package="a", hashes=(("sha256", "b" * 64),))
            for v in range(1, 5)
        ]
        b_wheels = [
            make_wheel(str(v), package="b", hashes=(("sha256", "c" * 64),))
            for v in (1, 2)
        ]
        metadata = {}
        for wheel in a_wheels:
            assert wheel.metadata_url is not None
            metadata[wheel.metadata_url] = make_metadata(
                "a", wheel.version, "b==2" if wheel.version == "1" else "b==1"
            )
        for wheel in b_wheels:
            assert wheel.metadata_url is not None
            metadata[wheel.metadata_url] = make_metadata("b", wheel.version)
        return make_coordinator(
            listings={"a": a_wheels, "b": b_wheels}, metadata_by_url=metadata
        )

    class Pins:
        """Record application pin notifications."""

        def __init__(self) -> None:
            """Start before any application pin is committed."""
            self.values: list[int] = []

        def on_fetch(self) -> None:
            pass

        def on_pin(self, decided: int) -> None:
            self.values.append(decided)

    roots = [Requirement("a"), Requirement("b>=1")]
    inputs = ResolveInputs(
        build_policy=BuildPolicy.NEVER, decision_order=DecisionOrder.STABLE
    )
    ordinary_pins = Pins()
    ordinary = resolve_with_coordinator(
        registry(),
        [target()],
        roots,
        inputs=inputs,
        preferences={"a": Version("1"), "b": Version("1")},
        progress=ordinary_pins,
    )
    reused_pins = Pins()
    reused = resolve_with_coordinator(
        registry(),
        [target()],
        roots,
        inputs=inputs,
        lock_preferences=old,
        progress=reused_pins,
    )
    ordinary.raise_for_failure()
    reused.raise_for_failure()

    assert ordinary.target_results[0].pins == {"a": Version("4"), "b": Version("1")}
    assert reused.target_results[0].pins == ordinary.target_results[0].pins
    assert reused.target_results[0].lock == ordinary.target_results[0].lock
    assert reused_pins.values == ordinary_pins.values


def test_previous_lock_only_prepares_the_selected_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    older = make_wheel("1", package="a", hashes=(("sha256", "b" * 64),))
    newer = make_wheel(
        "2", package="a", tag="cp312-cp312-win_amd64", hashes=(("sha256", "c" * 64),)
    )
    seen: set[str] = set()
    accepts = TagSet.accepts

    def observe(self: TagSet, filename: str) -> bool:
        seen.add(filename)
        return accepts(self, filename)

    monkeypatch.setattr(TagSet, "accepts", observe)
    files = append_yanked_file(
        [older, newer], make_wheel("0", package="a"), reason="withdrawn"
    )
    port = make_coordinator(listings={"a": files}, auto_metadata=True)

    result = resolve_with_coordinator(
        port,
        [target()],
        [Requirement("a")],
        lock_preferences=LockPreferences(prior(wheel_package())),
        inputs=ResolveInputs(build_policy=BuildPolicy.NEVER),
    )
    result.raise_for_failure()

    assert result.target_results[0].pins == {"a": Version("1")}
    assert seen == {older.filename}
    assert port.calls_to("request_metadata")
    assert result.target_results[0].lock is not None
    assert result.target_results[0].lock.pins["a"].wheels[0].hashes == (
        ("sha256", "b" * 64),
    )


def conditional_prior() -> LockPreferences:
    """Retain independent Linux and Windows choices from one valid old lock."""
    return LockPreferences(
        prior(
            wheel_package("a", "1", marker='sys_platform == "linux"'),
            wheel_package("a", "2", marker='sys_platform == "win32"'),
            wheel_package("b", "1", marker='sys_platform == "linux"'),
            wheel_package("b", "2", marker='sys_platform == "win32"'),
        )
    )


def changed_registry(case: str) -> FakeFetchPort:
    """Serve current declarations independently of the old lock's artifacts."""
    a_wheels = []
    metadata: dict[str, str] = {}
    for version in ("1", "2", "3"):
        wheel = make_wheel(
            version,
            package="a",
            url=f"https://current.example/a-{version}.whl",
            hashes=(("sha256", "b" * 64),),
        )
        if case == "removed" and version == "1":
            continue
        a_wheels.append(wheel)
        dependencies = [f"b=={version}"]
        requires_python = None
        if version == "1":
            if case in {"metadata", "source"}:
                dependencies = ["b==3"]
            elif case == "python":
                requires_python = ">=3.13"
        assert wheel.metadata_url is not None
        metadata[wheel.metadata_url] = make_metadata(
            "a",
            version,
            *dependencies,
            requires_python=requires_python,
            provides_extra=("new",) if version == "3" else (),
        )
    a_files: list[WheelFile | SdistFile] = list(a_wheels)
    if case == "yanked":
        a_files = append_yanked_file(
            list(a_wheels[1:]), a_wheels[0], reason="withdrawn"
        )
    listings = {"a": a_files}
    for package in ("b", "bonus", "unrelated"):
        files = []
        for version in ("1", "2", "3"):
            wheel = make_wheel(version, package=package, hashes=(("sha256", "c" * 64),))
            files.append(wheel)
            assert wheel.metadata_url is not None
            metadata[wheel.metadata_url] = make_metadata(package, version)
        listings[package] = files
    port = make_coordinator(listings=listings, metadata_by_url=metadata)
    if case == "source":
        port.indexes.append(IndexConfig("current", "https://current.example/simple/"))
        port.index.store_listing_index("a", "current")
    return port


@pytest.mark.parametrize(
    "case",
    [
        "addition",
        "requirements",
        "metadata",
        "python",
        "removed",
        "yanked",
        "source",
        "extra",
        "group",
        "unrelated",
    ],
)
def test_changed_inputs_recheck_current_choices(case: str) -> None:
    preferences = conditional_prior()
    consumers = [target(), target("windows_amd64")]
    roots = [
        Requirement(
            "a>=3" if case == "requirements" else "a[new]" if case == "extra" else "a"
        )
    ]
    if case == "group":
        consumers = [item.with_selection((("group", "new"),)) for item in consumers]
        roots.append(Requirement("bonus"))
    inputs = ResolveInputs(
        build_policy=BuildPolicy.NEVER, decision_order=DecisionOrder.STABLE
    )
    expected = []
    for consumer in consumers:
        normal = resolve_with_coordinator(
            changed_registry(case),
            [consumer],
            roots,
            inputs=inputs,
            preferences=preferences.versions_for(
                consumer, selection=consumer.selection, default_groups=()
            ),
        )
        normal.raise_for_failure()
        expected.append(normal.target_results[0])

    reused = resolve_with_coordinator(
        changed_registry(case),
        consumers,
        roots,
        inputs=inputs,
        lock_preferences=preferences,
    )
    reused.raise_for_failure()

    assert [item.pins for item in reused.target_results] == [
        item.pins for item in expected
    ]
    assert [item.lock for item in reused.target_results] == [
        item.lock for item in expected
    ]
    for result in reused.target_results:
        assert result.lock is not None
        for pin in result.lock.pins.values():
            assert all(
                wheel.url is not None
                and not wheel.url.startswith("https://old.example/")
                for wheel in pin.wheels
            )
    if case in {"metadata", "source"}:
        assert reused.target_results[0].pins["b"] == Version("3")
    if case in {"removed", "yanked", "python", "requirements", "extra"}:
        assert reused.target_results[0].pins["a"] == Version("3")
