"""Unit tests for the resolver-free disqualification checks."""

from __future__ import annotations

from nab_python._lockfile.validate import (
    check_constraints,
    check_direct_requirements,
    check_envelope,
)
from nab_python._vendor.packaging.markers import Marker
from nab_python._vendor.packaging.pylock import (
    Package,
    PackageDirectory,
    Pylock,
)
from nab_python._vendor.packaging.requirements import Requirement
from nab_python._vendor.packaging.specifiers import SpecifierSet
from nab_python._vendor.packaging.utils import canonicalize_name
from nab_python._vendor.packaging.version import Version
from nab_python.lockfile import LockDisqualification, RootRequirement
from nab_python.tags import PlatformSpec
from nab_python.target import ResolveTarget


def make_pylock(
    *,
    requires_python: str | None = None,
    extras: tuple[str, ...] | None = None,
    dependency_groups: tuple[str, ...] | None = None,
    default_groups: tuple[str, ...] | None = None,
) -> Pylock:
    return Pylock(
        lock_version=Version("1.0"),
        requires_python=SpecifierSet(requires_python) if requires_python else None,
        extras=(
            tuple(canonicalize_name(e) for e in extras) if extras is not None else None
        ),
        dependency_groups=(
            tuple(canonicalize_name(g) for g in dependency_groups)
            if dependency_groups is not None
            else None
        ),
        default_groups=(
            tuple(canonicalize_name(g) for g in default_groups)
            if default_groups is not None
            else None
        ),
        created_by="nab",
        packages=(),
    )


def envelope(
    committed: Pylock,
    *,
    requires_python: str | None = None,
    extras: tuple[str, ...] = (),
    dependency_groups: tuple[str, ...] = (),
    default_groups: tuple[str, ...] = (),
    base_group: str | None = None,
) -> LockDisqualification | None:
    return check_envelope(
        committed,
        requires_python=requires_python,
        extras=extras,
        dependency_groups=dependency_groups,
        default_groups=default_groups,
        base_group=base_group,
    )


def test_requires_python_changed_fires() -> None:
    committed = make_pylock(requires_python=">=3.8")
    result = envelope(committed, requires_python=">=3.9")
    assert result is not None
    assert result.reason == (
        "the lockfile requires-python >=3.8 does not match this run's >=3.9"
    )


def test_requires_python_reformatted_but_equal_does_not_fire() -> None:
    committed = make_pylock(requires_python=">=3.8,<4")
    assert envelope(committed, requires_python="<4,>=3.8") is None


def test_requires_python_both_absent_does_not_fire() -> None:
    committed = make_pylock(requires_python=None)
    assert envelope(committed, requires_python=None) is None


def test_requires_python_empty_string_is_absent() -> None:
    committed = make_pylock(requires_python=None)
    assert envelope(committed, requires_python="") is None


def test_requires_python_committed_present_current_absent_fires() -> None:
    committed = make_pylock(requires_python=">=3.8")
    result = envelope(committed, requires_python=None)
    assert result is not None
    assert result.reason == (
        "the lockfile requires-python >=3.8 does not match this run's (none)"
    )


def test_requires_python_committed_absent_current_present_fires() -> None:
    committed = make_pylock(requires_python=None)
    result = envelope(committed, requires_python=">=3.8")
    assert result is not None
    assert result.reason == (
        "the lockfile requires-python (none) does not match this run's >=3.8"
    )


def test_extras_added_fires() -> None:
    committed = make_pylock(extras=None)
    result = envelope(committed, extras=("cli",))
    assert result is not None
    assert result.reason == (
        "the lockfile was built with extras {} but this run selects {cli}"
    )


def test_extras_removed_fires() -> None:
    committed = make_pylock(extras=("cli",))
    result = envelope(committed, extras=())
    assert result is not None
    assert result.reason == (
        "the lockfile was built with extras {cli} but this run selects {}"
    )


def test_extras_reordered_does_not_fire() -> None:
    committed = make_pylock(extras=("cli", "test"))
    assert envelope(committed, extras=("test", "cli")) is None


def test_extras_empty_selection_matches_none_committed() -> None:
    committed = make_pylock(extras=None)
    assert envelope(committed, extras=()) is None


