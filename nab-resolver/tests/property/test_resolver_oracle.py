"""End-to-end soundness oracle: closure on success, brute force on failure.

Scenarios are resolved directly from multi-package requirements (no
virtual root pin) with a constant-priority provider, so the decision
order differs from the count-prioritizing :class:`FuzzProvider` runs
elsewhere in this suite.  On success the result must be closed: every
requirement and every pinned package's dependencies are pinned in
range.  On failure a brute-force enumeration over all pin subsets must
agree that no solution exists.

The lookahead variant exercises the pending-clauses and force-backtrack
provider contract: the provider rejects candidates whose dependencies
conflict with current decisions, queues the corresponding sound clauses,
and periodically requests force backtracks, mimicking nab-python's
look-ahead.  Every queued clause is implied by a dependency fact, so any
unsoundness flagged here is the resolver's.
"""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_resolver.ranges import Range
from nab_resolver.resolver import ResolutionError, Resolver
from nab_resolver.types import Incompatibility, IncompatibilityCause, Term

from .strategies import DEEP_SETTINGS

if TYPE_CHECKING:
    from collections.abc import Mapping

    from nab_resolver.types import RangeProtocol

pytestmark = pytest.mark.property

ORACLE_TIMEOUT_SECONDS = 120

Versions = dict[str, list[int]]
Deps = dict[tuple[str, int], dict[str, Range[int]]]
Roots = dict[str, Range[int]]


@st.composite
def scenarios(draw: st.DrawFn) -> tuple[Versions, Deps, Roots]:
    """Generate (versions, deps, roots) over 2-4 packages, 2-3 versions."""
    n_packages = draw(st.integers(min_value=2, max_value=4))
    n_versions = draw(st.integers(min_value=2, max_value=3))
    packages = [f"p{i}" for i in range(n_packages)]
    versions: Versions = {p: list(range(1, n_versions + 1)) for p in packages}

    deps: Deps = {}
    for package in packages:
        others = [q for q in packages if q != package]
        for version in versions[package]:
            dep_packages = draw(
                st.lists(st.sampled_from(others), max_size=2, unique=True)
            )
            package_deps: dict[str, Range[int]] = {}
            for dep in dep_packages:
                lower = draw(st.integers(min_value=1, max_value=n_versions))
                upper = min(
                    n_versions, lower + draw(st.integers(min_value=0, max_value=2))
                )
                package_deps[dep] = Range.between(lower, upper + 1)
            deps[(package, version)] = package_deps

    root_count = draw(st.integers(min_value=1, max_value=min(3, n_packages)))
    root_packages = draw(
        st.lists(
            st.sampled_from(packages),
            min_size=root_count,
            max_size=root_count,
            unique=True,
        )
    )
    roots: Roots = {}
    for package in root_packages:
        lower = draw(st.integers(min_value=1, max_value=n_versions))
        upper = min(
            n_versions, lower + draw(st.integers(min_value=1, max_value=n_versions))
        )
        roots[package] = Range.between(lower, upper + 1)
    return versions, deps, roots


