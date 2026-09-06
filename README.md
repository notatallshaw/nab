# nab

nab is an experimental dependency locker and download tool for Python
packages, written in Python but aiming to have similar performance to
uv on cold resolves.

It reads your `pyproject.toml`, finds compatible versions of your
dependencies, and writes a PEP 751 `pylock.toml` or pinned requirements.
An installer such as pip then installs from that file.

Documentation: <https://nab.readthedocs.io/en/stable/>

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

From a directory containing your `pyproject.toml` (or the [example
below](#example-project)), resolve and write the lock:

```bash
nab lock pyproject.toml
```

Open `pylock.toml` to see the selected versions and distribution hashes.

In a virtual environment matching the lock's Python and platform,
install with pip 26.1 or newer:

```bash
python -m pip install -r pylock.toml
```

pip's `pylock.toml` support is experimental. This installs the locked
dependencies; installing your own project is a separate step. The
[getting-started tutorial][getting-started] includes environment setup,
and [Use a lock][use-lock] explains pip's selection limits.

## Why nab?

### Keep dependency choices in a file

Locking is a separate step from changing your environment. `nab lock`
records exact dependency versions for the environments you resolve,
so you can review and commit the result before installing it.

For a single-environment lock, you can also [check the committed file in
CI][check-lock].

### Use standard packaging formats

Keep your dependency declarations in the standard `[project]` table of
`pyproject.toml`. nab reads Python packaging versions, requirements, and
environment markers, and produces a [cross-tool PEP 751
lockfile][lockfiles]. You can also write hashed requirements for pip.

### Choose your target environments explicitly

By default, nab resolves for the Python and platform running nab. You
can [declare a different target][resolve-environment], such as the
Python used in production, or a matrix of targets for several platforms.
Dependency markers and wheel compatibility are evaluated for those
targets, and the lock records where it applies. If any declared target
cannot be resolved, nab fails without writing a lock.

### Security via build and VCS policies

Reading dependency metadata can require running a package's build
backend. nab [reads remote sources without building them by
default][build-policy]; local checkouts may build when their metadata
needs it. If a remote package requires a build, you can opt in for that
package. [Direct archives][archive-sources] also require a verified
digest.

nab blocks [VCS dependencies][vcs] by default and supports only Git. To
allow them, set `policy = "allow"` under `[tool.nab.vcs]` and list both
the permitted URL schemes and repository prefixes in `allowed-schemes`
and `allowed-repos`. By default, `require-pin = true` requires a full
40-character commit SHA; branch and tag references are rejected. Declare
dependencies in `[[tool.nab.vcs-sources]]`. Allowing a repository
permits cloning and reading static metadata; running its build backend
still requires opting in to remote builds.

### Override global policies for specific packages and indexes

To keep global policies strict and secure while allowing exceptions for
specific needs, you can [override global policies][policy-overrides] for
a package, a version range such as `numpy > 2`, or every package served
by a named index.

For example, allow remote builds for one package, require wheels for
selected versions, or set a different upload cutoff for an internal
index. Use `[tool.nab.packages."numpy > 2"]` for one selector,
`[[tool.nab.package-rules]]` to share a policy across several, or
`[tool.nab.index.<name>]` for an index.

Requirement selectors accept package names and version specifiers.
Fields without an override retain their global defaults.

## Example project

If you do not have a project yet, save this small example as
`pyproject.toml` in a new directory:

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

Run `nab lock pyproject.toml` from that directory. The constraints above
must hold together: nab may choose an older FastAPI release to satisfy
the Starlette limit.

## Write hashed requirements

For a single-environment project using only index packages, the
hashed-requirements alternative is:

```bash
nab lock --format requirements pyproject.toml
python -m pip install --require-hashes -r requirements.txt
```

This performs a fresh resolve and writes `requirements.txt`.

`--format requirements-without-hashes` omits separate hash lines from
index pins; archive URLs keep their digest. See [Output
formats][output-formats] before using local, VCS, archive, or
multi-target inputs.

## Lock for your deployment targets

If your application uses Python 3.12 on the same platform as nab, select
it explicitly:

```bash
nab lock --python 3.12 pyproject.toml
```

`--python` changes the resolve target; it does not install or switch
interpreters. Use an environment matching the lock when installing.

To cover Python 3.11 and 3.12 on Linux x86-64 and macOS ARM64, add these
tables to your project:

```toml
[tool.nab]
mode = "universal"

[tool.nab.matrix]
python = ">=3.11,<3.13"
platforms = ["linux_x86_64", "macos_arm64"]
```

Run `nab lock pyproject.toml` again to write one lock covering those
four targets. Versions can differ between targets when compatibility
requires it. The multi-target lock format is experimental; [Universal
resolution][universal] explains the matrix and how to set minimum
operating-system versions.

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
[getting-started]: https://nab.readthedocs.io/en/stable/tutorial/getting-started.html
[lockfiles]: https://nab.readthedocs.io/en/stable/reference/lockfile.html
[output-formats]: https://nab.readthedocs.io/en/stable/reference/formats.html
[packages]: https://nab.readthedocs.io/en/stable/explanation/packages.html
[policy-overrides]: https://nab.readthedocs.io/en/stable/reference/configuration.html#overrides
[resolve-environment]: https://nab.readthedocs.io/en/stable/reference/configuration.html#the-resolve-environment
[status]: https://nab.readthedocs.io/en/stable/#status
[universal]: https://nab.readthedocs.io/en/stable/explanation/universal.html
[use-lock]: https://nab.readthedocs.io/en/stable/how-to/use-the-lock.html
[vcs]: https://nab.readthedocs.io/en/stable/how-to/vcs.html