def test_extras_normalized_name_equivalence_does_not_fire() -> None:
    committed = make_pylock(extras=("fancy-cli",))
    assert envelope(committed, extras=("Fancy_CLI",)) is None


def test_dependency_groups_added_fires() -> None:
    committed = make_pylock(dependency_groups=None)
    result = envelope(committed, dependency_groups=("dev",))
    assert result is not None
    assert result.reason == (
        "the lockfile was built with dependency-groups {} but this run selects {dev}"
    )


def test_dependency_groups_removed_fires() -> None:
    committed = make_pylock(dependency_groups=("dev",))
    result = envelope(committed, dependency_groups=())
    assert result is not None
    assert result.reason == (
        "the lockfile was built with dependency-groups {dev} but this run selects {}"
    )


def test_dependency_groups_reordered_does_not_fire() -> None:
    committed = make_pylock(dependency_groups=("dev", "docs"))
    assert envelope(committed, dependency_groups=("docs", "dev")) is None


def test_default_groups_added_fires() -> None:
    committed = make_pylock(default_groups=None)
    result = envelope(committed, default_groups=("main",))
    assert result is not None
    assert result.reason == (
        "the lockfile was built with default-groups {} but this run selects {main}"
    )


def test_default_groups_removed_fires() -> None:
    committed = make_pylock(default_groups=("main",))
    result = envelope(committed, default_groups=())
    assert result is not None
    assert result.reason == (
        "the lockfile was built with default-groups {main} but this run selects {}"
    )


def test_default_groups_reordered_does_not_fire() -> None:
    committed = make_pylock(default_groups=("main", "extra"))
    assert envelope(committed, default_groups=("extra", "main")) is None


def test_the_named_base_group_does_not_fire() -> None:
    """The writer adds it to both arrays and no run selects it."""
    committed = make_pylock(
        dependency_groups=("dev", "default"),
        default_groups=("main", "default"),
    )
    assert (
        envelope(
            committed,
            dependency_groups=("dev",),
            default_groups=("main",),
            base_group="default",
        )
        is None
    )


def test_the_name_is_looked_for_in_dependency_groups() -> None:
    """``default-groups`` alone is a lock no installer can activate it from."""
    committed = make_pylock(dependency_groups=("dev",), default_groups=("default",))
    result = envelope(committed, dependency_groups=("dev",), base_group="default")
    assert result is not None
    assert "does not name 'default'" in result.reason


def test_a_lock_predating_the_option_fires_on_the_missing_name() -> None:
    """A lock written before ``base-group`` was set names no group at all."""
    committed = make_pylock(dependency_groups=None, default_groups=None)
    result = envelope(committed, default_groups=("test",), base_group="default")
    assert result is not None
    assert "does not name 'default'" in result.reason


def test_a_lock_naming_a_different_base_group_fires() -> None:
    """The option was renamed since, so the committed name is stale."""
    committed = make_pylock(
        dependency_groups=("dev", "default"),
        default_groups=("main", "default"),
    )
    result = envelope(
        committed,
        dependency_groups=("dev",),
        default_groups=("main",),
        base_group="default-1",
    )
    assert result is not None
    assert "does not name 'default-1'" in result.reason


def test_all_envelope_fields_matching_returns_none() -> None:
    committed = make_pylock(
        requires_python=">=3.9",
        extras=("cli",),
        dependency_groups=("dev",),
        default_groups=("main",),
    )
    assert (
        envelope(
            committed,
            requires_python=">=3.9",
            extras=("cli",),
            dependency_groups=("dev",),
            default_groups=("main",),
        )
        is None
    )


def test_requires_python_checked_before_extras() -> None:
    committed = make_pylock(requires_python=">=3.8", extras=None)
    result = envelope(committed, requires_python=">=3.9", extras=("cli",))
    assert result is not None
    assert result.reason.startswith("the lockfile requires-python")


def test_lock_disqualification_is_frozen() -> None:
    disq = LockDisqualification(reason="x")
    try:
        disq.reason = "y"  # type: ignore[misc]
    except AttributeError:
        return
    msg = "LockDisqualification should be frozen"
    raise AssertionError(msg)


LINUX_ENV = {"sys_platform": "linux"}

