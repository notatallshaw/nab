# Getting started

This guide walks through a first resolve with nab against PyPI.

## Install

```bash
uv tool install nab
nab --version
```

For other install paths (extras, pipx, dev check-out) see
[installation](installation.md).

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
anyio==4.6.2.post1
fastapi==0.115.2
idna==3.10
sniffio==1.3.1
starlette==0.36.0
typing_extensions==4.12.2
```

The resolver pins one version per package for the host's marker
environment. For multi-platform / multi-Python locks see
[universal resolution](universal.md).

## Where to next

* [Configuration](configuration.md): every key under `[tool.nab]`,
  what it does, and what the default is.
* [CLI](cli.md): every subcommand, flag, exit code, and
  environment variable.
* [Lockfile](lockfile.md): what is in `pylock.toml`, the
  `requirements.txt --hash` shape, and how `nab download` consumes
  them.
