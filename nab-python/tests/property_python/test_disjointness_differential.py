"""Differential property tests for :mod:`nab_python._lockfile.disjointness`.

`PEP 751`_ requires that all packages to be installed narrow down to
a single entry at install time, so ``validate_marker_disjointness``
must reject any pair of same-name entries whose markers can fire
together.  The validator prunes the extras/groups powerset via a
regex over marker strings plus a backtracking enumeration honouring
exclusive sets.  Each test here re-models one layer as a full
filtered-powerset brute force and requires exact agreement; a
divergence in either direction (validator raises but the model finds
no collision, or vice versa) is a bug.

.. _PEP 751: https://peps.python.org/pep-0751/#packages
"""

from __future__ import annotations

import itertools

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_python._conflict_kind import KIND_EXTRA, KIND_GROUP
from nab_python._lockfile.disjointness import (
    DisjointnessError,
    _enumerate_valid_points,
    validate_marker_disjointness,
)
from nab_python._vendor.packaging.markers import Marker
from nab_python._vendor.packaging.pylock import Package
from nab_python._vendor.packaging.utils import canonicalize_name
from nab_python._vendor.packaging.version import Version

from .strategies import BRUTE_FORCE_SETTINGS

pytestmark = pytest.mark.property

PLATFORMS = ("linux", "win32")
PYTHONS = ("3.10", "3.11")

EXTRA_DECLS = ("aa", "b-b", "c-c")
# Unnormalised spellings a marker may carry for each declared extra,
# plus one undeclared name; same shape for groups.
EXTRA_SPELLINGS = ("aa", "AA", "b-b", "B.B", "b_b", "c-c", "C_C", "zz")
GROUP_DECLS = ("dev", "test-g")
GROUP_SPELLINGS = ("dev", "DEV", "test-g", "Test_G", "qq")

env_atoms = st.one_of(
    st.sampled_from([f'sys_platform == "{p}"' for p in PLATFORMS]),
    st.sampled_from([f'python_version == "{v}"' for v in PYTHONS]),
)
extra_atoms = st.builds(
    lambda spelling, negate: f'"{spelling}" {"not in" if negate else "in"} extras',
    st.sampled_from(EXTRA_SPELLINGS),
    st.booleans(),
)
group_atoms = st.builds(
    lambda spelling, negate: (
        f'"{spelling}" {"not in" if negate else "in"} dependency_groups'
    ),
    st.sampled_from(GROUP_SPELLINGS),
    st.booleans(),
)
atoms = st.one_of(env_atoms, extra_atoms, group_atoms)


@st.composite
def markers(draw: st.DrawFn) -> Marker:
    """Generate a 1-3 atom marker mixing env and membership clauses."""
    n = draw(st.integers(min_value=1, max_value=3))
    parts = [draw(atoms) for _ in range(n)]
    joiners = [draw(st.sampled_from([" and ", " or "])) for _ in range(n - 1)]
    text = parts[0]
    for joiner, part in zip(joiners, parts[1:], strict=True):
        text += joiner + part
    return Marker(text)


@st.composite
def package_entries(draw: st.DrawFn) -> list[Package]:
    """Generate 2-3 same-name entries, each with an optional marker."""
    n = draw(st.integers(min_value=2, max_value=3))
    out = []
    for i in range(n):
        marker = None if draw(st.integers(0, 9)) == 0 else draw(markers())
        out.append(Package(name="pkg", version=Version(f"{i + 1}.0"), marker=marker))
    return out


@st.composite
def environment_maps(draw: st.DrawFn) -> dict[str, dict[str, str]]:
    """Generate 1-3 declared environments over the platform/python pools."""
    combos = draw(
        st.lists(
            st.tuples(st.sampled_from(PLATFORMS), st.sampled_from(PYTHONS)),
            min_size=1,
            max_size=3,
            unique=True,
        )
    )
    return {
        f"env{i}": {
            "sys_platform": plat,
            "python_version": py,
            "python_full_version": f"{py}.0",
        }
        for i, (plat, py) in enumerate(combos)
    }


member_items = st.one_of(
    st.tuples(st.just(KIND_EXTRA), st.sampled_from(EXTRA_SPELLINGS)),
    st.tuples(st.just(KIND_GROUP), st.sampled_from(GROUP_SPELLINGS)),
)

exclusive_sets = st.lists(
    st.frozensets(member_items, min_size=2, max_size=3),
    min_size=0,
    max_size=2,
)


def _powerset(items: list[str]) -> list[tuple[str, ...]]:
    """Return every subset of ``items``, including the empty one."""
    return list(
        itertools.chain.from_iterable(
            itertools.combinations(items, r) for r in range(len(items) + 1)
        )
    )


