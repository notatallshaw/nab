"""Differential and law checks for the marker algebra.

Two ground-truth cross-checks against the vendored packaging:

* ``evaluate`` must agree with ``packaging.markers.Marker.evaluate`` on every
  generated (marker, environment) pair, for the environment-variable fragment.
* the decision procedures must never be UNSOUND: a claimed disjoint / implies /
  equivalent is checked against an exact packaging grid, and any disagreement
  where the algebra is more permissive than the grid is a failure.

Both use a deterministic, CI-fast generator (a few hundred cases).
"""

from __future__ import annotations

import itertools
import random

import pytest

from nab_python._vendor.packaging.markers import Marker
from nab_python._vendor.packaging.markersets import MarkerSet
from nab_python._vendor.packaging.version import Version

# Version values straddle every literal the alphabets use
# (3.9, 3.10, 3.14), including the 3.14 prerelease-carve-out witnesses, an epoch
# version (full ordering and major.minor truncation diverge), and a local one.
PFV_GRID = [
    "2.7.18",
    "3.8.0",
    "3.9.0",
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
    "1!3.9",
    "1!4.0",
    "2!0",
    "3.9.7+local",
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
]
STRING_ATOMS = [
    'sys_platform == "linux"',
    'sys_platform != "win32"',
    'platform_machine == "x86_64"',
    'platform_machine != "arm64"',
    'os_name == "posix"',
]
TWIN_ATOMS = [
    'implementation_version == "pypy"',
    'implementation_version != "foo"',
    'implementation_version >= "3.9"',
]
ALPHABET = VERSION_ATOMS + STRING_ATOMS + TWIN_ATOMS


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
