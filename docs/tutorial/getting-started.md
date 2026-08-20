# Getting started

This tutorial walks through a first resolve with nab against PyPI.

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
environment, taking the newest release the constraints allow. That
block came off CPython 3.12 on Linux, so another host or a later
resolve gives different versions. Names print in their PEP 503
canonical form, so `typing_extensions` appears as `typing-extensions`.

For multi-platform / multi-Python locks see
[universal resolution](../explanation/universal.md).

## Where to next

* [Configuration](../reference/configuration.md): every key under
  `[tool.nab]`, what it does, and what the default is.
* [CLI](../reference/cli.md): every subcommand, flag, exit code, and
  environment variable.
* [Lockfile](../reference/lockfile.md): what is in `pylock.toml`, the
  `requirements.txt --hash` shape, and what `nab download` fetches.
