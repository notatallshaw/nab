# The six distributions

Installing `nab` installs all six distributions. This is how a command reaches them:

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

`nab_provider.fetch_port.FetchPort` is the interface the provider uses to ask for package files and
metadata. Nab supplies a `FetchCoordinator` that handles those requests using `nab-index`.

The CLI loads configuration and writes output; `nab-project` orchestrates resolution and builds
lockfile data. Library APIs, including import paths, can change between 0.0.x releases. When
embedding a component, pin the version you tested.

## Package responsibilities

| Distribution     | Owns                                                                                                         |
| ---------------- | ------------------------------------------------------------------------------------------------------------ |
| `nab-resolver`   | Generic PubGrub solving over abstract versions and ranges; standard library only                             |
| `nab-markersets` | Marker parsing, algebra and bounded conversion reuse; callers own reuse lifetimes                            |
| `nab-provider`   | Candidate/dependency selection, targets, tags, extras, metadata parsing, policies and result storage; no I/O |
| `nab-index`      | Simple API, HTTP cache, wheel range reads, archive/VCS fetches and local indexes                             |
| `nab-project`    | Fetch coordination, workspaces, PEP 517 builds, locks, downloads and resolve orchestration                   |
| `nab`            | Configuration, CLI commands and output                                                                       |

An installer that already finds files and prepares metadata can supply its candidates to
`nab_provider.catalogue`. The catalogue distinguishes files with equal versions, checks when an
exact pin permits a yanked file, and returns the selected candidates and their dependency edges. The
installer supplies file order and keeps the metadata it reads for one resolution.

`nab-project` reads project files and PEP 723 script metadata into the declaration view used by
resolution and saved-lock validation. The CLI selects the input format and applies configuration
layers, including a script's inline `[tool.nab]`. Script parsing never imports the script, and
selecting a script does not discover an enclosing workspace.

Dependency input imports also belong to `nab-project`: it reads selected project, group, extra and
static build declarations from local trees or pinned source archives. `nab-index` reads archive
members without extracting a tree; `nab-provider` expands references between the separately
installed projects and carries their extra conditions. Imported project configuration and build
backends are never executed.

The CLI compiles lock plan configuration into project declarations. `nab-project` owns the prepared
declaration view and checks its original inputs during resolution and saved-lock validation.
`nab-index` stores the declaration files read from source archives so cache maintenance and offline
reads use the same record format. The project’s selection planner checks conflicts and combines the
selected roots for one installation. The provider binds selected extra markers while preserving
their environment conditions.

`nab-project` also emits its generated TOML. Its writer uses a fixed layout for locks and synthetic
build projects; it does not preserve source formatting or comments. Datetime values use a `T`
separator.

### Matrix ownership

```python
from nab_provider.tags import PlatformSpec
from nab_provider.target import Matrix, MatrixInclude

matrix = Matrix(
    python=">=3.10,<3.16",
    platforms=(PlatformSpec("linux_x86_64"),),
    include=(MatrixInclude(python="==3.15", free_threaded=True),),
)
targets = matrix.expand()
```

The provider expands and deduplicates targets and validates interpreter/ABI combinations. The CLI
parses and renders the declarations. The project layer checks project metadata and whether the
results can share a lock.

## Dependencies between packages

```text
nab-resolver   ->  standard library
nab-markersets ->  nab-resolver; packaging, or nab-provider, by extra
nab-provider   ->  nab-markersets, nab-resolver
nab-index      ->  nab-provider, nab-resolver, packaging
nab-project    ->  nab-markersets, nab-provider, nab-index, nab-resolver, packaging
nab            ->  nab-markersets, nab-provider, nab-index, nab-project, nab-resolver
```

This shows only internal dependencies and `packaging`, which exists in two copies. `nab-index` uses
the provider's file records, index configuration and fetch errors. Its Simple API client uses
`nab_resolver.diagnostics` to explain malformed index responses. `nab_provider.value` supplies
shared record behavior; concrete record types belong to their domain package. Fixed fields use
`Final` annotations without runtime write guards.

