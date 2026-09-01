"""Differential and law checks for the marker algebra.

Two ground-truth cross-checks against packaging:

* ``evaluate`` must agree with ``packaging.markers.Marker.evaluate`` on every
  generated (marker, environment) pair, for the environment-variable fragment.
* the decision procedures must never be UNSOUND: a claimed disjoint / implies /
  equivalent is checked against an exact packaging grid, and any disagreement
  where the algebra is more permissive than the grid is a failure.

On one string variable the grid is replaced by an exhaustive string domain,
which pins emptiness in both directions rather than one.

All three use a deterministic, CI-fast generator (a few hundred cases).
"""

from __future__ import annotations

import itertools
import random

import pytest

from nab_markersets._packaging import Marker, Version
from nab_markersets.markersets import MarkerSet

# Version values straddle every literal the alphabets use
# (3.9, 3.10, 3.14), including the 3.14 prerelease-carve-out witnesses, an epoch
# version (full ordering and major.minor truncation diverge), a local one, and
# the deeper releases that are the only shape entering the band between two
# adjacent literals.
PFV_GRID = [
    "2.7.18",
    "3.8.0",
    "3.9.0",
    "3.9.0.1",
    "3.9.0b2",
    "3.9.7",
    "3.10.0",
    "3.10.4",
    "3.13.0",
    "3.14.0rc1",
    "3.14.0rc2",
    "3.14.0",
    "3.14.0.post2",
    "3.14.0.post6",
    "3.14.2",
    "3.15.0",
    "1!3.0.1",
    "1!3.9",
    "1!4.0",
    "2!0",
    "3.9.7+local",
    "3.9.0.1",
    "1!3.0.1",
]
SYS_GRID = ["linux", "win32", "darwin"]
MACHINE_GRID = ["x86_64", "aarch64", "arm64"]


def _mm(pfv: str) -> str:
    release = Version(pfv).release
    major = release[0]
    minor = release[1] if len(release) > 1 else 0
    return f"{major}.{minor}"


def _grid() -> list[dict[str, str]]:
    envs = []
    for pfv, sysp, machine in itertools.product(PFV_GRID, SYS_GRID, MACHINE_GRID):
        envs.append(
            {
                "python_full_version": pfv,
                "python_version": _mm(pfv),
                "sys_platform": sysp,
                "platform_machine": machine,
                "os_name": "posix" if sysp != "win32" else "nt",
                "platform_system": {"linux": "Linux", "win32": "Windows"}.get(
                    sysp, "Darwin"
                ),
                "platform_python_implementation": "CPython",
                "implementation_name": "cpython",
                "implementation_version": pfv,
                "platform_release": "6.6.0",
                "platform_version": "#1 SMP",
            }
        )
    for release in ("6", "6.0.1", "6.1", "6.1.0.4", "7", "generic"):
        envs.append({**envs[0], "platform_release": release})
    return envs


GRID = _grid()

# A twin such as implementation_version dispatches as a version yet may carry a
# non-version string; pin environments that hold one so the differential covers
# the twin's string branch, not only its version-shaped default.
GRID = [
    *GRID,
    {**GRID[0], "implementation_version": "pypy"},
    {**GRID[0], "implementation_version": "foo"},
]

# platform_release is one value for the whole product above, so the values that
# separate its atoms are pinned here instead of widening every environment.
GRID = [
    *GRID,
    *(
        {**GRID[0], "platform_release": release}
        for release in ("6", "6.0.1", "6.1", "6.1.0.4", "7", "generic")
    ),
]

VERSION_ATOMS = [
    'python_full_version < "3.14"',
    'python_full_version >= "3.14"',
    'python_full_version >= "3.14.dev0"',
    'python_full_version == "3.9"',
    'python_full_version != "3.10"',
    'python_version == "3.9"',
    'python_version >= "3.10"',
    'python_version < "3.14"',
    'python_full_version <= "3.10.4"',
    'python_full_version > "3.9.7"',
    'python_full_version > "3.14.0rc1"',
    'python_full_version > "3.14.0.post5"',
    'python_full_version < "3.14.0rc2"',
    'python_full_version > "3.9.0b2"',
    'python_version > "3.14.0rc1"',
    'python_full_version > "1!3.9"',
    'python_full_version ~= "1!3.9"',
    'python_full_version > "3.9"',
    'python_full_version < "3.9.1"',
    'python_full_version > "1!3"',
    'python_full_version < "1!3.1"',
]
STRING_ATOMS = [
    'sys_platform == "linux"',
    'sys_platform != "win32"',
    'platform_machine == "x86_64"',
    'platform_machine != "arm64"',
    'os_name == "posix"',
    'sys_platform in "linuxwin32"',
    '"win" in sys_platform',
    '"posix" not in os_name',
    '"arm" in platform_machine',
    'os_name >= "posix"',
]
TWIN_ATOMS = [
    'implementation_version == "pypy"',
    'implementation_version != "foo"',
    'implementation_version >= "3.9"',
    'platform_release > "6"',
    'platform_release < "6.1"',
    'platform_release > "6.1"',
    'platform_release <= "6.1.0.4"',
]

