"""Search ordinary release versions before resolving individual file identities."""

from __future__ import annotations

from itertools import chain
from typing import TYPE_CHECKING, Generic, TypeVar, cast

from packaging.ranges import VersionRange
from packaging.requirements import Requirement
from packaging.version import Version

from nab_resolver.errors import (
    ResolutionError,
    ResolutionInvariantError,
    ResolutionTerminatedError,
)
from nab_resolver.priority import compute_tier
from nab_resolver.resolver import BaseProvider, Resolver
from nab_resolver.types import Incompatibility, IncompatibilityCause, Term

from ._compat import override
from .lookahead import (
    LOOKAHEAD_ABORT_THRESHOLD,
    DecisionConflict,
    find_dependency_conflict,
    queue_blocker,
)
from .resolution import PreparedResolution
from .versions import VersionCandidate

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping

    from nab_resolver.types import RangeProtocol

    from .preference import DependencyData

__all__ = ["PreparedVersionResolution"]

_Choice = TypeVar("_Choice", bound=VersionCandidate)
# A tuple asks for extra-specific metadata once the exact base file is selected.
_Package = str | tuple[str, Version]
_METADATA_VERSION = Version("0")
_METADATA_RANGE = VersionRange.singleton(_METADATA_VERSION)


class _NeedsFileSearchError(Exception):
    """A release number cannot represent the choice or its admission policy."""


class PreparedVersionResolution(PreparedResolution[_Choice, VersionRange, Requirement]):
    """Try PEP 440 version ranges before the catalogue's exact-file live search."""

    @override
    def try_live_candidates(self) -> dict[str, _Choice] | None:
        """Try release ranges, falling back when a file identity matters."""
        if not self.provider.roots:
            return super().try_live_candidates()

        self.provider.preference.require_remaining_rounds()
        query = _ReleaseProvider(self)
        resolver: Resolver[_Package, Version] = Resolver(
            query,
            max_iterations=self.remaining_rounds,
            range_type=VersionRange,
            root_version=_METADATA_VERSION,
        )
        # Only package names are roots. The query introduces metadata keys.
        roots: dict[_Package, RangeProtocol[Version]] = dict(
            self.provider.roots.items()
        )
        constraints: dict[_Package, RangeProtocol[Version]] = dict(
            self.provider.constraints.items()
        )
        try:
            pins = resolver.resolve(roots, constraints)
        except ResolutionTerminatedError:
            raise
        except (_NeedsFileSearchError, ResolutionError):
            # A release-level failure can still have a solution using another file.
            pass
        else:
            selected = {
                name: query.files[name, version]
                for name, version in pins.items()
                if isinstance(name, str)
            }
            if not query.has_untried_alternatives(selected) and self.admits_selection(
                selected
            ):
                return selected
        finally:
            self.provider.preference.record_solver_stats(resolver.stats)
            self.provider.preference.spend(resolver.stats.rounds)

        return super().try_live_candidates()