class ConstantPriorityProvider:
    """Newest-first provider whose priority is constant for every package."""

    def __init__(self, versions: Versions, deps: Deps) -> None:
        """Create a provider from versions and per-version dependency dicts."""
        self._versions = versions
        self._deps = deps

    def choose_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> int | None:
        """Pick the newest version within the allowed range."""
        best: int | None = None
        for version in self._versions.get(package, []):
            if version in version_range and (best is None or version > best):
                best = version
        return best

    def has_satisfying_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> bool:
        """Report whether any available version falls in the range."""
        return any(v in version_range for v in self._versions.get(package, []))

    def get_dependencies(self, package: str, version: int) -> dict[str, Range[int]]:
        """Return dependencies for a specific version."""
        return self._deps.get((package, version), {})

    def begin_decision_scan(self) -> None:
        """No-op: nothing in this stub moves under a scan."""

    def prioritize(
        self,
        package: str,
        version_range: RangeProtocol[int],
        conflict_counts: Mapping[str, int],
        culprit_counts: Mapping[str, int] | None = None,
    ) -> int:
        """Constant priority: decision order is left to the resolver."""
        del package, version_range, conflict_counts, culprit_counts
        return 0

    def is_ready(self, package: str) -> bool:
        """All packages decidable immediately in tests."""
        del package
        return True

    def receive_partial_solution_hint(
        self,
        positive_ranges: Mapping[str, RangeProtocol[int]],
        decisions: Mapping[str, int],
    ) -> None:
        """No-op: this provider does not use partial solution state."""
        del positive_ranges, decisions

    def consume_pending_clauses(self) -> list[Incompatibility[str, int]]:
        """No queued clauses."""
        return []

    def consume_force_backtrack_targets(self) -> list[str]:
        """No force-backtrack signal from this provider."""
        return []

    def widen_decision(self, package: str, version: int) -> RangeProtocol[int] | None:
        """No widening: dependency clauses keep the exact decided version."""
        del package, version
        return None

    def narrow_for_display(
        self, package: str, constraint: RangeProtocol[int]
    ) -> RangeProtocol[int]:
        """Identity: constraints render as stored."""
        del package
        return constraint


class LookaheadProvider:
    """Provider exercising the pending-clauses and force-backtrack contract.

    When the preferred candidate ``v`` for a package has a dependency
    whose range excludes that dependency's current decision ``w``, the
    provider rejects ``v``, queues the clause ``{package==v, dep==w}``,
    and on every ``force_every``-th blocker requests a force backtrack
    on the dependency.
    """

    def __init__(self, versions: Versions, deps: Deps, force_every: int) -> None:
        """Create a provider that force-backtracks on every nth blocker."""
        self._versions = versions
        self._deps = deps
        self._force_every = force_every
        self._decisions: dict[str, int] = {}
        self._pending: list[Incompatibility[str, int]] = []
        self._force_targets: list[str] = []
        self._blockers_seen = 0

    def choose_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> int | None:
        """Pick the newest unblocked version, queueing clauses for blocked ones."""
        candidates = sorted(
            (v for v in self._versions.get(package, []) if v in version_range),
            reverse=True,
        )
        for version in candidates:
            blockers: list[tuple[str, int]] = []
            for dep, dep_range in self._deps.get((package, version), {}).items():
                decided = self._decisions.get(dep)
                if decided is not None and decided not in dep_range:
                    blockers.append((dep, decided))
            if not blockers:
                return version
            for dep, decided in blockers:
                self._pending.append(
                    Incompatibility(
                        [
                            Term(package, Range.singleton(version), positive=True),
                            Term(dep, Range.singleton(decided), positive=True),
                        ],
                        cause=IncompatibilityCause.DEPENDENCY,
                    )
                )
                self._blockers_seen += 1
                if self._blockers_seen % self._force_every == 0:
                    self._force_targets.append(dep)
        return None

    def has_satisfying_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> bool:
        """Report whether any available version falls in the range."""
        return any(v in version_range for v in self._versions.get(package, []))

    def get_dependencies(self, package: str, version: int) -> dict[str, Range[int]]:
        """Return dependencies for a specific version."""
        return self._deps.get((package, version), {})

    def begin_decision_scan(self) -> None:
        """No-op: nothing in this stub moves under a scan."""

    def prioritize(
        self,
        package: str,
        version_range: RangeProtocol[int],
        conflict_counts: Mapping[str, int],
        culprit_counts: Mapping[str, int] | None = None,
    ) -> int:
        """Constant priority: decision order is left to the resolver."""
        del package, version_range, conflict_counts, culprit_counts
        return 0

    def is_ready(self, package: str) -> bool:
        """All packages decidable immediately in tests."""
        del package
        return True

    def receive_partial_solution_hint(
        self,
        positive_ranges: Mapping[str, RangeProtocol[int]],
        decisions: Mapping[str, int],
    ) -> None:
        """Track current decisions for the look-ahead check."""
        del positive_ranges
        self._decisions = dict(decisions)

    def consume_pending_clauses(self) -> list[Incompatibility[str, int]]:
        """Hand over and clear the queued clauses."""
        clauses = self._pending
        self._pending = []
        return clauses

    def consume_force_backtrack_targets(self) -> list[str]:
        """Hand over and clear the force-backtrack targets."""
        targets = self._force_targets
        self._force_targets = []
        return targets

    def widen_decision(self, package: str, version: int) -> RangeProtocol[int] | None:
        """No widening: dependency clauses keep the exact decided version."""
        del package, version
        return None

    def narrow_for_display(
        self, package: str, constraint: RangeProtocol[int]
    ) -> RangeProtocol[int]:
        """Identity: constraints render as stored."""
        del package
        return constraint


