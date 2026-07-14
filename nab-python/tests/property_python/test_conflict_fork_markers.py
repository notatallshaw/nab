"""Semantic property tests for conflict-fork marker emission.

`PEP 751`_ consumers install from the emitted markers alone, so a
lock built from conflict forks must reproduce each fork's pins
exactly.  The model here is a universal resolve over 1-2
environments, each forked over a declared ``at_most_one`` conflict
between extras ``a`` and ``b``.  Per fork, each package name may be
pinned; the base (no-member) install set is the names pinned in
every fork.  Generation keeps the shape nab's builder produces: a
base name is pinned at the SAME version across an environment's
forks.

The oracle evaluates the emitted lockfile text per install context:

* extras={m}: exactly fork m's pins fire, at fork m's versions.
* extras=empty: exactly the base names fire, at the agreed version.

Any extra or missing firing entry is a correctness bug (silent over-
or under-install).

.. _PEP 751: https://peps.python.org/pep-0751/#packages
"""

from __future__ import annotations

import sys
from typing import NamedTuple

import pytest
from hypothesis import given
from hypothesis import strategies as st

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - 3.10 fallback
    import tomli as tomllib  # type: ignore[no-redef]

from nab_python._vendor.packaging.pylock import Pylock
from nab_python.config import (
    ConflictKind,
    ConflictMember,
    ConflictPolicy,
    ConflictSet,
)
from nab_python.lockfile import (
    IndexPin,
    LockInput,
    TargetLock,
    WheelArtifact,
    write_lock,
)
from nab_python.tags import PlatformSpec
from nab_python.target import ResolveTarget

from .strategies import PROPERTY_SETTINGS

pytestmark = pytest.mark.property

MEMBERS = ("a", "b")
NAMES = ("p1", "p2", "p3")
VERSIONS = ("1.0", "2.0")
ENVS = (
    ("linux_x86_64", "3.11"),
    ("windows_amd64", "3.11"),
)


class EnvForks(NamedTuple):
    """One environment's fork pins and base names."""

    platform: str
    python: str
    fork_pins: dict[str, dict[str, str]]
    base_names: frozenset[str]


def _pin(name: str, version: str) -> IndexPin:
    """Build a one-wheel ``IndexPin``."""
    return IndexPin(
        name=name,
        version=version,
        index="https://pypi.org/simple/",
        wheels=(
            WheelArtifact(
                filename=f"{name}-{version}-py3-none-any.whl",
                url=f"https://example.com/{name}-{version}-py3-none-any.whl",
                hashes=(("sha256", "a" * 64),),
            ),
        ),
    )


@st.composite
def fork_cases(draw: st.DrawFn) -> list[EnvForks]:
    """Generate per-environment fork pins with version-agreeing base names."""
    n_envs = draw(st.integers(min_value=1, max_value=2))
    case = []
    for plat, py in ENVS[:n_envs]:
        fork_pins: dict[str, dict[str, str]] = {}
        for member in MEMBERS:
            names = draw(
                st.lists(st.sampled_from(NAMES), min_size=0, max_size=3, unique=True)
            )
            fork_pins[member] = {n: draw(st.sampled_from(VERSIONS)) for n in names}
        in_all_forks = set(fork_pins["a"]) & set(fork_pins["b"])
        base_names = frozenset(
            draw(
                st.lists(
                    st.sampled_from(sorted(in_all_forks)),
                    unique=True,
                    max_size=len(in_all_forks),
                )
            )
            if in_all_forks
            else []
        )
        # Well-formedness: a base name agrees on version across forks.
        for n in base_names:
            fork_pins["b"][n] = fork_pins["a"][n]
        case.append(EnvForks(plat, py, fork_pins, base_names))
    return case


def _base_target(env_forks: EnvForks) -> ResolveTarget:
    """The unforked target for one generated environment."""
    return ResolveTarget.for_declared(
        python_version=env_forks.python, spec=PlatformSpec(env_forks.platform)
    )


def _environment(env_forks: EnvForks) -> dict[str, str]:
    """Build the marker environment for one generated environment."""
    return dict(_base_target(env_forks).marker_env)