# A target standing for the whole 3.11 minor, so its python_full_version is
# the synthesized 3.11.0 floor.
MINOR_TARGET = ResolveTarget.for_declared(
    python_version="3.11", spec=PlatformSpec("linux_x86_64")
)
MINOR_ENV = MINOR_TARGET.marker_env


def index_pin(name: str, version: str) -> Package:
    return Package(name=canonicalize_name(name), version=Version(version))


def marked_pin(name: str, version: str, marker: str) -> Package:
    return Package(
        name=canonicalize_name(name),
        version=Version(version),
        marker=Marker(marker),
    )


def directory_pin(name: str, version: str | None = None) -> Package:
    return Package(
        name=canonicalize_name(name),
        version=Version(version) if version is not None else None,
        directory=PackageDirectory(path=f"./{name}"),
    )


def pylock_of(*packages: Package) -> Pylock:
    return Pylock(
        lock_version=Version("1.0"),
        created_by="nab",
        packages=packages,
    )


def root(text: str, source: str = "[project].dependencies") -> RootRequirement:
    return RootRequirement(requirement=Requirement(text), source=source)


def test_direct_present_and_satisfying_returns_none() -> None:
    committed = pylock_of(index_pin("foo", "2.5"))
    assert (
        check_direct_requirements(committed, [root("foo>=2.0")], marker_env=LINUX_ENV)
        is None
    )


def test_direct_present_and_violating_fires() -> None:
    committed = pylock_of(index_pin("foo", "1.5"))
    result = check_direct_requirements(
        committed, [root("foo>=2.0")], marker_env=LINUX_ENV
    )
    assert result is not None
    assert result.reason == (
        "[project].dependencies requires foo>=2.0 but the lock pins foo 1.5"
    )


def test_direct_missing_fires() -> None:
    committed = pylock_of(index_pin("other", "1.0"))
    result = check_direct_requirements(
        committed, [root("bar>=1")], marker_env=LINUX_ENV
    )
    assert result is not None
    assert result.reason == (
        "[project].dependencies requires bar and its marker applies here, "
        "but the lock has no bar pin"
    )


def test_direct_missing_names_the_source() -> None:
    committed = pylock_of()
    result = check_direct_requirements(
        committed,
        [root("bar>=1", source="[project.optional-dependencies].cli")],
        marker_env=LINUX_ENV,
    )
    assert result is not None
    assert result.reason.startswith("[project.optional-dependencies].cli requires bar")


def test_direct_marker_true_applies_and_fires() -> None:
    committed = pylock_of(index_pin("foo", "1.5"))
    result = check_direct_requirements(
        committed,
        [root('foo>=2.0; sys_platform == "linux"')],
        marker_env=LINUX_ENV,
    )
    assert result is not None
    assert result.reason.startswith("[project].dependencies requires foo>=2.0")


def test_direct_prerelease_pin_inside_specifier_does_not_fire() -> None:
    """A pre-release pin is not on its own a reason to disqualify the lock."""
    committed = pylock_of(index_pin("foo", "2.0b1"))
    assert (
        check_direct_requirements(committed, [root("foo>=2.0b1")], marker_env=LINUX_ENV)
        is None
    )


def test_direct_prerelease_pin_under_bare_requirement_does_not_fire() -> None:
    committed = pylock_of(index_pin("foo", "2.0b1"))
    assert (
        check_direct_requirements(committed, [root("foo")], marker_env=LINUX_ENV)
        is None
    )


def test_direct_prerelease_pin_outside_specifier_fires() -> None:
    committed = pylock_of(index_pin("foo", "1.0b1"))
    result = check_direct_requirements(
        committed, [root("foo>=2.0")], marker_env=LINUX_ENV
    )
    assert result is not None
    assert result.reason == (
        "[project].dependencies requires foo>=2.0 but the lock pins foo 1.0b1"
    )


def test_direct_versionless_pin_skipped() -> None:
    committed = pylock_of(directory_pin("foo"))
    assert (
        check_direct_requirements(committed, [root("foo>=2.0")], marker_env=LINUX_ENV)
        is None
    )


