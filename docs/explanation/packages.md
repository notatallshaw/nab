# The six distributions

`nab` is one command built out of six distributions. Installing `nab` pulls all of them, so the
split matters only when you embed part of nab elsewhere.

Except for `nab-resolver`'s documented API, these APIs are experimental. Pin exact versions when
embedding them.

## Following a command

The CLI loads configuration and calls `nab_project.resolve.resolve_for_targets`. During resolution,
the solver asks the provider for candidates and dependencies:

```text
nab CLI
   |
   v
nab-project: resolve_for_targets
   |
   v
nab-resolver <----> nab-provider ----> nab-markersets
                         |
                      FetchPort
                         |
                         v
             nab-project: FetchCoordinator
                         |
                         v
                     nab-index
```

`nab-project` turns the resulting pins into lockfile data; the CLI writes the requested output.

## Package responsibilities

### `nab-resolver`

`nab-resolver` is the solver: a generic PubGrub implementation over abstract versions and ranges,
with no knowledge of Python packaging. It does not import `packaging`.

### `nab-markersets`

`nab-markersets` is the marker algebra: a PEP 508 marker read as the set of environments it selects.
It owns parsing, algebra and bounded conversion reuse. Callers own the lifetime of that reuse. Its
parser and evaluator come from whichever `packaging` is installed, released or nab's fork.
[Reasoning about markers](../how-to/reason-about-markers.md) works through it.

### `nab-provider`

`nab-provider` is the resolution logic: the provider the solver asks for candidates, the target and
tag model, marker evaluation, extras expansion, the metadata parser, packaging policies with their
per-package and per-index overrides, and the result store. It does no I/O: package data arrives
through `nab_provider.fetch_port.FetchPort`, an interface implemented outside the provider.

### `nab-index`

`nab-index` is the index client: the Simple API reader, the on-disk HTTP cache, the lazy-wheel range
reader, the archive and VCS fetchers, and the local `file://` index.

### `nab-project`

`nab-project` supplies the I/O that `nab-provider` needs. The application supplying this I/O is
called the host. It implements `FetchPort` over `nab-index` in `nab_project.fetch.FetchCoordinator`,
then adds workspace discovery, the PEP 517 build path, the lockfile writer, the downloader, and
resolve orchestration. Its caller specifies the Python/platform environments to resolve for and
project settings in `nab_project.inputs.ResolveInputs`.

### `nab`

`nab` is the CLI. It combines configuration files, environment variables, and command-line flags in
`nab.config`, and turns them into the targets and inputs `nab-project` resolves under.

## How they depend on each other

```text
nab-resolver   ->  (nothing outside the standard library)
nab-markersets ->  nab-resolver; packaging, or nab-provider, by extra
nab-provider   ->  nab-markersets, nab-resolver
nab-index      ->  nab-provider, packaging
nab-project    ->  nab-markersets, nab-provider, nab-index, nab-resolver,
                   packaging
nab            ->  nab-markersets, nab-provider, nab-index, nab-project,
                   nab-resolver
```

Only `packaging` is shown among the third-party dependencies because it exists here in two copies.

`nab-index` depends on `nab-provider` for `WheelFile`, `SdistFile`, `IndexConfig` and fetch error
types. The provider owns these data contracts; the index client supplies their data.

The provider also owns `nab_provider.value`, shared record behavior used by the index, project and
CLI layers. Concrete records keep their typed constructors in the package owning their data; the
shared code handles comparison and copying. Fixed fields use `Final` annotations rather than runtime
write guards.

## Diagnostic suggestions

`nab_resolver.diagnostics` supplies shared messages and corrective actions without logging or
retaining command state. `nab_provider.diagnostics.Diagnostic` adds package summaries and evidence.
Package, index and build-policy decisions remain with the code that produces the diagnostic.

`nab-project` adds provider diagnostics to a terminal resolution error through
`nab_project.diagnostics.ResolutionMessage`. Candidate rejections retain data for that report; they
do not emit suggestions while the resolver tries alternatives.

The CLI owns verbosity, streams and the emission lifetime. Its printer shares one suggestion set
across logs and target failures, retaining every cause while printing an identical action once. Each
command starts with a fresh set. Library hosts can supply their own emission state or render each
error independently.

## Prefetch windows

HTTP backends recommend metadata prefetch windows. `nab-project` snapshots those recommendations
below explicit operation settings. `nab-provider` owns the candidate lookahead algorithm and its
fallback defaults; the index client does not make resolution decisions.

```python
from nab_project.resolve import resolve_for_targets
from nab_provider.fetch_port import PrefetchSettings

settings = PrefetchSettings(metadata_versions=0, lookahead_versions=64)
result = resolve_for_targets(
    project_path, transport, targets=targets, prefetch_settings=settings
)
```

`metadata_versions` controls the newest versions fetched after a listing. `lookahead_versions`
controls the extra version window fetched before a provider's pipelined scan. Zero disables that
extra prefetch; the pipelined candidate checks still run. Omitted fields inherit the selected
backend's recommendation. Custom transports without recommendations retain the existing defaults.

