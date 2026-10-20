# nab

nab generates universal lockfiles for Python projects. Declare your target Python versions and
platforms in `pyproject.toml`, then write one PEP 751 `pylock.toml` covering them. An installer such
as pip installs the dependencies selected for its environment from that file.

nab is written in Python and is experimental. It resolves dependencies and can download artifacts;
it does not install packages or manage virtual environments.

[Documentation](https://nab.readthedocs.io/en/stable/) ·
[What nab locks](https://nab.readthedocs.io/en/stable/explanation/locking.html) ·
[First universal lock](https://nab.readthedocs.io/en/stable/tutorial/getting-started.html)

## Install

```bash
pipx install nab
nab --version
```

nab runs on CPython 3.10 and newer. See
[installation options](https://nab.readthedocs.io/en/stable/how-to/install.html).

## Example project

Save this as `pyproject.toml` in a new directory:

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

Run:

```bash
nab lock pyproject.toml
```

This writes one lock for Python 3.12 through 3.15 on Linux x86-64 and Apple Silicon macOS. You can
generate it on one machine without installing every target Python. Targets can use different
dependency versions when needed; the lock records the conditions for each selection.

In a virtual environment covered by the lock, use pip 26.1 or newer:

```bash
python -m pip install -r pylock.toml
```

pip's pylock support is experimental. The
[tutorial](https://nab.readthedocs.io/en/stable/tutorial/getting-started.html) includes environment
setup and pip upgrading. Commit the project file and lock together; other covered environments can
install from the same lock.

Universal mode is opt-in. Without a matrix configuration, `nab lock` defaults to the Python and
platform running nab. See
[target configuration](https://nab.readthedocs.io/en/stable/reference/target-matrix.html) for other
platforms and minimum system versions.

## More features

Use [requirements and markers](https://nab.readthedocs.io/en/stable/how-to/choose-dependencies.html)
to select dependencies by environment, and extras or groups for optional sets. Nab also supports
scoped policies and metadata overrides, multiple indexes, workspaces, pinned Git sources and hashed
archives. The [feature guide](https://nab.readthedocs.io/en/stable/explanation/features.html) links
to each workflow and reference.

## Libraries

nab also publishes components for other tools:

- `nab-resolver`: a generic PubGrub resolver with stable public module paths.
- `nab-markersets`: PEP 508 marker algebra.
- `nab-provider`: Python packaging policy and resolution logic without I/O.
- `nab-index`: package-index and source clients with caching.
- `nab-project`: resolve orchestration, lockfiles and downloads.

The other component APIs are experimental. See
[how the packages fit together](https://nab.readthedocs.io/en/stable/explanation/packages.html) and
the [CLI walkthrough](src/nab/_cli/README.md) to explore the code.
