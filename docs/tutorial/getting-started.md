# Getting started

This tutorial resolves a project against PyPI, writes a lock, and
installs the locked dependencies.

## Install

```bash
uv tool install nab
nab --version
```

For other install paths (extras, pipx, a checkout) see
[Install nab](../how-to/install.md).

## A minimal `pyproject.toml`

```toml
[project]
name = "example"
version = "0.1.0"
dependencies = [
    "starlette<=0.36.0",
    "fastapi<=0.115.2",
]
```

## First resolve

```bash
nab lock pyproject.toml
```

Writes `pylock.toml` next to your project. To see the resolved
versions on stdout instead, use the requirements formats:

```bash
nab lock --format requirements-without-hashes --output - pyproject.toml
```

```
annotated-types==0.8.0
anyio==4.14.2
fastapi==0.109.1
idna==3.18
pydantic==2.13.4
pydantic-core==2.46.4
starlette==0.35.1
typing-extensions==4.16.0
typing-inspection==0.4.4
```

`fastapi` resolves to 0.109.1 rather than its `<=0.115.2` cap because
every later release requires a starlette that `<=0.36.0` excludes.

The resolver pins one version per package for the host's marker
environment, taking the newest release the constraints allow. This
block came from CPython 3.12 on Linux, so another host or a later
resolve can differ.

Names use their PEP 503 canonical form, so `typing_extensions`
appears as `typing-extensions`.

## Install the locked dependencies

pip 26.1 and newer can read the default lock:

```bash
python -m pip install -r pylock.toml
```

pip selects the current environment, the lock's default dependency
groups, and no extras. Its `pylock.toml` support is experimental.

See [Use a lock](../how-to/use-the-lock.md) for hashed requirements,
an offline wheelhouse, and the limits of each path.

## Where to next

* [Configuration](../reference/configuration.md): configure the resolve.
* [Resolution failures](../reference/diagnostics.md): read an error and
  its recovery hints.
* [Universal resolution](../explanation/universal.md): lock for several
  Python and platform targets.
* [CLI](../reference/cli.md): look up commands and flags.
