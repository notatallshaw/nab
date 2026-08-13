# The five distributions

`nab` is one command built out of five distributions. Installing `nab` pulls
all of them, so nothing here changes how the CLI is used. It matters when you
embed part of nab in something else, and it explains why a bug report about
markers goes to a different place from one about HTTP.

## What each one is

`nab-resolver` is the solver. A generic PubGrub implementation over abstract
versions and ranges: it knows about terms, incompatibilities, decisions and
backtracking, and nothing about Python packaging. It does not import
`packaging` and has no idea what a wheel is.

`nab-provider` is the resolution logic. The provider the solver asks for
candidates, the target and tag model, marker evaluation, extras expansion, the
metadata parser, the policies (`BuildPolicy`, `DistPolicy`, VCS admission)
with their per-package and per-index overrides, and the store the answers land
in. It does no IO at all: no socket, no filesystem, no subprocess, no clock.
Everything it needs from the world arrives through one interface,
`nab_provider.fetch_port.FetchPort`, which whoever is driving it implements.

`nab-index` is the index client. The Simple API reader, the on-disk HTTP
cache, the lazy-wheel range reader, the archive and VCS fetchers, the local
`file://` index. It depends on `nab-provider` for the record types a listing
is made of, and on nothing else of nab's.

`nab-python` is nab's own host. It implements `FetchPort` over `nab-index` in
`nab_python.fetch.FetchCoordinator`, and adds everything a command-line
resolver needs beyond resolution: the `[tool.nab]` config ladder, workspace
discovery, the PEP 517 build path, the lockfile writer and the downloader.

`nab` is the CLI.

## How they depend on each other

```text
nab-resolver   ->  (nothing outside the standard library)
nab-provider   ->  nab-resolver
nab-index      ->  nab-provider
nab-python     ->  nab-provider, nab-index, nab-resolver
nab            ->  nab-provider, nab-index, nab-python, nab-resolver
```

The one edge worth explaining is `nab-index -> nab-provider`. It looks
backwards: the index client is the lower layer, so why does it depend on the
resolution logic? Because the records both sides agree on, `WheelFile`,
`SdistFile`, `IndexConfig` and the fetch errors, are the vocabulary of the
conversation rather than either party's private types. They live with the side
that must never fetch, and the side that fetches imports them. Pointing the
edge the other way would put the HTTP client in the import graph of anything
that resolves, which is exactly what the split exists to prevent.

## Why the provider is separate

Two reasons, and only one of them is about embedding.

The first is that resolution logic with no HTTP client in its import graph can
be tested and reasoned about as a unit. `nab-provider`'s own suite runs with
`nab-resolver` installed and nothing else, and CI runs it that way in its own
workspace, so the claim is checked rather than asserted.

The second is embedding. A host that already owns a package finder, a session
and a download path can take `nab-provider` and `nab-resolver`, implement
`FetchPort` over what it already has, and get nab's resolution without nab's
networking, caching, config format or lock output. That is a real target:
pip has all of those already and must keep owning them.

## Driving the provider on its own

`nab_provider.testing` ships the reference port, a `FetchPort` over a store
you fill yourself. Nothing in this reaches the network:

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

What the provider cannot do is fetch anything, read or write any file
including `pyproject.toml`, discover a workspace, build an sdist, or write a
lock. It has no entry point that takes a path and no `resolve()` of its own:
that is `nab_python.resolve`, which is a host. `nab-provider` is a library
with two intended consumers, `nab-python` and an embedding host, and it should
not pretend to be an application.

## Where the vendored packaging fork lives

`nab-provider` carries a vendored fork of `packaging` at
`nab_provider._vendor.packaging`, holding the `VersionRange` work that has not
reached a release yet. There is exactly one copy, and `nab-python` reaches
into it rather than carrying its own: `nab_python.config` builds `Version`,
`Requirement` and `VersionRange` objects that `nab_provider.provider`
consumes, and two copies would be two distinct classes that `isinstance` and
dict keying would silently disagree about. `tasks/check_boundaries.py`
otherwise forbids one package reaching into another's `_vendor`, and carries
that single allowance by name. It goes away when the fork's changes land
upstream and nab depends on a released `packaging` again.
