# Embed the resolver in your own tool

`nab-resolver` is the PubGrub core nab drives, published on its own and
with no dependencies. It knows nothing about Python packaging: package
identity, version ordering and dependency metadata all come from you,
through a provider.

This page builds a provider over an in-memory graph, resolves with it,
and reads the failure report.

Install it with `pip install nab-resolver`.

## What you supply

A provider answers eleven methods. `BaseProvider` supplies six of them,
which leaves five to write:

`choose_version`
: Pick a version of a package inside a range, or return `None` when none
  fits.

`has_satisfying_version`
: Answer whether `choose_version` would pick a version in the range you
  are handed. It attributes failures, so restore decision-affecting
  state; diagnostic-only evidence may remain.

`get_dependencies`
: Report what a version depends on, as a range per dependency.

`prioritize`
: Return a sort key deciding which undecided package to take next. Lower
  goes first.

`widen_decision`
: Return a range that may replace a decided version in clauses,
  or `None` for the version alone. `Range.full()` is unsound when two
  versions differ: it assigns one version's dependencies to every
  version and can make a solvable graph fail. A widening provider also
  overrides `narrow_for_display`; otherwise reports name widened ranges.

`ResolverProvider` in `nab_resolver.resolver` documents all eleven, and
its docstrings are the full contract. Subclassing is optional: the
resolver accepts any object satisfying that protocol.

## A provider over an in-memory graph

Packages are strings and versions are integers here. A package can be
any hashable value, a version any type its range type orders.

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

Counting the versions still in range is PubGrub's usual `prioritize`
heuristic: the most constrained package gets decided first.
`conflict_counts` tracks a package's own discarded decisions and
`culprit_counts` the ones it caused elsewhere, for a provider that wants
to move either kind to the front.

## Resolving

`solve` takes one range per required package and returns the pins, the
dependency edges it crossed, and the roots you named. Its second
argument, `constraints`, narrows a package that something else pulls in
without requiring the package itself.

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

`pins` holds only the packages reachable from the roots, so one decided
on a branch the resolver later abandoned is filtered out. Building it
walks the graph back through your provider, so cache `get_dependencies`
by package and version.

## Reading a failure

The requirements below pin `http` to 2 and rule out the only `json` it
can use. `ResolutionError` carries the finished report as its message.

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

A `so` line is the clause the lines above it prove, and what it names
cannot hold: `so http 2` rules that version out rather than choosing it.

That report has already been through the provider's `narrow_for_display`
and the resolver's `format_range`, so re-rendering from
`error.incompatibility` means supplying both again. Walk its
`cause_left` and `cause_right` when you want the proof as a tree rather
than as text.

Two failures arrive without a report: passing the resolver's
`max_iterations`, which attaches no `incompatibility`, and a
conflict-resolution loop that stops making progress, which attaches one
but reports a resolver bug. Neither means the requirements are
unsatisfiable.

## Bringing your own range type

`Range` orders whatever it is handed, so integers suit a toy graph and
`packaging.version.Version` suits a real Python one.

A host with its own range algebra replaces `Range` outright: pass
`Resolver` a `range_type` satisfying `RangeProtocol`, a `root_version`
to decide the virtual root at, which `range_type.singleton()` has to
accept, and a `format_range` unless that type's `str` already reads as a
constraint. nab drives the resolver this way, with a PEP 440 range type.

## Candidates discovered while resolving

When candidate availability depends on selected packages, pass `Resolver` an `availability_generation` callback. It takes no arguments and returns an integer that increases whenever provider operations can change a candidate query's answer. Reading it must not change availability.

An unsuccessful query is deferred while other packages can still be decided. After a stable pass through the remaining packages, the resolver records absence guarded by the current decisions. The provider must guarantee that absence whenever those decisions and requirements hold, even after its caches gain data. A source discovered through one parent must therefore remain eligible only while its declaring requirement is active; a growing cache alone does not satisfy this contract.

This mode is synchronous: availability cannot change between the final generation check and recording the clause. Omit the callback for a provider with a fixed candidate universe.

## Reusing a host's prepared candidates

`CandidateProvider` in `nab_resolver.candidate_provider` adapts a `CandidateHost` that supplies `iter_candidates`, `get_dependencies` and `priority`. Construct it with the host and a sequence of `CandidateRequirement` roots, then pass `provider.root_requirements()` to `Resolver.solve`.

The host yields `PreparedCandidate` objects in its preferred order. Each key must identify stable dependency metadata for that package, including distinctions such as source or build options. The key must be hashable and accepted by your range type. Retrieve the selected host object with `provider.prepared(package, key).origin`.

Host methods receive a read-only mapping of active requirements. It contains roots and dependencies from the last decision snapshot supplied before candidate selection; `priority` can therefore see an earlier snapshot. Original host objects remain available through each requirement's `origin`.

Keep requirement fields (`package`, `constraint`, `origin`) and prepared candidate fields (`key`, `origin`) fixed during a resolve. Changes inside host objects may populate caches but must preserve requirement meaning and candidate metadata. Dependency collection stops when a merged restriction becomes empty; `causes_for(package, key)` returns the declarations consumed for that candidate.

Candidate queries also serve diagnostic probes, so preparing or caching a candidate must preserve answers for the same active requirements. For conditional availability, use the callback and eligibility contract above.

## The supported API

These module paths will not move without a major version bump:

```text
nab_resolver.candidate_provider
                       CandidateHost, CandidateProvider,
                       CandidateRequirement, PreparedCandidate
nab_resolver.errors     ResolutionError
nab_resolver.ranges     Range
nab_resolver.resolver   BaseProvider, DEFAULT_MAX_ITERATIONS, Resolver,
                        ResolverObserver, ResolverProvider, Solution
nab_resolver.root       ROOT
nab_resolver.types      Incompatibility, IncompatibilityCause,
                        RangeProtocol, RootRequirement, Term
```

The package root binds no names, so importing `nab_resolver` pulls in no
submodules. Anything else is internal and may move in any release.