# packaging's string operator table reads < and > as constant false and <= and
# >= as equality, and a version-dispatch variable falls onto that table whenever
# the literal builds no specifier. Both degradations reshape the complement, so
# the alphabet carries one of each: a wildcard and a local version are the
# literals an ordered specifier rejects while an equality specifier accepts.
DEGRADED_ATOMS = [
    'sys_platform < "linux"',
    'platform_machine > "x86_64"',
    'python_full_version < "3.*"',
    'python_full_version <= "3.*"',
    'implementation_version > "pypy"',
    'implementation_version >= "3.9+local"',
]
ALPHABET = VERSION_ATOMS + STRING_ATOMS + TWIN_ATOMS + DEGRADED_ATOMS


def _random_marker(rng: random.Random, depth: int) -> str:
    if depth == 0 or rng.random() < 0.45:
        return rng.choice(ALPHABET)
    joiner = rng.choice([" and ", " or "])
    return f"({_random_marker(rng, depth - 1)}{joiner}{_random_marker(rng, depth - 1)})"


def test_evaluate_matches_packaging() -> None:
    rng = random.Random(20260717)  # noqa: S311
    compared = 0
    for _ in range(300):
        text = _random_marker(rng, 3)
        packaging_marker = Marker(text)
        algebra = MarkerSet.from_marker(text)
        for env in GRID:
            assert algebra.evaluate(env) == packaging_marker.evaluate(env), (text, env)
            compared += 1
    assert compared > 3000


def _grid_disjoint(a: Marker, b: Marker) -> bool:
    return not any(a.evaluate(e) and b.evaluate(e) for e in GRID)


def _grid_implies(a: Marker, b: Marker) -> bool:
    return all(b.evaluate(e) for e in GRID if a.evaluate(e))


def _grid_equivalent(a: Marker, b: Marker) -> bool:
    return all(a.evaluate(e) == b.evaluate(e) for e in GRID)


def test_decisions_are_never_unsound() -> None:
    """A claimed disjoint/implies/equivalent must hold on the exact grid."""
    rng = random.Random(4242)  # noqa: S311
    checked = 0
    for _ in range(400):
        ta = _random_marker(rng, 2)
        tb = _random_marker(rng, 2)
        pa, pb = Marker(ta), Marker(tb)
        sa, sb = MarkerSet.from_marker(ta), MarkerSet.from_marker(tb)
        if sa.is_disjoint(sb):
            assert _grid_disjoint(pa, pb), ("disjoint", ta, tb)
        if sa.is_subset(sb):
            assert _grid_implies(pa, pb), ("implies", ta, tb)
        if sa.equivalent(sb):
            assert _grid_equivalent(pa, pb), ("equivalent", ta, tb)
        checked += 1
    assert checked == 400


def test_every_atom_pair_decides_soundly() -> None:
    """Every two-atom conjunction and disjunction, checked against the grid.

    The random walk reaches a given pair of atoms too rarely to pin one, and a
    band closed by two adjacent literals is exactly a pair.
    """
    pairs = 0
    for left, right in itertools.product(ALPHABET, repeat=2):
        for joiner in (" and ", " or "):
            text = f"{left}{joiner}{right}"
            algebra = MarkerSet.from_marker(text)
            marker = Marker(text)
            if algebra.is_empty():
                assert not any(marker.evaluate(e) for e in GRID), ("empty", text)
            if algebra.is_full():
                assert all(marker.evaluate(e) for e in GRID), ("tautology", text)
            pairs += 1
    assert pairs == 2 * len(ALPHABET) ** 2


def test_is_empty_and_tautology_never_unsound() -> None:
    rng = random.Random(99)  # noqa: S311
    for _ in range(300):
        text = _random_marker(rng, 3)
        marker = Marker(text)
        algebra = MarkerSet.from_marker(text)
        if algebra.is_empty():
            assert not any(marker.evaluate(e) for e in GRID), ("empty", text)
        if algebra.is_full():
            assert all(marker.evaluate(e) for e in GRID), ("tautology", text)


# ------------------------------------------------- exhaustive string axis

# A string variable is compared for exact equality, tested for being a
# substring of a literal, and tested for carrying one; its ordered operators are
# constant. So a finite domain of strings decides it exactly. The domain below
# is every string of up to three characters over "abc", which covers the atoms'
# literals ("a", "b", "ab"), their concatenations, and the separator "c" the
# engine mints, without claiming to be the smallest such domain.
STRING_DOMAIN = [
    "".join(chars)
    for width in range(4)
    for chars in itertools.product("abc", repeat=width)
]

