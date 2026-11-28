# Embed the resolver in your own tool

`nab-resolver` is the PubGrub core nab drives, published on its own and with no dependencies. It
knows nothing about Python packaging: package identity, version ordering and dependency metadata all
come from you, through a provider.

This page builds a provider over an in-memory graph, resolves with it, and reads the failure report.
If you already have a finder for Python packages, skip to
[Resolve Python package files](#resolve-python-package-files).

Install it with `pip install nab-resolver`.

## What you supply

A provider answers eleven methods. `BaseProvider` supplies six of them, which leaves five to write:

`choose_version`
: Pick a version of a package inside a range, or return `None` when none fits.

`has_satisfying_version`
: Return whether `choose_version` could select a version in the given range, without changing
  subsequent version choices. The method may retain diagnostic information.

`get_dependencies`
: Report what a version depends on, as a range per dependency.

`prioritize`
: Return a sort key deciding which undecided package to take next. Lower goes first.

`widen_decision`
: Return a range that may replace a decided version in clauses, or `None` for the version alone.
  `Range.full()` is unsound when versions have different dependencies: it assigns one version's
  dependencies to every version and can make a solvable graph fail. A widening provider also
  overrides `narrow_for_display`; otherwise reports name widened ranges.

`ResolverProvider` in `nab_resolver.resolver` documents all eleven, and its docstrings are the full
contract. Subclassing is optional: the resolver accepts any object satisfying that protocol.

Within one resolution, `get_dependencies(package, version)` must describe the same dependencies on
every call. Keep each returned mapping unchanged until the resolution finishes, including result
construction. Range values must be immutable. Returning a fresh equal mapping is allowed. Use
distinct package/version keys for candidates with different dependencies, or start a new resolution.
`widen_decision` may return a different parent range on a later decision, provided it still
satisfies its contract.

Which candidates you can use may change as the resolver selects dependencies. Check the active
requirements and your prerelease and yanked-file rules before preparing a candidate. If you widen a
range, it must also be valid for versions that could become eligible later in the search.

## A provider over an in-memory graph

Packages are strings and versions are integers here. A package can be any hashable value. Versions
must also be hashable and ordered by their range type. Their hashes must remain stable while
retained by the resolver, and equal versions must have equal hashes.

```python
from collections.abc import Mapping

from nab_resolver.errors import ResolutionError
from nab_resolver.ranges import Range
from nab_resolver.resolver import BaseProvider, Resolver
from nab_resolver.types import RangeProtocol

Graph = Mapping[str, Mapping[int, Mapping[str, Range[int]]]]


class NewestFirstProvider(BaseProvider[str, int]):
    """Answer from an in-memory graph, preferring the newest version."""

    def __init__(self, graph: Graph) -> None:
        self._graph = graph

    def _versions(self, package: str) -> list[int]:
        """Every known version of ``package``, newest first."""
        return sorted(self._graph.get(package, {}), reverse=True)

    def choose_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> int | None:
        return next((v for v in self._versions(package) if v in version_range), None)

    def has_satisfying_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> bool:
        return any(v in version_range for v in self._versions(package))

    def get_dependencies(self, package: str, version: int) -> Mapping[str, Range[int]]:
        return self._graph.get(package, {}).get(version, {})

    def prioritize(
        self,
        package: str,
        version_range: RangeProtocol[int],
        conflict_counts: Mapping[str, int],
        culprit_counts: Mapping[str, int] | None = None,
    ) -> int:
        return sum(1 for v in self._versions(package) if v in version_range)

    def widen_decision(self, package: str, version: int) -> RangeProtocol[int] | None:
        return None
```

Counting the versions still in range is PubGrub's usual `prioritize` heuristic: the most constrained
package gets decided first. `conflict_counts` tracks a package's own discarded decisions and
`culprit_counts` the ones it caused elsewhere, for a provider that wants to move either kind to the
front.

To use these counts, put the integer returned by `nab_resolver.priority.compute_tier` first in your
priority key. Lower tiers are tried first. The helper moves a package forward when its decisions are
repeatedly discarded, and moves it back when it is blamed much more often than any other package.
Your provider chooses how to break ties.

## Resolving

`solve` takes one range per required package and returns the pins, the dependency edges it crossed,
and the roots you named. Its second argument, `constraints`, narrows a package that something else
pulls in without requiring the package itself.

```python
graph: Graph = {
    "app": {1: {"http": Range.at_least(2), "json": Range.full()}},
    "http": {
        3: {"json": Range.at_least(2)},
        2: {"json": Range.less_than(2)},
    },
    "json": {2: {}, 1: {}},
}

solution = Resolver(NewestFirstProvider(graph)).solve({"app": Range.singleton(1)})

print("roots:", solution.roots)
for package, version in sorted(solution.pins.items()):
    print("pin:", package, version)
for parent, child in solution.edges:
    print("edge:", parent, "->", child)
```

```text
roots: ('app',)
pin: app 1
pin: http 3
pin: json 2
edge: app -> http
edge: app -> json
edge: http -> json
```

`resolve` is the same call, returning `solution.pins` alone.

`pins` holds only the packages reachable from the roots, so one decided on a branch the resolver
later abandoned is filtered out. Building it walks the graph back through your provider, so cache
`get_dependencies` by package and version.

### Candidates discovered during resolution

When reading metadata can reveal another candidate, pass `availability_generation` to `Resolver`.
The callback returns an integer that increases when the provider discovers new choices. The solver
retries a missing candidate after other packages make progress. Once all queries are exhausted, the
provider must be able to rule out that candidate whenever the same decisions and requirements hold.

## Reading a failure

The requirements below pin `http` to 2 and rule out the only `json` it can use. `ResolutionError`
carries the finished report as its message.

```python
try:
    Resolver(NewestFirstProvider(graph)).resolve(
        {"http": Range.singleton(2), "json": Range.at_least(2)}
    )
except ResolutionError as error:
    print(error)
```

```text
because http 2 depends on json (-inf, 2)
because your project depends on json [2, +inf)
so http 2
because your project depends on http 2
so your project's requirements cannot be satisfied
```

A `so` line is the clause the lines above it prove, and what it names cannot hold: `so http 2` rules
that version out rather than choosing it.

That report has already been through the provider's `narrow_for_display` and the resolver's
`format_range`, so re-rendering from `error.incompatibility` means supplying both again. Walk its
`cause_left` and `cause_right` when you want the proof as a tree rather than as text.

Catch `ResolutionTerminatedError` before `ResolutionError` to handle limits and resolver faults
separately from conflicts. `ResolutionLimitError` reports an exhausted work budget;
`ResolutionInvariantError` reports inconsistent provider or solver state, including a
`ResolutionStalledError` when a conflict loop stops making progress. A stalled error can carry a
diagnostic `incompatibility`, and these errors do not prove the requirements unsatisfiable.

## Bringing your own range type

`Range` orders whatever it is handed, so integers suit a toy graph and `packaging.version.Version`
suits a real Python one.

To use your own range type, pass it to `Resolver` as `range_type`. It must implement
`RangeProtocol`. Pass a `root_version` that `range_type.singleton()` accepts for the virtual root.
If the type's string representation is hard to read in a constraint, also pass `format_range`. Nab
uses these arguments for PEP 440 version ranges.

Use `left.intersect(right)` or `left.union(right)` to combine terms for the same package. Both raise
`ValueError` if the packages differ. Intersection returns a term even when its range is empty. Union
returns `None` if every possible selection satisfies the combined term.

(resolve-a-prepared-catalogue)=

## Resolve Python package files

Install `nab-candidates` if your installer already finds wheels and source distributions. Each file
is a candidate for installation. Give files with different dependencies distinct candidate
identities, even when they have the same package name and version.

1. Subclass `PreparedVersionCatalogue` from `nab_candidates.versions`. Set `range_type` to
   `packaging.ranges.VersionRange`. Implement `base_name()` and `pin_project()` so package names and
   exact pins refer to the same project. Use `ordered(package, allowed)` to return matching
   candidates in preference order.
2. Implement `read_choices(package)` to list candidates for your target and
   `read_candidate(candidate)` to read one candidate's dependency ranges and exact pins. Keep the
   original pins. A range alone cannot tell you whether the user wrote an exact pin.
3. Pass your catalogue, root requirement ranges, original input pins and constraints to
   `nab_candidates.resolution.PreparedResolution.from_catalogue()`.
4. Call `solve()` for the selected candidates, roots, dependency edges and the pin that permitted
   each selected yanked file. Call `select()` if you only need the mapping from package names to
   candidates. Nab uses `select()` when checking yanked files and records the choices in its lock.

Keep candidate identities and listings stable for one resolution. You can read and cache more
metadata as the search progresses. If a new source changes which files are available, start a new
resolution with a fresh catalogue.

(reuse-scoped-artifact-preference)=

### Check yanked files in your own search

Use the lower-level `nab_candidates.preference.PreferenceSearch` if you already run the resolver
yourself and need to check yanked files. For example, a selected version of `app` might require
exactly `lib==2.0`. If the file for that version of `lib` is yanked, the search can report `app`'s
dependency as the reason it was allowed.

Implement `PreferenceCatalogue` to expose your candidates, their known dependencies, version ranges
and original pins. Subclass `PreferenceSearch` to provide the catalogue. Call `run()` with a
callback that resolves one `PreferenceScope`: the scope specifies which earlier choices must stay
fixed and which packages need a non-yanked file. The searches share one work limit.

Before reading a yanked file's metadata, call `check()` with the candidate, current selection and
scope. It can allow the read, rule out that selection, or report that the available metadata is
insufficient. Record missing listings in `missing_listings` before returning from the listing query.
Keep previously cached facts unchanged. With `refresh_incomplete=True`, adding facts allows the
search to revisit an inconclusive result. After a successful search, `explain()` returns the source
of the pin for each selected yanked file.

(the-supported-api)=

## Supported nab-resolver imports

Import these names from the modules listed below. Import paths and APIs can change between 0.0.x
releases, so pin the version you tested.

| Module                     | Supported names                                                                                                              |
| -------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `nab_resolver.resolver`    | `Resolver`, `ResolverProvider`, `BaseProvider`, `ResolverObserver`, `Solution`, `DEFAULT_MAX_ITERATIONS`                     |
| `nab_resolver.root`        | `ROOT`                                                                                                                       |
| `nab_resolver.priority`    | `compute_tier`                                                                                                               |
| `nab_resolver.ranges`      | `Range`, `IntegerRangeMembership`                                                                                            |
| `nab_resolver.types`       | `RootRequirement`, `Term`, `Incompatibility`, `IncompatibilityCause`, `RangeProtocol`                                        |
| `nab_resolver.errors`      | `ResolutionError`, `ResolutionTerminatedError`, `ResolutionLimitError`, `ResolutionInvariantError`, `ResolutionStalledError` |
| `nab_resolver.diagnostics` | `DiagnosticMessage`, `Suggestion`, `error_message`, `suggestion_lines`                                                       |
| `nab_resolver.proofs`      | `DependencyCause`, `dependency_terms`, `external_clauses`, `select_dependency_causes`                                        |
| `nab_resolver.result`      | `IncompleteSelectionError`, `walk_selected`                                                                                  |
| `nab_resolver.testing`     | `ProviderScenario`, `ProviderConformanceError`, `check_provider`, `check_provider_scenarios`                                 |

## Checking a provider

`check_provider_scenarios` runs reusable checks with a factory that builds your provider from each
scenario's graph and requirements:

```python
from nab_resolver.testing import check_provider_scenarios

check_provider_scenarios(
    lambda scenario: NewestFirstProvider(scenario.graph),
    packages=("a", "b", "c"),
    versions=(1, 2),
    range_type=Range,
)
```

The suite checks conflicts, cycles, constraints and backtracking. It repeats metadata queries,
changes root order and tries every combination in each small fixture to check both solutions and
failures.

Supply three distinct package keys, two distinct version keys and your range class. `check_provider`
accepts custom fixtures and an explicit search budget. List every available version and its
dependencies in the fixture. Those dependencies must stay the same throughout the check. If the
provider gives a different answer, `ProviderConformanceError` names the scenario and explains what
failed. Read its `scenario` attribute to identify the failing fixture.

Use `probe_state` to return an independent snapshot of decision state around
`has_satisfying_version`. Exclude caches and diagnostics from that snapshot.