The resolver counts discarded decisions for each package and records which earlier packages it
blames for them. The Python provider uses `nab_resolver.priority.compute_tier` to rank packages by
those counts. Within a tier it considers the number of candidates, then tries an extra before its
base package if the counts are equal.

## Diagnostic suggestions

```text
owning package -> DiagnosticMessage / Suggestion
              -> ResolutionMessage
              -> CLI output
```

`nab-resolver` supplies message types; `nab-provider` adds package evidence and terminal-error
rendering. Policy decisions stay with their owning packages. Candidate rejections retain suggestions
for terminal-error reporting.

The CLI owns verbosity and output lifetime. Each command prints an identical suggestion once while
retaining every cause. Library hosts can use their own emission state or render errors
independently.

## Prefetch windows

```python
from nab_project.resolve import resolve_for_targets
from nab_provider.fetch_port import PrefetchSettings

settings = PrefetchSettings(metadata_versions=0, lookahead_versions=64)
result = resolve_for_targets(
    project_path, transport, targets=targets, prefetch_settings=settings
)
```

`metadata_versions` sets how many of the newest versions to prefetch when an index listing arrives.
`lookahead_versions` sets how many extra versions to prefetch before scanning candidates. Set either
to zero to disable that prefetch. Nab still fetches metadata it needs to decide whether a candidate
can be used. An omitted setting uses the transport's recommendation if it has one.

The fetch coordinator can use four optional interfaces to decide what to prefetch.

- `MetadataPrefetchHints` limits requests for root packages to versions allowed by their root
  requirements.
- `MetadataArtifactHints` chooses a wheel within each release using the provider's wheel ranking for
  the target.
- `MetadataPrefetchAdmission` skips a release when every candidate wheel for that request rules out
  the target Python. Missing or malformed Python bounds allow the request. A skipped release leaves
  its place in the window empty. See
  [how Nab compares Python versions](../reference/target-environment.md).
- `MetadataPrefetchDeferral` lets Nab wait for a dependency's version range before prefetching a
  prerelease. `first_matching_version` then applies the same selection rules as a required request,
  including explicit prerelease requests and prerelease-only fallback. `prefetch_in_range` requests
  metadata for that candidate. Later constraints can trigger ordinary fetches and lookahead.

Custom fetch coordinators can implement just the interfaces they use. The last two optimizations
apply to the default prefetch window and the built-in provider. Package overrides disable the
Python-bound check. A version preference also postpones default prefetch for a transitive
dependency, except when overrides or lowest-version ordering apply.

## Cache-only fetches

```text
cache hit  -> validate digest and freshness -> provider
cache miss -> start worker -> index client
```

With one unrouted disk cache, `nab-project` checks fresh parsed listings and metadata before
starting its fetcher thread. `nab-index` owns those checks and `IdleClosableTransport`, which lets
an unused transport close without an event loop. Other transports keep the normal worker lifecycle.

Use the coordinator's `request_listing`, `request_metadata` and artifact request methods to submit
work. These methods register the waiting caller before the fetch starts. At shutdown, the index
clients finish their cache writes and the coordinator closes their shared transport.

Direct coordinator callers opt in with `start_on_demand=True`; explicit `start()` still waits for a
ready worker.

## Shared candidate preparation

```text
listing -> policy/Python filters -> tag groups
                                       |
target tags -> admission answers -> candidate view -> resolver
```

The provider shares a candidate view only when all tag groups have identical admission answers. Each
target retains wheel ranking, metadata validation and marker evaluation. Views hold no learned
clauses or pending rejections, and their cache lasts one project resolve.

## Previous-lock preferences