def test_direct_pin_with_version_skips_version_check() -> None:
    committed = pylock_of(directory_pin("foo", "1.0"))
    assert (
        check_direct_requirements(committed, [root("foo>=2.0")], marker_env=LINUX_ENV)
        is None
    )


def test_direct_duplicate_versioned_pins_skipped() -> None:
    committed = pylock_of(
        marked_pin("foo", "1.5", "'old' in extras"),
        marked_pin("foo", "2.5", "'new' in extras"),
    )
    assert (
        check_direct_requirements(committed, [root("foo<2")], marker_env=LINUX_ENV)
        is None
    )
    assert (
        check_direct_requirements(committed, [root("foo>=2")], marker_env=LINUX_ENV)
        is None
    )


def test_direct_reference_skipped() -> None:
    committed = pylock_of(directory_pin("foo", "1.0"))
    assert (
        check_direct_requirements(
            committed,
            [root("foo @ https://example.com/foo-9.9-py3-none-any.whl")],
            marker_env=LINUX_ENV,
        )
        is None
    )


def test_direct_marker_false_absent_does_not_fire() -> None:
    committed = pylock_of()
    assert (
        check_direct_requirements(
            committed,
            [root('bar>=1; sys_platform == "win32"')],
            marker_env=LINUX_ENV,
        )
        is None
    )


def test_direct_indeterminate_form_skipped() -> None:
    committed = pylock_of()
    assert (
        check_direct_requirements(
            committed,
            [root('bar>=1; extras == "x"')],
            marker_env=LINUX_ENV,
        )
        is None
    )


def test_direct_undefined_variable_skipped() -> None:
    """A marker naming a variable the environment omits is indeterminate.

    ``extras`` and the other lockfile-only set variables are seeded empty,
    so they evaluate to False rather than raising. A standard variable the
    environment does not supply still raises
    :class:`UndefinedEnvironmentName`, and an item nab cannot decide for is
    skipped rather than reported as unsatisfied.
    """
    committed = pylock_of()
    assert (
        check_direct_requirements(
            committed,
            [root('bar>=1; python_version >= "3.12"')],
            marker_env={"sys_platform": "linux"},
        )
        is None
    )


def test_direct_no_requirements_returns_none() -> None:
    assert check_direct_requirements(pylock_of(), [], marker_env=LINUX_ENV) is None


def test_direct_marker_splitting_the_minor_skipped() -> None:
    """A requirement gated at a micro boundary is undecided at the floor.

    It holds in some of the minor's slices and not others. The skip covers
    the presence check too, which is coarser than the proof needs: the lower
    slice alone would settle a name the lock never pins.
    """
    committed = pylock_of()
    assert (
        check_direct_requirements(
            committed,
            [root('bar>=1; python_full_version < "3.11.4"')],
            marker_env=MINOR_ENV,
            resolve_target=MINOR_TARGET,
        )
        is None
    )


def test_direct_marker_not_splitting_the_minor_still_fires() -> None:
    """A micro-axis marker that does not split the minor is decided at the floor."""
    committed = pylock_of()
    result = check_direct_requirements(
        committed,
        [root('bar>=1; python_full_version >= "3.9"')],
        marker_env=MINOR_ENV,
        resolve_target=MINOR_TARGET,
    )
    assert result is not None
    assert result.reason.startswith("[project].dependencies requires bar")


def test_direct_marker_on_a_micro_boundary_at_a_point_fires() -> None:
    committed = pylock_of()
    result = check_direct_requirements(
        committed,
        [root('bar>=1; python_full_version < "3.11.4"')],
        marker_env=MINOR_ENV,
    )
    assert result is not None
    assert result.reason.startswith("[project].dependencies requires bar")


def test_constraint_satisfied_returns_none() -> None:
    committed = pylock_of(index_pin("baz", "2.0"))
    assert (
        check_constraints(committed, [Requirement("baz<3")], marker_env=LINUX_ENV)
        is None
    )


def test_constraint_violated_fires() -> None:
    committed = pylock_of(index_pin("baz", "3.1"))
    result = check_constraints(committed, [Requirement("baz<3")], marker_env=LINUX_ENV)
    assert result is not None
    assert result.reason == ("the constraint baz<3 is violated by the pinned baz 3.1")