def _build_input(case: list[EnvForks]) -> LockInput:
    """Assemble the per-tuple ``LockInput`` for the generated forks."""
    targets: dict[str, TargetLock] = {}
    env_base_names: dict[tuple[tuple[str, str], ...], frozenset[str]] = {}
    for env_forks in case:
        base = _base_target(env_forks)
        env_base_names[tuple(sorted(base.marker_env.items()))] = env_forks.base_names
        for member in MEMBERS:
            target = base.with_selection(((ConflictKind.EXTRA.value, member),))
            targets[target.label] = TargetLock(
                target=target,
                pins={n: _pin(n, v) for n, v in env_forks.fork_pins[member].items()},
            )
    conflicts = (
        ConflictSet(
            members=tuple(
                ConflictMember(kind=ConflictKind.EXTRA, name=m) for m in MEMBERS
            ),
            policy=ConflictPolicy.AT_MOST_ONE,
        ),
    )
    return LockInput(
        targets=targets,
        env_base_names=env_base_names,
        extras=tuple(MEMBERS),
        conflicts=conflicts,
    )


class TestQuoteSelectAgreesWithForkPins:
    """PEP 751, § Installation:

    > "Tools MUST NOT install a package if its ``marker`` evaluates
    > to false."

    ``packaging.pylock.Pylock.select`` is the spec-reference
    consumer: selecting with one conflict member active must install
    exactly that fork's pins, never raise an ambiguity error.

    Reference: https://peps.python.org/pep-0751/#installation
    """

    @given(case=fork_cases())
    @PROPERTY_SETTINGS
    def test_select_agrees_with_fork_pins(self, case: list[EnvForks]) -> None:
        """``Pylock.select`` per fork returns exactly the fork's pins."""
        text = write_lock(_build_input(case))
        pylock = Pylock.from_dict(tomllib.loads(text))
        for env_forks in case:
            env = _environment(env_forks)
            for member in MEMBERS:
                selected = {
                    str(pkg.name): str(pkg.version)
                    for pkg, _artifact in pylock.select(
                        environment=env,  # type: ignore[arg-type]
                        extras={member},
                        dependency_groups=(),
                    )
                }
                assert selected == env_forks.fork_pins[member], (
                    f"select(env={env_forks.platform}, extras={{{member}!r}}) "
                    f"-> {selected}, expected {env_forks.fork_pins[member]}\n{text}"
                )


class TestForkAndBaseInstallSets:
    """Direct marker evaluation per install context: with one member
    active, exactly that fork's pins fire at the fork's versions;
    with no member active, exactly the base names fire at the agreed
    version.  At most one entry may fire per name in any context.
    """

    @given(case=fork_cases())
    @PROPERTY_SETTINGS
    def test_fork_and_base_install_sets(self, case: list[EnvForks]) -> None:
        """Each extras context fires exactly its expected pin set."""
        text = write_lock(_build_input(case))
        pylock = Pylock.from_dict(tomllib.loads(text))

        for env_forks in case:
            env = _environment(env_forks)
            contexts: list[tuple[frozenset[str], dict[str, str]]] = [
                (
                    frozenset(),
                    {n: env_forks.fork_pins["a"][n] for n in env_forks.base_names},
                ),
                (frozenset({"a"}), dict(env_forks.fork_pins["a"])),
                (frozenset({"b"}), dict(env_forks.fork_pins["b"])),
            ]
            for extras, expected in contexts:
                ctx: dict[str, object] = dict(env)
                ctx["extras"] = extras
                ctx["dependency_groups"] = frozenset()
                fired: dict[str, list[str]] = {}
                for p in pylock.packages:
                    if p.marker is None or p.marker.evaluate(ctx):  # type: ignore[arg-type]
                        fired.setdefault(str(p.name), []).append(str(p.version))
                for name, vers in fired.items():
                    assert len(vers) == 1, (
                        f"{name}: multiple entries fire under "
                        f"env={env_forks.platform} extras={set(extras)}: {vers}\n{text}"
                    )
                got = {n: v[0] for n, v in fired.items()}
                assert got == expected, (
                    f"env={env_forks.platform} extras={set(extras)}: "
                    f"installed {got}, expected {expected}\n{text}"
                )