`nab-project` projects a saved PEP 751 lock onto each target's combined extras and groups.
`LockPreferences` supplies versions to `nab-provider`, which checks live candidates, metadata and
artifacts. The CLI exposes this policy through
[`--lock-policy prefer-existing`](../reference/cli.md#--lock-policy-resolveprefer-existingreuse).

With `prefer-existing`, the project:

1. Tries the saved dependency graph against current package information.
2. Discards the trial provider and solver if the graph changes or cannot be reused.
3. Starts fresh solving with the saved versions as preferences.

Old dependency declarations and artifact records are not reused.

`LockPolicy.REUSE` checks the saved solution before live resolution. Unsupported mutable or file
sources use live resolution. An accepted lock stays unchanged and no index transport is created;
remote freshness is not checked.

The CLI selects the policy and reports whether the saved lock was reused.

## Driving the provider without networking

```python
from nab_provider.provider import Provider
from nab_provider.ranges import VersionRange
from nab_provider.testing import make_coordinator, make_metadata, make_wheel
from nab_resolver.resolver import Resolver

coordinator = make_coordinator(
    listings={
        "app": [make_wheel("1.0", package="app")],
        "lib": [make_wheel("2.0", package="lib")],
    },
    metadata_by_version={
        "1.0": make_metadata("app", "1.0", "lib"),
        "2.0": make_metadata("lib", "2.0"),
    },
)
provider = Provider(coordinator)
resolver = Resolver(
    provider,
    range_type=VersionRange,
    root_version="0",
    format_range=provider.format_range,
)
print(resolver.resolve({"app": VersionRange.full()}))
```

Here `make_coordinator` supplies an in-memory implementation of `FetchPort`. To use your own HTTP
session and downloader, provide an implementation of that interface. The fetcher stores queued
results in the shared index. `materialize_source(request)` returns the source tree and its metadata
directly. `build_sdist_metadata(...)` returns the built metadata. The provider caches it by sdist
version and URL and checks it for each target. Use `nab_project.resolve.resolve_for_targets` when
resolving a project.

### Collect active dependency declarations

```python
result = resolve_for_targets(
    project_path, transport, targets=targets, include_dependency_requirements=True
)
result.raise_for_failure()
for target_result in result.target_results:
    requirements = target_result.dependency_requirements()
```

Read `requirements[parent][dependency]` to get the declarations for that dependency. Both keys are
canonical package names. An empty graph returns `{}`. If the target failed, the method raises its
resolution error. If declarations were never collected, resolve with
`include_dependency_requirements=True`. Root requirements and user constraints are separate.

For a single target, `target_result.require_lock()` returns its lock or raises its resolution error.
Use the optional `lock` attribute when collecting just the successful targets.

| Record field                     | Contains                                                                |
| -------------------------------- | ----------------------------------------------------------------------- |
| `requirement_text`               | Full declaration with normalized formatting and markers                 |
| `dependency_specifier`           | Parent's version restriction, preserving `~=`; empty means unrestricted |
| `parent_name`, `dependency_name` | Canonical package names                                                 |
| `dependency_extras`              | Requested dependency features, such as `socks` in `requests[socks]`     |
| `requirement_condition`          | Condition text, or `None`                                               |
| `required_for_parent_extras`     | Parent extras activating the requirement                                |
| `required_without_parent_extras` | Whether the requirement applies without a parent extra                  |

Declarations reflect the target, activated extras and metadata overrides. The underlying
`lock.dependency_requirements` is `None` when collection is disabled.

## Python 3.15 imports

```python
__lazy_modules__ = ("nab_provider.target",)

from nab_provider.target import Matrix
```

Nab uses [explicit lazy-import declarations](https://peps.python.org/pep-0810/#semantics) on Python
3.15, including direct library imports. Earlier Python versions import eagerly; third-party modules
retain their own behavior.

## Packaging

Nab uses `packaging` 26.4 or newer for versions, requirements, tags and markers.
`MarkerSet.from_marker` accepts marker text or a `packaging.markers.Marker`.

The provider owns dependency classification by environment and extra; the project reuses it to check
saved pins. CI tests the provider with only its declared internal dependencies installed.
