# Your first universal lock

Create one lock for Python 3.12 through 3.15 on Linux x86-64 and Apple Silicon macOS, then install
its dependencies on a matching machine. You only need one of those environments to follow the
tutorial.

nab and its multi-target lockfile format are experimental.
[What nab locks](../explanation/locking.md) explains requirements, targets and the installer's role.

## Install nab

```bash
pipx install nab
nab --version
```

nab runs on CPython 3.10 and newer. See [Install nab](../how-to/install.md) for other installation
methods.

## Declare the project and its targets

Create an empty directory and save this as `pyproject.toml`:

```toml
[project]
name = "example"
version = "0.1.0"
requires-python = ">=3.12,<3.16"
dependencies = ["requests>=2,<3"]

[tool.nab]
mode = "universal"

[tool.nab.matrix]
python = ">=3.12,<3.16"
platforms = ["linux_x86_64", "macos_arm64"]
```

`requires-python` declares the project's supported Python range. The matrix chooses four Python
minors on each platform: eight targets. `mode = "universal"` tells nab to resolve them together into
one file. Without it, nab defaults to a single-environment resolve.

Change the range and platform IDs to match your deployments when using your own project. See
[Target matrix](../reference/target-matrix.md) for Windows, other architectures and PyPy, and
[Platform tags](../reference/platform-tags.md) for minimum operating-system versions.

## Generate the lock

Run from the project directory:

```bash
nab lock pyproject.toml
```

This reads PyPI metadata and writes `pylock.toml` beside your project. It does not install anything.
You do not need all eight target interpreters or machines to generate it.

Open the file and inspect:

- `environments`: the Python and platform combinations covered by the lock.
- `packages`: Requests and its transitive dependencies, with selected versions and artifact hashes.
- Package `marker` fields, when present: conditions selecting an entry for particular environments.

Compatible targets can share package entries. When targets need different versions, the lock can
carry both with markers. If one target fails, nab reports the failure and does not write a new lock.

(install-the-locked-dependencies)=

## Install the locked dependencies

Use Python 3.12, 3.13, 3.14 or 3.15 on one of the two declared platforms. Check your Python, create
a virtual environment and activate it:

```bash
python --version
python -m venv .venv
source .venv/bin/activate
```

On Windows, declare a Windows platform in the matrix and regenerate the lock first; activate the
environment in PowerShell with `.venv\Scripts\Activate.ps1`.

Install with pip 26.1 or newer:

```bash
python -m pip install --upgrade 'pip>=26.1'
python -m pip install -r pylock.toml
python -c "import requests; print(requests.__version__)"
```

pip's pylock support is experimental. It selects the current environment from the lock, its default
dependency groups, and no extras. This installs the locked dependencies; installing your own project
is a separate step.

## Share and update the lock

Commit `pyproject.toml` and `pylock.toml` together. Copy the same lock to another covered
environment and run the pip installation command there. An environment outside the declared targets
needs a new lock that includes it.

After changing dependencies or targets, run `nab lock pyproject.toml` again and review the diff.
Existing pins do not seed the new resolve. `--locked` currently checks single-environment locks
only; it cannot check this universal lock.

## Continue with your project

- [Lock for multiple environments](../how-to/lock-multiple-environments.md): change targets from the
  CLI and inspect per-target requirements.
- [Use a lock](../how-to/use-the-lock.md): installation formats, offline use and selection limits.
- [Resolution failures](../reference/diagnostics.md): recover when a target cannot resolve.
