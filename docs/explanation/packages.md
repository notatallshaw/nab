# The six distributions

`nab` is one command built out of six distributions. Installing `nab` pulls
all of them, so the split matters only when you embed part of nab elsewhere.

## What each one is

`nab-resolver` is the solver: a generic PubGrub implementation over abstract
versions and ranges, with no knowledge of Python packaging. It does not import
`packaging`.

`nab-markersets` is the marker algebra: a PEP 508 marker read as the set of
environments it selects, so markers can be intersected, complemented and
compared for emptiness, subset and equivalence. Its parser and evaluator come
from released `packaging`, not from the fork `nab-provider` vendors.

`nab-provider` is the resolution logic: the provider the solver asks for
candidates, the target and tag model, marker evaluation, extras expansion, the
metadata parser, the policies (`BuildPolicy`, `DistPolicy`, VCS admission) with
their per-package and per-index overrides, and the store the answers land in.
It does no I/O: everything it needs arrives through
`nab_provider.fetch_port.FetchPort`, which its host implements.

`nab-index` is the index client: the Simple API reader, the on-disk HTTP
cache, the lazy-wheel range reader, the archive and VCS fetchers, and the local
`file://` index.

`nab-project` is nab's own host: it implements `FetchPort` over `nab-index` in
`nab_project.fetch.FetchCoordinator`, and adds workspace discovery, the PEP 517
build path, the lockfile writer, the downloader and the resolve orchestration.
It resolves against a list of targets and a `nab_project.inputs.ResolveInputs`,
both of which its host supplies.

`nab` is the CLI, and it owns the `[tool.nab]` config ladder in `nab.config`:
reading the option a project declares, and turning it into the targets and
inputs nab-project resolves under.

## How they depend on each other

```text
nab-resolver   ->  (nothing outside the standard library)
nab-markersets ->  packaging
nab-provider   ->  nab-markersets, nab-resolver, packaging
nab-index      ->  nab-provider, packaging
nab-project    ->  nab-markersets, nab-provider, nab-index, nab-resolver,
                   packaging
nab            ->  nab-markersets, nab-provider, nab-index, nab-project,
                   nab-resolver
```

Of the third-party dependencies only `packaging` is shown, since it is the one
that exists here in two copies. Each manifest lists the rest.

`nab-index` depends on `nab-provider` because the records `WheelFile`,
`SdistFile`, `IndexConfig` and the fetch errors live with the side that must
never fetch.

## Why the provider is separate

Resolution logic with no HTTP client in its import graph can be tested on its
own: CI runs `nab-provider/tests` in a workspace that installs only
`nab-provider`, `nab-markersets` and `nab-resolver`.

A host that already owns a session and a download path can implement
`FetchPort` and get nab's resolution without its networking, caching or lock
output.

## Driving the provider on its own

`nab_provider.testing` ships a `FetchPort` over a store you fill yourself, so
nothing below reaches the network:

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

`nab_project.resolve.resolve_for_targets` is the entry point that takes a
project path; the provider has none.

## Where the vendored packaging fork lives

`nab-provider` carries nab's fork of `packaging` at
`nab_provider._vendor.packaging`, and `nab-project` and `nab.config` reach into
it rather than carrying their own copy: both build `Version`, `Requirement` and
`VersionRange` objects the provider consumes, and two copies would be two
distinct classes that `isinstance` and dict keying disagree about.
`tasks/check_boundaries.py` forbids every other reach into another package's
`_vendor`, and lists these two in `VENDOR_ALLOWANCES`.

`nab-markersets` is outside that: it takes released `packaging` and never reads
the fork. Both copies already ran in one process, since `nab-index` reads
`packaging.utils` and `packaging.version`; what is new is that marker handling
runs on the released copy too. Nothing bridges the two. A marker crosses as
`str(marker)`, and the exceptions the algebra raises are released `packaging`'s.
