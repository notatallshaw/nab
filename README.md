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

Or install with uv:

```bash
uv tool install nab
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

[tool.nab.matrix]
python = ">=3.12,<3.16"
platforms = ["linux_x86_64", "macos_arm64"]
```

Generate the lock:

```bash
nab lock pyproject.toml
```

Or use `uvx nab lock pyproject.toml` without installing nab first. This writes one lock for Python
3.12 through 3.15 on Linux x86-64 and Apple Silicon macOS.

Install from it in a covered virtual environment with pip 26.1 or newer:

```bash
python -m pip install -r pylock.toml
```

In nab, you choose which Python versions, platforms and interpreter implementations to resolve
universally, because explicit is better than implicit. See the
[tutorial](https://nab.readthedocs.io/en/stable/tutorial/getting-started.html) for a walkthrough and
[target configuration](https://nab.readthedocs.io/en/stable/reference/target-matrix.html) for other
targets.

## Safer defaults

[pip](https://pip.pypa.io/en/stable/reference/build-system/#metadata-generation) and
[uv](https://docs.astral.sh/uv/reference/settings/#no-build) can run a downloaded package's build
backend just to discover its dependencies. nab makes remote builds opt-in, reducing code execution
during locking.

- Universal locking runs no build backends. Host-specific locking allows local project builds;
  remote builds need explicit permission, which you can limit to one package or index. See
  [build policy](https://nab.readthedocs.io/en/stable/reference/build-policy.html).
- Git sources are blocked until you allow them. Opting in requires allowed schemes and repository
  prefixes, with full commit pins required by default. See
  [Git sources](https://nab.readthedocs.io/en/stable/how-to/vcs.html).
- Direct archive sources require a hash, verified before reading metadata. See
  [archive sources](https://nab.readthedocs.io/en/stable/how-to/archive-sources.html).

These controls apply during locking; your installer may still build a locked source distribution.

## More features

Use [requirements and markers](https://nab.readthedocs.io/en/stable/how-to/choose-dependencies.html)
to select dependencies by environment, and extras or groups for optional sets. Nab also supports
scoped policies and metadata overrides, multiple indexes, workspaces, pinned Git sources and hashed
archives. [Browse the guides](https://nab.readthedocs.io/en/stable/how-to/index.html) for these
workflows.

## Universal lock benchmark

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: light)" srcset="docs/_static/universal-lock-benchmark-light.svg">
    <source media="(prefers-color-scheme: dark)" srcset="docs/_static/universal-lock-benchmark-dark.svg">
    <img src="https://raw.githubusercontent.com/notatallshaw/nab/main/docs/_static/universal-lock-benchmark-light.svg" alt="Cold and warm universal lock timings for nab, uv, Poetry and PDM, split into interpreter, imports and other work; shorter bars are faster." width="960">
  </picture>
</p>

Locking Requests 2.32.3 for Python 3.12–3.14. Each panel selects the lowest median Nab time from my
local desktop and GitHub CI, on Python 3.15 and 3.15t. All four tools in a panel are measured in
that environment; the Python tools use the runtime shown. Nab covers CPython and PyPy on its eight
supported platforms. uv, Poetry and PDM resolve without platform or implementation limits. All four
locks select the same packages and usable wheels on the 48 checked targets.

Cold runs use empty dependency caches; warm runs use primed caches. Both generate fresh native
lockfiles. Nab uses `--http-backend httpx2`.

The Python bars separate interpreter launch, imports and other work, including resolution and file
writing. The split is estimated from separate probes; imports include deferred loads during locking.
Nab uses Python 3.15 lazy imports. PDM and Poetry also defer some imports. uv is shown whole.

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