STRING_AXIS_ATOMS = [
    'os_name == "ab"',
    'os_name != "ab"',
    'os_name == "a"',
    'os_name in "ab"',
    'os_name not in "ab"',
    '"a" in os_name',
    '"a" not in os_name',
    '"b" in os_name',
    '"ab" in os_name',
    '"ab" not in os_name',
]

_STRING_AXIS_TRIPLES = 150


def _string_axis_markers(rng: random.Random) -> list[str]:
    """Every pair of string-axis atoms under both joiners, plus random triples."""
    out = [
        f"{left} {joiner} {right}"
        for left, right in itertools.product(STRING_AXIS_ATOMS, repeat=2)
        for joiner in ("and", "or")
    ]
    out.extend(
        " ".join(
            (
                rng.choice(STRING_AXIS_ATOMS),
                rng.choice(("and", "or")),
                rng.choice(STRING_AXIS_ATOMS),
                rng.choice(("and", "or")),
                rng.choice(STRING_AXIS_ATOMS),
            )
        )
        for _ in range(_STRING_AXIS_TRIPLES)
    )
    return out


def test_string_axis_emptiness_is_exact() -> None:
    """A string axis is neither over- nor under-approximated.

    Both directions are asserted against packaging over the finite domain, so a
    substring test read apart from the variable's value fails here: it reports
    a set as inhabited that no string inhabits.
    """
    rng = random.Random(20260829)  # noqa: S311
    markers = _string_axis_markers(rng)
    for text in markers:
        satisfiable = any(
            Marker(text).evaluate({"os_name": value}) for value in STRING_DOMAIN
        )
        assert MarkerSet.from_marker(text).is_empty() == (not satisfiable), text
    assert len(markers) == 2 * len(STRING_AXIS_ATOMS) ** 2 + _STRING_AXIS_TRIPLES


SUMMARY_PREFIX = "<MarkerSet '"
SUMMARY_SUFFIX = "'>"

# The words repr uses where no marker string applies.
PLACEHOLDERS = frozenset({"universe", "empty", "unrepresentable", "too deeply nested"})


def _summary(marker_set: MarkerSet) -> str:
    """Return the marker-string summary inside a set's repr."""
    rendered = repr(marker_set)
    assert rendered.startswith(SUMMARY_PREFIX), rendered
    assert rendered.endswith(SUMMARY_SUFFIX), rendered
    return rendered[len(SUMMARY_PREFIX) : -len(SUMMARY_SUFFIX)]


def test_repr_is_total_and_agrees_on_the_grid() -> None:
    """repr renders every set, and what it renders denotes that set."""
    rng = random.Random(20260829)  # noqa: S311
    spelled = 0
    universes = 0
    for _ in range(300):
        text = _random_marker(rng, 2)
        base = MarkerSet.from_marker(text)
        for candidate in (base, ~base, base & ~base, base | ~base):
            summary = _summary(candidate)
            if summary in PLACEHOLDERS:
                if summary == "universe":
                    universes += 1
                continue
            marker = Marker(summary)
            for env in GRID:
                assert marker.evaluate(env) == candidate.evaluate(env), (summary, env)
            spelled += 1

    assert spelled > 300
    # A complement that normalises to a constant is the shape with no atom to
    # render, so the run reaches that path only while it produces some.
    assert universes > 0


# --------------------------------------------------------------------- laws

LAW_ALPHABET = VERSION_ATOMS[:6] + STRING_ATOMS[:3]


def _rand(rng: random.Random) -> MarkerSet:
    return MarkerSet.from_marker(rng.choice(LAW_ALPHABET))


@pytest.mark.parametrize("seed", range(25))
def test_boolean_algebra_laws(seed: int) -> None:
    rng = random.Random(seed)  # noqa: S311
    a, b, c = _rand(rng), _rand(rng), _rand(rng)
    t, f = MarkerSet.full(), MarkerSet.empty()

    assert (a & b).complement().equivalent(a.complement() | b.complement())
    assert (a | b).complement().equivalent(a.complement() & b.complement())
    assert a.complement().complement().equivalent(a)
    assert (a & (a | b)).equivalent(a)
    assert (a | (a & b)).equivalent(a)
    assert (a & (b | c)).equivalent((a & b) | (a & c))
    assert (a | (b & c)).equivalent((a | b) & (a | c))
    assert (a & t).equivalent(a)
    assert (a | f).equivalent(a)
    assert (a | t).equivalent(t)
    assert (a & f).equivalent(f)
    assert (a | a.complement()).is_full()
    assert (a & a.complement()).is_empty()