def test_constraint_satisfied_by_prerelease_pin_does_not_fire() -> None:
    """A constraint bounds the version without excluding pre-releases."""
    committed = pylock_of(index_pin("baz", "2.0b1"))
    assert (
        check_constraints(committed, [Requirement("baz<3")], marker_env=LINUX_ENV)
        is None
    )


def test_constraint_violated_by_prerelease_pin_fires() -> None:
    committed = pylock_of(index_pin("baz", "3.1b1"))
    result = check_constraints(committed, [Requirement("baz<3")], marker_env=LINUX_ENV)
    assert result is not None
    assert result.reason == "the constraint baz<3 is violated by the pinned baz 3.1b1"


def test_constraint_marker_false_skipped() -> None:
    committed = pylock_of(index_pin("baz", "3.1"))
    assert (
        check_constraints(
            committed,
            [Requirement('baz<3; sys_platform == "win32"')],
            marker_env=LINUX_ENV,
        )
        is None
    )


def test_constraint_indeterminate_skipped() -> None:
    committed = pylock_of(index_pin("baz", "3.1"))
    assert (
        check_constraints(
            committed,
            [Requirement('baz<3; extras == "x"')],
            marker_env=LINUX_ENV,
        )
        is None
    )


def test_constraint_absent_package_noop() -> None:
    committed = pylock_of(index_pin("other", "1.0"))
    assert (
        check_constraints(committed, [Requirement("baz<3")], marker_env=LINUX_ENV)
        is None
    )


def test_constraint_versionless_pin_skipped() -> None:
    committed = pylock_of(directory_pin("baz"))
    assert (
        check_constraints(committed, [Requirement("baz<3")], marker_env=LINUX_ENV)
        is None
    )


def test_constraint_marker_splitting_the_minor_skipped() -> None:
    """A constraint gated below a micro boundary cannot judge a pin from above it."""
    committed = pylock_of(marked_pin("baz", "3.1", 'python_full_version >= "3.11.4"'))
    assert (
        check_constraints(
            committed,
            [Requirement('baz<3; python_full_version < "3.11.4"')],
            marker_env=MINOR_ENV,
            resolve_target=MINOR_TARGET,
        )
        is None
    )


def test_constraint_marker_the_split_cannot_tile_skipped() -> None:
    """A micro-axis clause the split cannot tile is undecided here too.

    It holds at the floor, so without the skip the check would fire on it.
    """
    committed = pylock_of(index_pin("baz", "3.1"))
    assert (
        check_constraints(
            committed,
            [Requirement('baz<3; python_full_version not in "3.11.4"')],
            marker_env=MINOR_ENV,
            resolve_target=MINOR_TARGET,
        )
        is None
    )


def test_constraint_marker_on_a_micro_boundary_at_a_point_fires() -> None:
    committed = pylock_of(index_pin("baz", "3.1"))
    result = check_constraints(
        committed,
        [Requirement('baz<3; python_full_version < "3.11.4"')],
        marker_env=MINOR_ENV,
    )
    assert result is not None
    assert result.reason == "the constraint baz<3 is violated by the pinned baz 3.1"


def test_constraint_marker_not_splitting_the_minor_still_fires() -> None:
    """Only a marker the minor's slices answer differently is indeterminate."""
    committed = pylock_of(index_pin("baz", "3.1"))
    result = check_constraints(
        committed,
        [Requirement('baz<3; python_full_version >= "3.9"')],
        marker_env=MINOR_ENV,
        resolve_target=MINOR_TARGET,
    )
    assert result is not None
    assert result.reason == "the constraint baz<3 is violated by the pinned baz 3.1"


def test_constraint_duplicate_versioned_pins_skipped() -> None:
    committed = pylock_of(
        marked_pin("baz", "1.5", "'old' in extras"),
        marked_pin("baz", "3.1", "'new' in extras"),
    )
    assert (
        check_constraints(committed, [Requirement("baz<3")], marker_env=LINUX_ENV)
        is None
    )


def test_root_requirement_is_frozen() -> None:
    rr = root("foo>=1")
    try:
        rr.source = "x"  # type: ignore[misc]
    except AttributeError:
        return
    msg = "RootRequirement should be frozen"
    raise AssertionError(msg)
