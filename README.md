# nab

nab is an experimental dependency locker for Python packages. It reads a
`pyproject.toml` and writes a PEP 751 `pylock.toml` or pinned
requirements; it does not install packages.

Documentation: <https://nab.readthedocs.io/en/stable/>

## Why nab?

* It uses standard Python packaging inputs and outputs: static PEP 621
  dependency metadata, PEP 440 versions, PEP 508 requirements and
  markers, and [cross-tool PEP 751 lockfiles][lockfiles].
* It can [resolve for a declared Python, platform, and
  implementation][resolve-environment]. Marker evaluation and wheel
  compatibility use that target, and the lock records its scope.
* It limits what resolution executes. [Remote sources are static-only by
  default][build-policy], [direct archives][archive-sources] require a
  verified digest, and hashed requirements can be installed with pip's
  `--require-hashes` mode.
* It separates resolution from installation. `nab lock` does not install
  project dependencies. Review its output before installation, or
  [check a single-environment `pylock.toml` in CI][check-lock].

## Install

Install nab in an isolated tool environment:

```bash
uv tool install nab
# or
pipx install nab
```

Confirm the command is available with `nab --version`. nab runs on
CPython 3.10 and newer.

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

Lock and install the dependencies:

```bash
nab lock pyproject.toml
python -m pip install -r pylock.toml
```

The second command needs pip 26.1 or newer. pip's `pylock.toml` support
is experimental; [Use a lock][use-lock] explains its selection limits
and a hashed-requirements alternative.

`--format requirements` writes index requirements with recorded hashes.
`--format requirements-without-hashes` writes index pins without their
hash lines. See [Output formats][output-formats] before using local,
VCS, archive, or multi-target inputs.

## Libraries

nab publishes five component libraries for other tools:

* `nab-resolver`: a generic PubGrub resolver.
* `nab-markersets`: a PEP 508 marker algebra.
* `nab-provider`: Python packaging policy and resolution logic without
  I/O.
* `nab-index`: package-index and source clients with caching.
* `nab-project`: resolve orchestration plus lock and download workflows.

`nab-resolver` has stable public module paths. The other component APIs
are experimental. See [how the distributions fit together][packages].

## Project status

nab is under active development. See the [status summary][status] for
supported inputs and experimental features.

[archive-sources]: https://nab.readthedocs.io/en/stable/how-to/archive-sources.html
[build-policy]: https://nab.readthedocs.io/en/stable/reference/build-policy.html
[check-lock]: https://nab.readthedocs.io/en/stable/reference/lockfile.html#checking-the-lock-in-ci
[lockfiles]: https://nab.readthedocs.io/en/stable/reference/lockfile.html
[output-formats]: https://nab.readthedocs.io/en/stable/reference/formats.html
[packages]: https://nab.readthedocs.io/en/stable/explanation/packages.html
[resolve-environment]: https://nab.readthedocs.io/en/stable/reference/configuration.html#the-resolve-environment
[status]: https://nab.readthedocs.io/en/stable/#status
[use-lock]: https://nab.readthedocs.io/en/stable/how-to/use-the-lock.html
