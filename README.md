# nab

nab is an experimental dependency locker for Python packages. It reads a `pyproject.toml` and writes a PEP 751 `pylock.toml` or pinned requirements; it does not install packages.

Documentation: <https://nab.readthedocs.io/en/stable/>

## Install

Install nab in an isolated tool environment:

```bash
uv tool install nab
# or
pipx install nab
```

Confirm the command is available with `nab --version`. nab runs on CPython 3.10 and newer.

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

The second command needs pip 26.1 or newer. pip's `pylock.toml` support is experimental; see [Use a lock](https://nab.readthedocs.io/en/stable/how-to/use-the-lock.html) for its selection limits and a hashed-requirements alternative.

`--format requirements` writes index requirements with recorded hashes. `--format requirements-without-hashes` writes index pins without their hash lines. See [Output formats](https://nab.readthedocs.io/en/stable/reference/formats.html) before using local, VCS, archive, or multi-target inputs.

## Libraries

nab publishes five component libraries for other tools:

* `nab-resolver`: a generic PubGrub resolver.
* `nab-markersets`: a PEP 508 marker algebra.
* `nab-provider`: Python packaging policy and resolution logic, without I/O.
* `nab-index`: index, archive, VCS, and cache clients.
* `nab-project`: nab's host, with resolve orchestration, workspace discovery,
  the build path, lockfile emitter, and downloader.

`nab-resolver` has stable public module paths. The other component APIs are
experimental. See [how the distributions fit together](https://nab.readthedocs.io/en/stable/explanation/packages.html).

## Project status

nab is under active development. See the [status summary](https://nab.readthedocs.io/en/stable/#status) for supported inputs and experimental features.