## Cache-only fetches

`nab-project` enables on-demand startup for its fetch coordinator. With a single unrouted disk
cache, it checks fresh parsed listings and metadata inline before creating a fetcher thread.

```text
cache hit -> validate digest and freshness -> provider
cache miss -> start worker -> normal index client
```

`nab-index` owns the cache checks and the optional `IdleClosableTransport` protocol. The coordinator
uses that protocol to close an unused transport without an event loop. Transports without it keep
the normal worker lifecycle. Direct coordinator callers opt in with `start_on_demand=True`; an
explicit `start()` still waits for a ready worker.

## Shared candidate preparation

`nab-provider` groups listing records by wheel tags after policy and Requires-Python filtering.
Targets reuse a candidate view only when every tag group has the same admission answer.

```text
listing -> policy/Python filters -> grouped records
                                        |
target tags -> admission answers -> candidate view -> target resolver
```

Each target keeps its own wheel ranking, metadata validation and dependency-marker evaluation.
Prepared views contain no learned clauses or pending rejections. Their cache lives for one project
resolve.

## Imports on Python 3.15

Nab modules use explicit
[`__lazy_modules__` declarations](https://peps.python.org/pep-0810/#semantics) to defer selected
dependencies until first use, including when you import a library directly. Third-party modules keep
their own import behavior. Earlier Python versions load imports eagerly.

## Why the provider is separate

Resolution logic with no HTTP client in its import graph can be tested alone. CI runs
`nab-provider/tests` in a workspace that installs only `nab-provider`, `nab-markersets`, and
`nab-resolver`.

A host that owns a session and download path can implement `FetchPort` and use nab's resolution
without its networking, cache, or lock output.

## Driving the provider on its own

`nab_provider.testing` ships a `FetchPort` over a store you fill, so nothing below reaches the
network:

```python
from nab_provider.provider import Provider
from nab_provider.records import WheelFile
from nab_provider.testing import make_coordinator
from nab_provider._vendor.packaging.ranges import VersionRange
from nab_resolver.resolver import Resolver


def wheel(name: str, version: str) -> WheelFile:
    return WheelFile(
        filename=f"{name}-{version}-py3-none-any.whl",
        url=f"https://example.invalid/{name}-{version}-py3-none-any.whl",
        version=version,
        requires_python=None,
        has_metadata=True,
        upload_time=None,
    )


port = make_coordinator(
    listings={"app": [wheel("app", "1.0")], "lib": [wheel("lib", "2.0")]},
    metadata_by_version={
        "1.0": "Metadata-Version: 2.1\nName: app\nVersion: 1.0\nRequires-Dist: lib\n",
        "2.0": "Metadata-Version: 2.1\nName: lib\nVersion: 2.0\n",
    },
)
provider = Provider(port)
resolver = Resolver(provider, range_type=VersionRange, root_version="0")
print(resolver.resolve({"app": VersionRange.full()}))
```

`nab_project.resolve.resolve_for_targets` is the entry point that takes a project path; the provider
has none.

Pass `include_dependency_requirements=True`, then call `target_result.dependency_requirements()`. It
returns a map keyed by parent and dependency, or raises for a failed target or omitted collection.
Use canonical package names. Each declaration record has:

- `requirement_text`: the full declaration with normalized formatting and markers.
- `dependency_specifier`: the parent's version restriction, preserving `~=`. Empty means
  unrestricted.
- `parent_name`: the package declaring the requirement.
- `dependency_name`: the package being required.
- `dependency_extras`: features requested on the dependency, such as `socks` in `requests[socks]`.
- `requirement_condition`: the requirement's condition as text, or `None` when there is no
  condition.
- `required_for_parent_extras`: selected features of the parent for which this requirement applies.
  `required_without_parent_extras` is true when the parent requires it without selecting an extra.

Declarations reflect the target, activated extras and metadata overrides. Root requirements and user
constraints are separate. An empty graph returns `{}`. The underlying `lock.dependency_requirements`
field is `None` without collection.

## Where the vendored packaging fork lives

```text
nab.config, nab-project --> nab-provider._vendor.packaging
nab-markersets          --> vendored packaging, then released packaging
```

`nab-provider` carries nab's fork of `packaging` at `nab_provider._vendor.packaging`. `nab-project`
and `nab.config` use it to build `Version`, `Requirement`, and `VersionRange` objects consumed by
the provider. A second copy would produce distinct classes, breaking `isinstance` checks and
dictionary keys.

`tasks/check_boundaries.py` forbids other packages from reaching into a package's `_vendor` tree and
lists these two exceptions in `VENDOR_ALLOWANCES`.

`nab_markersets._packaging` prefers the vendored tree, then released `packaging`, and requires
version 26.3 or newer. Inside nab the fork wins, so provider markers and exceptions retain the
classes the algebra expects.

The `nab-markersets[nab-vendored-packaging]` extra installs the fork; standalone users install
`nab-markersets[packaging]`. `nab-index` and `nab-project` still use released `packaging` for other
APIs, but no marker crosses between the two copies.
