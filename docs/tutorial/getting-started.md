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

Create a new directory and save the following as `pyproject.toml`, or use an existing project's dependency declarations. Run the commands below from that directory.

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

This writes `pylock.toml` next to your project without installing its dependencies. Open it to see exact package versions, the files and hashes selected for them, and the environment the lock covers. It includes dependencies of the packages you requested, such as FastAPI's dependency on Pydantic.

By default, the target is the interpreter and platform running nab, which may differ from your application's virtual environment. To target Python 3.12 on the same platform, use `nab lock --python 3.12 pyproject.toml`. See {ref}`the resolve environment <the-resolve-environment>` for other targets.

To inspect a short list of versions on stdout, run:

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

This command resolves again; it does not export the existing lock. nab prefers the newest releases that satisfy the constraints. This block came from CPython 3.12 on Linux, so another target or a later resolve can differ.

Names use their PEP 503 canonical form, so `typing_extensions`
appears as `typing-extensions`.

(install-the-locked-dependencies)=
## Install the locked dependencies

Use a virtual environment whose Python and platform match the lock. If you do not have one, create it with your application's Python and activate it:

```bash
python -m venv .venv
source .venv/bin/activate
```

On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1` instead. Check `python --version` against the Python you resolved for.

pip 26.1 and newer can read the default lock. Upgrade pip in this environment, then install:

```bash
python -m pip install --upgrade 'pip>=26.1'
python -m pip install -r pylock.toml
```

pip selects the current environment, the lock's default dependency
groups, and no extras. Its `pylock.toml` support is experimental.

This installs the dependencies, not your own project. For this example, confirm FastAPI is available with `python -c "import fastapi; print(fastapi.__version__)"`.

See [Use a lock](../how-to/use-the-lock.md) for hashed requirements,
an offline wheelhouse, and the limits of each path.

## Keep or refresh the result

Commit `pyproject.toml` and `pylock.toml` together. Another matching environment can install from the same lock without asking nab to select versions again.

When you change dependencies or want a fresh selection, run `nab lock pyproject.toml` and review the lock's diff before installing. Existing pins are not preferences for the next resolve. For automated checks, follow {ref}`Check a committed lock in CI <check-a-committed-lock-in-ci>`.

## Where to next

* [Configuration](../reference/configuration.md): configure the resolve.
* [Resolution failures](../reference/diagnostics.md): read an error and
  its recovery hints.
* [Universal resolution](../explanation/universal.md): lock for several
  Python and platform targets.
* [CLI](../reference/cli.md): look up commands and flags.
