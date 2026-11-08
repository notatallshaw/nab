"""Keep priority work proportional to changed inputs during availability sweeps."""

from collections import Counter
from collections.abc import Mapping

from nab_resolver import decide
from nab_resolver.ranges import Range
from nab_resolver.resolver import BaseProvider, Resolver
from nab_resolver.types import (
    Incompatibility,
    IncompatibilityCause,
    RangeProtocol,
    Term,
)


class Provider(BaseProvider[str, int]):
    def __init__(self) -> None:
        self.priorities: Counter[str] = Counter()

    def choose_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> int | None:
        return next((version for version in (1, 2) if version in version_range), None)

    def get_dependencies(self, package: str, version: int) -> Mapping[str, Range[int]]:
        return {}

    def has_satisfying_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> bool:
        return any(version in version_range for version in (1, 2))

    def widen_decision(self, package: str, version: int) -> None:
        del package, version

    def prioritize(
        self,
        package: str,
        version_range: RangeProtocol[int],
        conflict_counts: Mapping[str, int],
        culprit_counts: Mapping[str, int] | None = None,
    ) -> int:
        self.priorities[package] += 1
        if package == "b":
            return 1 if 1 not in version_range else 9
        return 0 if package == "a" else 2


def test_empty_exclusions_do_not_recompute_unchanged_priorities() -> None:
    provider = Provider()
    requirements = {f"p{index}": Range.singleton(1) for index in range(8)}
    resolver = Resolver(provider, availability_generation=lambda: 0)
    solution = resolver.solve(requirements)

    assert solution.pins == dict.fromkeys(requirements, 1)
    assert provider.priorities == Counter(dict.fromkeys(requirements, 1))


def test_excluded_scan_preserves_changes_for_the_priority_queue() -> None:
    resolver = Resolver(Provider())
    for package in ("a", "b", "c"):
        term = Term(package, Range.from_versions((1, 2)), positive=True)
        cause = Incompatibility([term.negate()], cause=IncompatibilityCause.ROOT)
        resolver.solution.derive(
            term.package, term.constraint, positive=True, cause=cause
        )
    assert decide.choose_package_to_decide(resolver, set()) == "a"

    term = Term("b", Range.singleton(2), positive=True)
    cause = Incompatibility([term.negate()], cause=IncompatibilityCause.ROOT)
    resolver.solution.derive(term.package, term.constraint, positive=True, cause=cause)
    assert decide.choose_package_to_decide(resolver, {"a", "b"}) == "c"
    resolver.solution.decide("a", 1)

    assert decide.choose_package_to_decide(resolver, set()) == "b"
