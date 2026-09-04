# The six distributions

`nab` is one command built out of six distributions. Installing `nab`
pulls all of them, so the split matters only when you embed part of nab
elsewhere.

Except for `nab-resolver`'s documented surface, these APIs are
experimental. Pin exact versions when embedding them.

## What each one is

`nab-resolver` is the solver: a generic PubGrub implementation over
abstract versions and ranges, with no knowledge of Python packaging. It
does not import `packaging`.

`nab-markersets` is the marker algebra: a PEP 508 marker read as the set
of environments it selects. Its parser and evaluator come from
whichever `packaging` is installed, released or nab's fork. [Reasoning
about markers](../how-to/reason-about-markers.md) works through it.

`nab-provider` is the resolution logic: the provider the solver asks for
candidates, the target and tag model, marker evaluation, extras
expansion, the metadata parser, packaging policies with their
per-package and per-index overrides, and the result store. It does no
I/O: everything arrives through `nab_provider.fetch_port.FetchPort`,
which its host implements.

`nab-index` is the index client: the Simple API reader, the on-disk HTTP
cache, the lazy-wheel range reader, the archive and VCS fetchers, and
the local `file://` index.

`nab-project` is nab's own host. It implements `FetchPort` over
`nab-index` in `nab_project.fetch.FetchCoordinator`, then adds workspace
discovery, the PEP 517 build path, the lockfile writer, the downloader,
and resolve orchestration. Its host supplies a target list and
`nab_project.inputs.ResolveInputs`.

`nab` is the CLI. It owns the `[tool.nab]` config ladder in
`nab.config`, reads the project options, and turns them into the targets
and inputs `nab-project` resolves under.

## How they depend on each other

```text
nab-resolver   ->  (nothing outside the standard library)
nab-markersets ->  packaging, or nab-provider, by extra
nab-provider   ->  nab-markersets, nab-resolver
nab-index      ->  nab-provider, packaging
nab-project    ->  nab-markersets, nab-provider, nab-index, nab-resolver,
                   packaging
nab            ->  nab-markersets, nab-provider, nab-index, nab-project,
                   nab-resolver
```

Only `packaging` is shown among the third-party dependencies because it
exists here in two copies.

`nab-index` depends on `nab-provider` because `WheelFile`, `SdistFile`,
`IndexConfig`, and the fetch errors live with the side that must never
fetch.

## Why the provider is separate

Resolution logic with no HTTP client in its import graph can be tested
alone. CI runs `nab-provider/tests` in a workspace that installs only
`nab-provider`, `nab-markersets`, and `nab-resolver`.

A host that owns a session and download path can implement `FetchPort`
and use nab's resolution without its networking, cache, or lock output.

## Driving the provider on its own

`nab_provider.testing` ships a `FetchPort` over a store you fill, so
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

`nab_project.resolve.resolve_for_targets` is the entry point that takes
a project path; the provider has none.

## Where the vendored packaging fork lives

`nab-provider` carries nab's fork of `packaging` at
`nab_provider._vendor.packaging`. `nab-project` and `nab.config` use it
to build `Version`, `Requirement`, and `VersionRange` objects consumed
by the provider. A second copy would produce distinct classes, breaking
`isinstance` checks and dictionary keys.

`tasks/check_boundaries.py` forbids other packages from reaching into a
package's `_vendor` tree and lists these two exceptions in
`VENDOR_ALLOWANCES`.

`nab_markersets._packaging` prefers the vendored tree, then released
`packaging`, and requires version 26.3 or newer. Inside nab the fork
wins, so provider markers and exceptions retain the classes the algebra
expects.

The `nab-markersets[nab-vendored-packaging]` extra installs the fork;
standalone users install `nab-markersets[packaging]`. `nab-index` and
`nab-project` still use released `packaging` for other APIs, but no
marker crosses between the two copies.
