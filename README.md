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
3.12, 3.13, 3.14 and 3.15 on Linux x86-64 and Apple Silicon macOS.

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

## Soundness

By default, Nab checks dependencies and artifact compatibility for every environment you declare.

- Nab uses each target's preferred wheel metadata; uv
  [assumes consistent metadata across a release's wheels](https://docs.astral.sh/uv/reference/internals/resolver/#metadata-consistency).
- Nab retains dependency `Requires-Python` upper bounds at Python-minor granularity, including
  bounds
  [uv deliberately ignores](https://docs.astral.sh/uv/reference/internals/resolver/#requires-python).
- Nab derives yank permission from active exact pins, including selected transitive pins. Rejected
  parent versions cannot leave permission behind; uv
  [builds its allowlist from initial inputs and locked versions](https://github.com/astral-sh/uv/blob/0.13.4/crates/uv-resolver/src/yanks.rs).
  See [Nab's policy](https://nab.readthedocs.io/en/stable/reference/yanking.html) and the
  [spec](https://packaging.python.org/en/latest/specifications/file-yanking/).

## More features

Use [requirements and markers](https://nab.readthedocs.io/en/stable/how-to/choose-dependencies.html)
to select dependencies by environment, and extras or groups for optional sets. Nab also supports
scoped policies and metadata overrides, multiple indexes, workspaces, pinned Git sources and hashed
archives. [Browse the guides](https://nab.readthedocs.io/en/stable/how-to/index.html) for these
workflows.

## Policy overrides

Nab uses dependency requirements to select which releases a policy overrides.

### Allow a reviewed update during a cooldown

Keep a seven-day cooldown, with an exception for reviewed files of `certifi` 2026.10.19:

```toml
[tool.nab]
uploaded-prior-to = "P7D"

[tool.nab.packages."certifi==2026.10.19"]
uploaded-prior-to = "2026-10-20T02:24:17Z"
```

The fixed cutoff excludes later uploads of the same version. Other versions keep the global
cooldown.

### Use an index without upload times

Disable the cutoff only for an internal index that does not publish upload times:

```toml
[tool.nab]
uploaded-prior-to = "P7D"

[[tool.nab.indexes]]
name = "pypi"
url = "https://pypi.org/simple/"

[[tool.nab.indexes]]
name = "internal"
url = "https://packages.example.com/simple/"

[tool.nab.index.internal]
uploaded-prior-to = false
```

See
[package and index policies](https://nab.readthedocs.io/en/stable/reference/package-policy.html).

## Performance

`nab lock` checks dependency solutions separately for each declared target by default. These options
can reduce work, with different tradeoffs:

| Option                              | Work saved                                       | Tradeoff                                                                                                                     |
| ----------------------------------- | ------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------- |
| `assume-consistent-metadata = true` | Reuse a release's wheel metadata across targets. | Assumes sibling wheels agree on identity, Requires-Python and dependencies; unread headers are not independently verified.   |
| `--lock-policy prefer-existing`     | Try saved-lock versions before ordinary search.  | May retain older valid versions; changed inputs can make the extra checks slower.                                            |
| `--lock-policy reuse`               | Leave an accepted saved solution unchanged.      | Does not look for new releases, yanks, removed artifacts or changed remote metadata. A miss uses live preference resolution. |

To assume consistent wheel metadata, add this setting to the project's existing table:

```toml
[tool.nab]
assume-consistent-metadata = true
```

Nab still evaluates conditional dependencies for every target and checks its selected artifacts and
wheel tags. The reused core Requires-Python also applies to each target. See
[distribution policies](docs/reference/distribution-policy.md) for scoped exceptions and
[lock policies](docs/reference/cli.md) for saved-lock behavior.

`metadata-policy = "prefer-wheel"`, `assume-universal-deps = true` and
`dist-policy = { policy = "sdist-if-needed", trust-unverified-deps = true }` make separate choices
about metadata sources, cross-target source dependencies and legacy source headers. They can admit
assumptions that a project does not satisfy. See
[distribution policies](docs/reference/distribution-policy.md). HTTP backend and concurrency
settings affect throughput and resource use independently of these dependency assumptions.

## Universal lock benchmark

These measurements use Nab `f3c05aa4`, uv 0.13.7, Poetry 2.5.1 and PDM 2.29.6 on GitHub-hosted
Ubuntu 24.04. The Python CLIs run on CPython 3.15.0 with the GIL enabled; Python 3.12.13 was
selected beforehand for project operations. Each bar is the median of ten fresh processes, including
startup and the native lock command. Exports and independent validation are outside the clock.

Cold means an empty application cache; warm means a populated application cache. OS caches were not
cleared. Fresh-lock runs have no existing lock. Existing-lock runs restore each tool's own prior
lock before every command. Reuse can accept that lock without refreshing remote package information.

The two Nab rows use the same installed Nab wheels and ordinary per-target resolution. Nab default
uses the defaults. Nab performance uses `nab lock --http-backend httpx2` with this configuration,
adding `--lock-policy reuse` for existing-lock runs:

```toml
[tool.nab]
dist-policy = { policy = "wheel-only", trust-unverified-deps = true }
assume-consistent-metadata = true
assume-universal-deps = true
metadata-policy = "prefer-wheel"
```

Concurrency stays at the default of eight. Wheel-only excludes releases without a usable wheel; it
can fail where the default could use a source distribution. `assume-universal-deps` and
`trust-unverified-deps` do not affect these wheel-only selections. The bundle does not speed up
every task: adding `six==1.17.0` to the Requests lock took between about 4% and 12% longer with warm
caches, with a middle estimate near 7.5% (99% interval).

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: light)" srcset="docs/_static/universal-lock-benchmark-light.svg">
    <source media="(prefers-color-scheme: dark)" srcset="docs/_static/universal-lock-benchmark-dark.svg">
    <img src="https://raw.githubusercontent.com/notatallshaw/nab/main/docs/_static/universal-lock-benchmark-light.svg" alt="Fresh Requests lock, cold and warm caches: Nab default, Nab performance, uv, Poetry and PDM. Shorter is faster." width="1080">
  </picture>
</p>

Fresh locks for `requests==2.32.3`, checked on 48 consumer targets: Python 3.12 to 3.14, eight
platforms and CPython/PyPy.

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: light)" srcset="docs/_static/universal-lock-scientific9-light.svg">
    <source media="(prefers-color-scheme: dark)" srcset="docs/_static/universal-lock-scientific9-dark.svg">
    <img src="https://raw.githubusercontent.com/notatallshaw/nab/main/docs/_static/universal-lock-scientific9-light.svg" alt="Fresh scientific lock, cold and warm caches: Nab default, Nab performance, uv, Poetry and PDM. Shorter is faster." width="1080">
  </picture>
</p>

Fresh locks for NumPy 2.1.3, pandas 2.2.3, SciPy 1.14.1, Matplotlib 3.9.2 and scikit-learn 1.5.2,
checked on nine consumer targets: CPython 3.10 to 3.12 on Linux x86-64, Apple Silicon macOS and
Windows x86-64.

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: light)" srcset="docs/_static/universal-lock-relock-scientific9-light.svg">
    <source media="(prefers-color-scheme: dark)" srcset="docs/_static/universal-lock-relock-scientific9-dark.svg">
    <img src="https://raw.githubusercontent.com/notatallshaw/nab/main/docs/_static/universal-lock-relock-scientific9-light.svg" alt="Unchanged scientific project with an existing lock, cold and warm caches: Nab default, Nab performance, uv, Poetry and PDM. Shorter is faster." width="1080">
  </picture>
</p>

The same scientific project with an existing lock and unchanged inputs. Accepted reuse leaves the
saved lock unchanged.

All tools receive the same root requirements and Python range. Nab declares the finite consumer
matrix; the other tools use their normal native universal locking behavior. Every tool's selected
dependencies and wheel metadata were independently checked on those consumer targets. Both Nab
modes, uv, Poetry and PDM also preserve Jupyter's platform conditional dependencies across all nine
tested targets. uv's
[release-metadata consistency assumption](https://docs.astral.sh/uv/reference/internals/resolver/#metadata-consistency)
does not discard conditional dependencies. The
[Jupyter fixture and native commands](nab-project/benchmarks/readme/README.md) make the
conditional-dependency check reproducible.

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