def _assert_closure(result: dict[str, int], deps: Deps, roots: Roots) -> None:
    """Assert the result satisfies all roots and is dependency-closed."""
    for package, required_range in roots.items():
        assert package in result, f"root {package!r} missing from result"
        assert result[package] in required_range, (
            f"root {package!r}@{result[package]} not in {required_range}"
        )
    for package, version in result.items():
        for dep, dep_range in deps.get((package, version), {}).items():
            assert dep in result, (
                f"dep {dep!r} of {package!r}@{version} missing from result"
            )
            assert result[dep] in dep_range, (
                f"dep {dep!r}@{result[dep]} of {package!r}@{version} not in {dep_range}"
            )


def _brute_force_sat(
    versions: Versions, deps: Deps, roots: Roots
) -> dict[str, int] | None:
    """Return a satisfying pin set (packages may be absent), or None."""
    packages = sorted(versions)
    for combo in itertools.product(*[[None, *versions[p]] for p in packages]):
        pins = {p: v for p, v in zip(packages, combo, strict=True) if v is not None}
        if any(p not in pins or pins[p] not in r for p, r in roots.items()):
            continue
        if all(
            dep in pins and pins[dep] in dep_range
            for (p, v) in pins.items()
            for dep, dep_range in deps.get((p, v), {}).items()
        ):
            return pins
    return None


def _check_scenario(
    resolver: Resolver[str, int],
    versions: Versions,
    deps: Deps,
    roots: Roots,
) -> None:
    """Resolve and check closure on success, brute-force unsat on failure."""
    try:
        result = resolver.resolve(roots)
    except ResolutionError as error:
        if "exceeded" in str(error):
            return
        witness = _brute_force_sat(versions, deps, roots)
        assert witness is None, (
            f"resolver reported impossible but {witness} works\n"
            f"roots={roots}\ndeps={deps}"
        )
        return
    _assert_closure(result, deps, roots)


class TestClosureSoundAgainstBruteForce:
    """resolve() output is closed; failures are brute-force verified unsat."""

    @pytest.mark.timeout(ORACLE_TIMEOUT_SECONDS)
    @given(scenario=scenarios())
    @DEEP_SETTINGS
    def test_plain_provider(self, scenario: tuple[Versions, Deps, Roots]) -> None:
        """Constant-priority provider results are closure-sound."""
        versions, deps, roots = scenario
        provider = ConstantPriorityProvider(versions, deps)
        resolver: Resolver[str, int] = Resolver(provider, max_iterations=5000)
        _check_scenario(resolver, versions, deps, roots)

    @pytest.mark.timeout(ORACLE_TIMEOUT_SECONDS)
    @given(
        scenario=scenarios(),
        force_every=st.integers(min_value=1, max_value=4),
    )
    @DEEP_SETTINGS
    def test_lookahead_provider(
        self, scenario: tuple[Versions, Deps, Roots], force_every: int
    ) -> None:
        """Pending clauses and force backtracks stay closure-sound."""
        versions, deps, roots = scenario
        provider = LookaheadProvider(versions, deps, force_every)
        resolver: Resolver[str, int] = Resolver(provider, max_iterations=5000)
        _check_scenario(resolver, versions, deps, roots)