class _ReleaseProvider(BaseProvider[_Package, Version], Generic[_Choice]):
    """Use one file per release for a disposable search over PEP 440 ranges."""

    def __init__(self, session: PreparedVersionResolution[_Choice]) -> None:
        self.catalogue = session.provider.catalogue
        self.roots = session.provider.roots
        self.root_order = {name: index for index, name in enumerate(self.roots)}
        self.preferred = session.preferred_candidate
        self.considered = session.provider.on_candidate_considered
        self.exact_dependencies = session.provider.preference.exact_dependencies

        self.files: dict[tuple[str, Version], _Choice] = {}
        self.pins: dict[str, set[Requirement]] = {}
        for pin in session.provider.input_pins:
            self.pins.setdefault(self.catalogue.pin_project(pin), set()).add(pin)

        self.decisions: Mapping[_Package, Version] = {}
        self.ranges: Mapping[_Package, VersionRange] = {}
        self.clauses: list[Incompatibility[_Package, Version]] = []
        self.backtracks: list[str] = []
        self.backtrack_counts: dict[str, int] = {}

    def receive_decision_scan_hint(
        self,
        positive_ranges: Mapping[_Package, RangeProtocol[Version]],
        decisions: Mapping[_Package, Version],
    ) -> None:
        # The owning resolver constructs every bound with VersionRange.
        self.ranges = cast("Mapping[_Package, VersionRange]", positive_ranges)
        self.decisions = decisions

    @override
    def begin_decision_scan(self) -> Callable[[_Package], bool]:
        return lambda package: isinstance(package, tuple)

    @override
    def is_ready(self, package: _Package) -> bool:
        if isinstance(package, str):
            return True

        name, version = package
        return self.decisions.get(self.catalogue.base_name(name)) == version

    def prioritize(
        self,
        package: _Package,
        version_range: RangeProtocol[Version],
        conflict_counts: Mapping[_Package, int],
        culprit_counts: Mapping[_Package, int] | None = None,
    ) -> tuple[int, ...]:
        if not isinstance(package, str):
            return (-1,)

        allowed = self._version_range(version_range)
        base = self.catalogue.base_name(package)
        tier = compute_tier(
            package,
            conflict_counts.get(package, 0),
            0 if culprit_counts is None else culprit_counts.get(package, 0),
            culprit_counts,
            force_backtracked=package in self.backtrack_counts,
        )
        matches = len(self.catalogue.ordered(package, allowed))
        preferred = None if self.preferred is None else self.preferred(package)

        # Empty and single-file domains come first. For wider domains keep the
        # incumbent and input order ahead of the remaining file count.
        return (
            0,
            tier,
            min(matches, 2),
            int(preferred is None or preferred.version not in allowed),
            self.root_order.get(package, len(self.roots)),
            matches,
            int(base == package),
        )

    @staticmethod
    def _version_range(value: RangeProtocol[Version]) -> VersionRange:
        # PreparedVersionResolution constructs its resolver with this backend.
        return cast("VersionRange", value)

    def _prepare(self, choice: _Choice) -> DependencyData[VersionRange, Requirement]:
        data = self.catalogue.prepare(choice)
        if data is None:
            # Rejected or unavailable metadata says nothing about another file.
            raise _NeedsFileSearchError

        return data

    def choose_version(
        self, package: _Package, version_range: RangeProtocol[Version]
    ) -> Version | None:
        if not isinstance(package, str):
            return (
                _METADATA_VERSION
                if self.has_satisfying_version(package, version_range)
                else None
            )

        choices = self._live_files(package, self._version_range(version_range))
        first = next(choices, None)
        if first is None:
            return None
        if self.catalogue.candidate_base(first) != package:
            # Extra metadata is scheduled after the matching base choice.
            return first.version

        return self._scan_dependencies(package, first, choices)

    def _live_files(self, package: str, allowed: VersionRange) -> Iterator[_Choice]:
        """Keep a stable file identity for each version in this search."""
        for choice in self.catalogue.ordered(package, allowed):
            if self.catalogue.candidate_withdrawn(choice):
                base = self.catalogue.candidate_base(choice)
                if any(
                    self.catalogue.pin_allows(pin, choice)
                    for pin in self.pins.get(base, ())
                ):
                    raise _NeedsFileSearchError
                continue

            if self.files.setdefault((package, choice.version), choice) != choice:
                if self._file_rejected(choice):
                    continue
                raise _NeedsFileSearchError

            if self.considered is not None:
                self.considered(choice)
            yield choice

    def _file_rejected(self, choice: _Choice) -> bool:
        facts = self.catalogue.facts
        if choice in facts and facts[choice] is None:
            return True

        # An extra always requires this particular base file.
        if self.catalogue.candidate_base(choice) != self.catalogue.candidate_package(
            choice
        ):
            base = self.catalogue.base_candidate(choice)
            return base in facts and facts[base] is None
        return False

    def has_untried_alternatives(self, selected: Mapping[str, _Choice]) -> bool:
        """Check whether a discarded release still has another eligible file."""
        discarded: dict[str, dict[Version, _Choice]] = {}
        for (package, version), choice in self.files.items():
            # An extra can lose on its required base version before its file's
            # metadata contributes any dependency to the search.
            if selected.get(package) != choice and choice in self.catalogue.facts:
                discarded.setdefault(package, {})[version] = choice

        # A downstream conflict can eliminate the entire release without asking
        # for its next file. Only the file search can discard an untried sibling.
        for package, releases in discarded.items():
            for file in self.catalogue.choices(package):
                tried = releases.get(file.version)
                if (
                    tried is None
                    or file == tried
                    or self.catalogue.candidate_withdrawn(file)
                ):
                    continue

                if not self._file_rejected(file):
                    return True
        return False

    def _scan_dependencies(
        self, package: str, first: _Choice, rest: Iterable[_Choice]
    ) -> Version | None:
        """Keep the dependency clauses for releases skipped during this scan."""
        rejected: list[Incompatibility[_Package, Version]] = []
        blockers: set[tuple[str, Version] | None] = set()
        for choice in chain((first,), rest):
            data = self._prepare(choice)
            conflict = find_dependency_conflict(
                data.dependencies, self.roots, self.decisions, self.ranges
            )
            if conflict is None or conflict.package == package:
                self.clauses.extend(rejected)
                return choice.version

            parent: Term[_Package, Version] = Term(
                package, VersionRange.singleton(choice.version), positive=True
            )
            child: Term[_Package, Version] = Term(
                conflict.package, conflict.required, positive=False
            )
            rejected.append(
                Incompatibility([parent, child], cause=IncompatibilityCause.DEPENDENCY)
            )

            if isinstance(conflict, DecisionConflict):
                blockers.add((conflict.package, conflict.version))
                if len(rejected) >= LOOKAHEAD_ABORT_THRESHOLD and len(blockers) == 1:
                    # Let the first release's actual dependency drive the backjump.
                    queue_blocker(
                        conflict.package, self.backtrack_counts, self.backtracks
                    )
                    return first.version
            else:
                blockers.add(None)

        self.clauses.extend(rejected)
        return None

    @override
    def consume_pending_clauses(self) -> list[Incompatibility[_Package, Version]]:
        pending = self.clauses
        self.clauses = []
        return pending

    @override
    def consume_force_backtrack_targets(self) -> list[_Package]:
        targets: list[_Package] = [*self.backtracks]
        self.backtracks.clear()
        return targets

    def has_satisfying_version(
        self, package: _Package, version_range: RangeProtocol[Version]
    ) -> bool:
        if not isinstance(package, str):
            return _METADATA_VERSION in version_range

        return any(
            not self.catalogue.candidate_withdrawn(choice)
            for choice in self.catalogue.ordered(
                package, self._version_range(version_range)
            )
        )

    def get_dependencies(
        self, package: _Package, version: Version
    ) -> dict[_Package, RangeProtocol[Version]]:
        if isinstance(package, str):
            choice = self.files[package, version]
            base = self.catalogue.candidate_base(choice)
            if base != package:
                return {
                    base: VersionRange.singleton(version),
                    (package, version): _METADATA_RANGE,
                }
        else:
            choice = self.files[package]
            base = self.catalogue.candidate_base(choice)
            base_file = self.files.get((base, choice.version))
            if not self.is_ready(package) or base_file != self.catalogue.base_candidate(
                choice
            ):
                raise _NeedsFileSearchError

        data = self._prepare(choice)
        for pin in data.pins:
            self.pins.setdefault(self.catalogue.pin_project(pin), set()).add(pin)
        dependencies: dict[_Package, RangeProtocol[Version]] = dict(
            data.dependencies.items()
        )
        if self.exact_dependencies is not None:
            for child in self.exact_dependencies(choice):
                name = self.catalogue.candidate_package(child)
                if name not in dependencies:
                    msg = f"Exact candidate {child!r} has no prepared dependency edge"
                    raise ResolutionInvariantError(msg)

                # A release choice can bind a known file (including its own base).
                # An unseen exact file belongs to the file search, before another
                # artifact's listing or metadata is requested for that requirement.
                if self.files.get((name, child.version)) != child:
                    raise _NeedsFileSearchError

                dependencies[name] &= VersionRange.singleton(child.version)
        return dependencies

    def widen_decision(self, package: _Package, version: Version) -> None:
        # Dependency facts belong to the actual selected file at this release.
        del package, version
