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

The CLI loads configuration and writes output; `nab-project` orchestrates resolution and builds
lockfile data. Except for `nab-resolver`'s documented API, library APIs are experimental: pin exact
versions when embedding them.

## Package responsibilities

| Distribution     | Owns                                                                                                         |
| ---------------- | ------------------------------------------------------------------------------------------------------------ |
| `nab-resolver`   | Generic PubGrub solving over abstract versions and ranges; standard library only                             |
| `nab-markersets` | Marker parsing, algebra and bounded conversion reuse; callers own reuse lifetimes                            |
| `nab-provider`   | Candidate/dependency selection, targets, tags, extras, metadata parsing, policies and result storage; no I/O |
| `nab-index`      | Simple API, HTTP cache, wheel range reads, archive/VCS fetches and local indexes                             |
| `nab-project`    | Fetch coordination, workspaces, PEP 517 builds, locks, downloads and resolve orchestration                   |
| `nab`            | Configuration, CLI commands and output                                                                       |

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
nab-index      ->  nab-provider, packaging
nab-project    ->  nab-markersets, nab-provider, nab-index, nab-resolver, packaging
nab            ->  nab-markersets, nab-provider, nab-index, nab-project, nab-resolver
```

This shows only internal dependencies and `packaging`, which exists in two copies. `nab-index` uses
the provider's file records, index configuration and fetch errors. `nab_provider.value` supplies
shared record behavior; concrete record types belong to their domain package. Fixed fields use
`Final` annotations without runtime write guards.

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

`metadata_versions` controls newest versions fetched after a listing; `lookahead_versions` controls
extra versions fetched before a pipelined scan. Zero disables extra prefetch, leaving candidate
checks active. Omitted fields inherit transport recommendations, or defaults for custom transports
without them. The index recommends windows, the project snapshots settings, and the provider owns
lookahead.

## Cache-only fetches

```text
cache hit  -> validate digest and freshness -> provider
cache miss -> start worker -> index client
```

With one unrouted disk cache, `nab-project` checks fresh parsed listings and metadata before
starting its fetcher thread. `nab-index` owns those checks and `IdleClosableTransport`, which lets
an unused transport close without an event loop. Other transports keep the normal worker lifecycle.

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

## Driving the provider without networking

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

`nab_provider.testing` supplies the in-memory `FetchPort`. A real host implements that interface
over its own session and download path. The provider has no project path; use
`nab_project.resolve.resolve_for_targets` when resolving a project.

### Collect active dependency declarations

```python
result = resolve_for_targets(
    project_path, transport, targets=targets, include_dependency_requirements=True
)
result.raise_for_failure()
for target_result in result.target_results:
    requirements = target_result.dependency_requirements()
```

The map is keyed by canonical parent and dependency names. It raises for a failed target or omitted
collection; an empty graph returns `{}`. Root requirements and user constraints are separate.

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

## The vendored packaging fork

```text
nab.config, nab-project --> nab-provider._vendor.packaging
nab-markersets          --> vendored packaging, then released packaging
```

The provider carries the fork. The CLI and project use its `Version`, `Requirement` and
`VersionRange` classes; duplicating them would break type checks and dictionary keys.
`tasks/check_boundaries.py` enforces these exceptions in `VENDOR_ALLOWANCES`.

`nab-markersets` prefers the fork, then released `packaging` 26.3 or newer. Standalone users install
`nab-markersets[packaging]`; `nab-markersets[nab-vendored-packaging]` installs the fork. The index
and project also use released `packaging` for other APIs, but markers do not cross between copies.

The provider owns dependency classification by environment and extra; the project reuses it to check
saved pins. CI tests the provider with only its declared internal dependencies installed.