def _model_collides(
    packages: list[Package],
    environments: dict[str, dict[str, str]],
    extras: list[str],
    groups: list[str],
    exclusive: list[frozenset[tuple[str, str]]],
) -> bool:
    """Brute force the full powerset of the declared install-context universe."""
    canon_sets = [
        {(kind, canonicalize_name(name)) for kind, name in s} for s in exclusive
    ]
    seen_envs: list[dict[str, str]] = []
    for env in environments.values():
        if env in seen_envs:
            continue
        seen_envs.append(env)
    for env in seen_envs:
        for extras_subset in _powerset(extras):
            for groups_subset in _powerset(groups):
                active = {(KIND_EXTRA, canonicalize_name(e)) for e in extras_subset} | {
                    (KIND_GROUP, canonicalize_name(g)) for g in groups_subset
                }
                if any(len(s & active) >= 2 for s in canon_sets):
                    continue
                ctx: dict[str, object] = dict(env)
                ctx["extras"] = frozenset(extras_subset)
                ctx["dependency_groups"] = frozenset(groups_subset)
                matching = [
                    p
                    for p in packages
                    if p.marker is None or p.marker.evaluate(ctx)  # type: ignore[arg-type]
                ]
                if len(matching) >= 2:
                    return True
    return False


class TestValidatorAgainstPowersetModel:
    """``validate_marker_disjointness`` prunes which extras/groups
    subsets it visits (regex relevance filter plus exclusive-set
    backtracking); the model enumerates the FULL powerset of declared
    extras x groups per environment, filters subsets activating two
    or more members of an exclusive set, and evaluates every
    same-name entry's marker directly.

    The validator must raise exactly when the model finds two entries
    firing for one install context: a missed collision means a broken
    lockfile passes validation, a spurious raise rejects a valid one.
    """

    @given(
        packages=package_entries(),
        environments=environment_maps(),
        extras=st.lists(st.sampled_from(EXTRA_DECLS), unique=True, max_size=3),
        groups=st.lists(st.sampled_from(GROUP_DECLS), unique=True, max_size=2),
        exclusive=exclusive_sets,
    )
    @BRUTE_FORCE_SETTINGS
    def test_validator_agrees_with_brute_force(
        self,
        packages: list[Package],
        environments: dict[str, dict[str, str]],
        extras: list[str],
        groups: list[str],
        exclusive: list[frozenset[tuple[str, str]]],
    ) -> None:
        """Validator raises iff the full-powerset model finds a collision."""
        expected = _model_collides(packages, environments, extras, groups, exclusive)
        raised = False
        try:
            validate_marker_disjointness(
                packages,
                environments=environments,
                extras=extras,
                groups=groups,
                exclusive_groups=exclusive,
                declared_groups=exclusive,
            )
        except DisjointnessError:
            raised = True
        assert raised == expected, (
            f"validator={'raised' if raised else 'passed'} "
            f"model={'collision' if expected else 'disjoint'}\n"
            f"markers={[str(p.marker) for p in packages]}\n"
            f"envs={environments}\nextras={extras} groups={groups}\n"
            f"exclusive={exclusive}"
        )


POINT_NAMES = ("A_a", "b-B", "c", "D.d", "e", "F")


@st.composite
def enumeration_cases(
    draw: st.DrawFn,
) -> tuple[list[str], list[str], list[frozenset[tuple[str, str]]]]:
    """Generate (extras, groups, exclusive sets) over mixed-case names."""
    extras = draw(st.lists(st.sampled_from(POINT_NAMES), unique=True, max_size=4))
    groups = draw(st.lists(st.sampled_from(POINT_NAMES), unique=True, max_size=3))
    pool = [(KIND_EXTRA, n) for n in extras] + [(KIND_GROUP, n) for n in groups]
    exclusive: list[frozenset[tuple[str, str]]] = []
    for _ in range(draw(st.integers(min_value=0, max_value=3))):
        if not pool:
            break
        members = draw(
            st.lists(st.sampled_from(pool), min_size=1, max_size=3, unique=True)
        )
        # Sometimes use a different surface spelling of the same name.
        exclusive.append(
            frozenset(
                (kind, name.upper() if draw(st.booleans()) else name)
                for kind, name in members
            )
        )
    return extras, groups, exclusive


class TestEnumerationAgainstFilteredPowerset:
    """``_enumerate_valid_points`` backtracks instead of materialising
    the ``2^(E+G)`` powerset, skipping any subset that activates two
    or more members of one exclusive set (canonicalised comparison).

    The model generates the full powerset and filters it; the
    enumeration must produce exactly the same set of points.  A
    missing point hides a marker collision from the validator, an
    extra point makes it over-report.
    """

    @given(case=enumeration_cases())
    @BRUTE_FORCE_SETTINGS
    def test_enumeration_matches_filtered_powerset(
        self,
        case: tuple[list[str], list[str], list[frozenset[tuple[str, str]]]],
    ) -> None:
        """Enumerated points equal the conflict-filtered full powerset."""
        extras, groups, exclusive = case
        got = {
            (frozenset(extras_subset), frozenset(groups_subset))
            for extras_subset, groups_subset in _enumerate_valid_points(
                extras, groups, exclusive
            )
        }

        expected = set()
        for extras_subset in _powerset(sorted(extras)):
            for groups_subset in _powerset(sorted(groups)):
                active = {(KIND_EXTRA, canonicalize_name(n)) for n in extras_subset} | {
                    (KIND_GROUP, canonicalize_name(n)) for n in groups_subset
                }
                if any(
                    sum(
                        1
                        for kind, name in members
                        if (kind, canonicalize_name(name)) in active
                    )
                    >= 2
                    for members in exclusive
                ):
                    continue
                expected.add((frozenset(extras_subset), frozenset(groups_subset)))
        assert got == expected, (extras, groups, exclusive, got ^ expected)
