(the-resolve-environment)=

# Target environment

nab resolves for the interpreter it is running on. The host is the target, like pip: the same lock
command on a Python 3.14 machine resolves the markers a Python 3.14 install evaluates.

`[tool.nab.environment]` selects a different target. A `python` setting alone changes the Python
version while keeping the host platform. A `platform` setting makes nab model that platform, using
the host's Python version if omitted and CPython if `implementation` is omitted:

```toml
[tool.nab]
build-policy = "never"

[tool.nab.environment]
python = "3.10"             # a version, not a specifier ("3.10" or "3.10.5")
platform = "linux_x86_64"   # a matrix platform id
implementation = "cpython"  # "cpython" (default) or "pypy"
```

- `python` alone keeps the host machine and moves only the interpreter, which is what pip's
  `--python-version` does. `nab lock --python 3.10` does it for one run, as does
  `nab lock --project-environment-python 3.10`.
- `platform` (with or without `implementation`) declares the machine, so the PEP 508 markers are
  synthesized from the platform id. `implementation` needs a `platform`: an interpreter is modelled
  on a declared machine, never on the host's.
- For a declared platform, nab forces `build-policy = "never"` and rejects an explicit different
  policy. Changing only Python produces a warning and keeps the configured build permissions. See
  [Build policy](build-policy.md).

`platform` accepts a platform ID with default settings, or a table containing the ID and explicit
settings, just like an entry in `[tool.nab.matrix].platforms`.

```toml
[tool.nab]
build-policy = "never"

[tool.nab.environment]
python = "3.13"
platform = { id = "macos_arm64", runs-on-macos = "14.0" }
```

[Platform tags](platform-tags.md) gives their defaults and rules. A bare id sets no system floor and
accepts wheels of any level. Declare `runs-on-macos` to set the oldest supported macOS; wheels
needing a newer one are dropped.

## Marker evaluation and wheel compatibility

PEP 508 markers condition dependencies; PEP 425 tags filter wheels. A version with only incompatible
wheels and no sdist is not a candidate (`pywin32` on Linux). The lockfile records only wheels the
target can install.

Tags filter wheels only, so a `.tar.gz` sdist keeps a version alive for any target. nab drops every
other sdist format when it parses the listing, so a version whose only sdist is a `.zip` and which
ships no wheel is not a candidate either (`pyreadline==2.1`).

The lock records this single target in PEP 751 `environments`; an installer rejects it on other
platforms. See [Lockfiles](lockfile.md). `mode = "universal"` lets nab resolve several targets
together.

## Combining target settings

`[tool.nab.environment]` and `[tool.nab.matrix]` cannot both be set: the matrix already declares one
environment per target. The rule holds however each was declared, in a file or by a flag.

## `[tool.nab.marker-environment]` (deprecated)

The old overlay could combine one declared marker with the rest of the host, producing a machine
that does not exist. It is translated to `[tool.nab.environment]` with a warning. An unknown axis or
unmapped `(sys_platform, platform_machine)` pair is a config error.

`platform_release` and `platform_version` describe the declared platform, so they translate into the
platform table (`platform-release`, `platform-version`) and need the pair alongside them.

## Python settings

- `[tool.nab].requires-python` (or `[project].requires-python`): the range the project supports. A
  declaration. It is recorded as the lockfile's top-level `requires-python` and checked against the
  resolve target; it does not choose that target.

  The check reads the declaration at the language version, the same way a candidate's
  `Requires-Python` is read, so `==3.13` and `>=3.13.2` both admit a 3.13 target however precisely
  that target names its interpreter, and `!=3.13` excludes one however it is named.

  A declaration that excludes the target is a config error naming the setting to change:
  `[tool.nab.environment] python` for a single-environment resolve, `[tool.nab.matrix].python` and
  `[tool.nab.matrix.python-patches]` for a matrix target, since neither `--python` nor
  `[tool.nab.environment]` is allowed alongside a matrix.

- `[tool.nab.environment].python`: the Python to resolve for, defaulting to the host's. A single
  version, not a specifier. `--python X.Y` and `--project-environment-python X.Y` set it for one
  run.

- `[tool.nab.matrix].python`: a range like `>=3.11,<3.14`, expanded into one target per minor
  version. Pair with `[tool.nab.matrix].platforms`, `[tool.nab.matrix].implementations`,
  per-implementation overrides and (optionally) `[tool.nab.matrix].python-patches` to select targets
  and their lockfile markers.

A matrix with one effective target defaults to `specific` and resolves that declared target. A
matrix with multiple targets defaults to `universal`; an explicit `specific` at the same precedence
is an error.

A higher-precedence `--project-mode specific` on the command line outranks the `[tool.nab]` table,
so it selects a single-environment resolve for that run and the declared matrix does not apply. This
is how a universal project takes one single-environment lock without editing its `pyproject.toml`:

```bash
nab lock --project-mode specific --python 3.13
```

## Declaring the environment on the command line

```bash
nab lock --project-environment-python 3.12 \
  --project-environment-platform macos_arm64 runs-on-macos=14.0 \
  --project-environment-implementation cpython \
  --project-build-policy never
```

`--project-environment-platform` accepts one platform id and its tag settings; a second id is an
error. Unspecified axes keep their file values or use the host.
