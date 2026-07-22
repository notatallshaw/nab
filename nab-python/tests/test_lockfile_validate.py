"""Unit tests for the resolver-free disqualification checks."""

from __future__ import annotations

from nab_python._vendor.packaging.pylock import Pylock
from nab_python._vendor.packaging.specifiers import SpecifierSet
from nab_python._vendor.packaging.utils import canonicalize_name
from nab_python._vendor.packaging.version import Version
from nab_python.lockfile import LockDisqualification, check_envelope


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
) -> LockDisqualification | None:
    return check_envelope(
        committed,
        requires_python=requires_python,
        extras=extras,
        dependency_groups=dependency_groups,
        default_groups=default_groups,
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
