# nab

nab is an experimental Python packaging lock and package download tool,
aiming to have similar resolver performance to uv, while being written
in Python.

nab reads a `pyproject.toml`, resolves the dependency tree, and
writes a pinned set of versions or a PEP 751 lockfile. It does not
install. Hand the lockfile to whatever installer you trust.

## Install

For package hygiene, and security reasons, the preference is to install nab itself
as a tool, e.g.

Via pipx:

```bash
pipx install nab
```

Or via uv:

```bash
uv tool install nab
```


## Quick start

```toml
# pyproject.toml
[project]
name = "example"
version = "0.1.0"
dependencies = [
    "starlette<=0.36.0",
    "fastapi<=0.115.2",
]
```

```bash
nab lock pyproject.toml
```

Writes `pylock.toml` next to the project. For a sorted
`name==version` list instead, use
`nab lock --format requirements-without-hashes --output -`.

# Security

nab makes some opinionated choices to be secure first

## Build policy

By default nab tries to extract static metadata, even from sdists,
but sometimes that is not possible and you have to build a package
to extract the dependency metadata. There are three build policies:

 * never: Never builds a Python package
 * build-local (default): Builds only your local workspace packages
   if they have dynamic versions or dependencies
 * build-remote: Builds packages sourced from indexes or VCS, it is
   recommended that this only be turned on via per-package override

## Indexes

nab does not currently support sourcing the same package from
distinct indexes. Indexes are processed in the order they are given
to nab, and the first index that has a package is the only index
that nab will source that package.

You can override this behavior by pinning specific packages to
specific behavior.

You can also list different urls as a mirror for the same index.
When a lockfile is written the primary url will always be used
so that the lockfile will be stable, even if mirrors are used
(this feature is a work in progress).

## VCS policy

By default nab only allows git URLs that point to a specific
commit. Using a floating branch as a dependency must be
enabled in the configuration.

# Standards first behavior

## Pre-releases

Pre-release versions are selected if there are no stable
versions to select given the requirements, even for transitive
dependencies. A user option to force allow or block
pre-releases per-package is a work in progress.

## Validate per-distribution dependencies

By default when a distribution is chosen the dependencies from
that distribution are used, nab does not assume two different
distributions for the same package version will have the same
dependencies.

However, sometimes you may want the lock file to produce an
sdist, that sdist may not have static metadata, and you don't
want to wait for the sdist to build on every lock, there is
a distribution policy of "sdist-install", that is the metadata
will be taken from an appropriate wheel, but the sdist will
be selected for the install.


# Libraries

This project includes multiple libraries that can be used by
other tools:

 * `nab-resolver`: An agnostic resolver library based on PubGrub, but with
   extensions that make it compatible with Python packaging standards
 * `nab-python`: A Python packaging provider that drives the nab-resolver
   with lots of specific features and optimizations for the Python packaging
   ecosystem
 * `nab-index`: Provides APIs for nab-python to interact with Python package
   indexes, abstracts HTTP library interface so different HTTP libraries can
   be plugged in

All 3 libraries are in experimental mode, I currently recommend pinning them,
e.g. `nab-resolver==0.0.1`, as APIs may change at any point.

Once we reach `0.1.0` we will only break API stability on each minor update,
so you will be able to pin to `==0.1.*` or `~=0.1.0`.
