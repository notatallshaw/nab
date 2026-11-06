# Universal resolution

> [!WARNING]
> The multi-target PEP 751 lockfile format is experimental and may change without notice.

A universal resolve puts several target environments in one lock. For CPython 3.11 through 3.13 on
Linux and macOS:

```toml
[tool.nab.matrix]
python = ">=3.11,<3.14"
platforms = ["linux_x86_64", "macos_arm64"]
```

```text
base matrix + include groups
             |
             v
expand and deduplicate targets
             |
             v
resolve each target, sharing fetches
             |
             v
merge pins and environment markers
             |
             v
         pylock.toml
```

These six targets use the single-environment resolver. Listings and metadata are shared; earlier
pins become preferences for later targets.

The matrix declares coverage. Dependency markers do not add targets. See
[Lock for multiple environments](../how-to/lock-multiple-environments.md) for commands.

## Interpreter implementations

To use the same Python/platform combinations for both interpreters:

```toml
[tool.nab.matrix]
python = ">=3.11,<3.14"
platforms = ["linux_x86_64"]
implementations = ["cpython", "pypy"]
```

CPython is the default. PyPy targets evaluate markers as `implementation_name = "pypy"` and
`platform_python_implementation = "PyPy"`, and accept PyPy wheel tags. A mixed lock records the
implementation in its environment declarations; a PyPy-only lock refuses CPython. A CPython-only
lock also records the implementation when a consulted marker names it.

PyPy's `implementation_version` is currently modelled as its Python version. Dependencies that
compare it with a PyPy release may be missed. Nab omits that synthetic value from lock environments.

### Add combinations with `include`

For CPython 3.10 through 3.15 on Linux and macOS, plus PyPy 3.11 on Linux:

```toml
[tool.nab.matrix]
python = ">=3.10,<3.16"
platforms = ["linux_x86_64", "macos_arm64"]

# Add PyPy without changing the base CPython targets.
[[tool.nab.matrix.include]]
python = "==3.11"
platforms = ["linux_x86_64"]
implementations = ["pypy"]
```

Prefer `include` for additional target groups. Each entry replaces supplied settings and inherits
omitted defaults. Groups expand independently; exact duplicate targets merge.

### Specialize selected implementations with `overrides`

For the same targets, with PyPy selection controlled by the CLI:

```toml
[tool.nab.matrix]
python = ">=3.10,<3.16"
platforms = ["linux_x86_64", "macos_arm64"]
implementations = ["cpython", "pypy"]

# Specialize PyPy only while it is selected.
[tool.nab.matrix.overrides.implementations.pypy]
python = "==3.11"
platforms = ["linux_x86_64"]
```

```bash
# Use both implementations with the configured PyPy specialization.
nab lock

# Leave the PyPy override inactive.
nab lock --project-matrix-implementations cpython
```

An override does not enable its implementation. An explicit PyPy include remains active when the CLI
selects only CPython for the base matrix. When both mechanisms apply, explicit include values take
precedence over implementation overrides, which take precedence over shared defaults. Use
`free-threaded = [false, true]` to expand both builds within a group. See
[Target matrix](../reference/target-matrix.md) for version-specific free-threaded examples.

## Where metadata comes from

For CPython 3.15 on Linux, given these wheels:

```text
demo-1.0-cp315-cp315-manylinux_2_17_x86_64.whl
demo-1.0-py3-none-any.whl
```

Nab reads dependencies from the native wheel, which ranks higher for that target. Tag preference,
then build tag, determines the choice. Targets share a wheel's metadata and an sdist's `PKG-INFO` by
archive URL; index listings are fetched once per package.

If equally ranked wheels already fetched disagree on dependencies, Nab reports an error. It compares
only fetched siblings, so it may not detect every disagreement. For a dynamic sdist without static
metadata, [assume-universal-deps](../reference/build-policy.md#assuming-dependencies-are-universal)
explicitly permits reusing dependency declarations from a same-version wheel.

The opt-in
[assume-consistent-metadata](../reference/distribution-policy.md#assuming-wheel-metadata-is-consistent)
policy instead lets one wheel's complete metadata answer for other wheels of the release. It shares
the raw metadata within one operation; every target still evaluates its markers and applies its
artifact restrictions. Unread sibling metadata is not independently validated. The default continues
to use the target's own wheel, and `assume-universal-deps` keeps its dependency-only scope.

## Patch-release markers

A minor target normally covers all its patch releases. This dependency changes within Python 3.11:

```toml
[project]
name = "example-lib"
version = "0.1.0"
requires-python = ">=3.11,<3.12"
dependencies = ['some-backport ; python_full_version < "3.11.4"']

[tool.nab]
mode = "universal"

[tool.nab.matrix]
python = "==3.11"
platforms = ["linux_x86_64"]
```

Nab starts at 3.11.0, then splits at the marker boundary:

```text
Python 3.11 minor
    |
    +-- < 3.11.4         backport required
    |
    +-- >= 3.11.4.dev0   backport absent
```

The lock records each slice in `environments`:

```toml
environments = [
    'python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64" and python_full_version < "3.11.4"',
    'python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64" and python_full_version >= "3.11.4.dev0"',
]
```

The backport's package marker selects the lower slice:

```toml
[[packages]]
name = "some-backport"
version = "1.2.0"
marker = 'python_full_version < "3.11.4"'
```

The upper slice starts at `3.11.4.dev0`: PEP 440 excludes prereleases from `< "3.11.4"`, so starting
at `3.11.4` would leave them uncovered. New dependencies may introduce further boundaries; Nab
repeats until no new split is needed.

Ordered comparisons, equality, inequality and compatible-release markers can split a minor. Nab
rejects `python_full_version` comparisons it cannot represent as intervals: membership tests,
arbitrary equality, non-version values, comparisons with another variable, and certain pre- or
post-release boundaries inside the minor.

### Pin a patch release instead

```toml
[tool.nab.matrix.python-patches]
"3.11" = "3.11.4"
"3.12" = "3.12.1"
```

Each named minor resolves as that exact release and is not split. Other minors keep their interval
behavior.

## Marker variables and wheel tags

A platform entry controls both markers and wheel compatibility:

```toml
[tool.nab.matrix]
python = "==3.15"
platforms = [{ id = "linux_x86_64", libc = "musl", runs-on-libc = "1.2" }]
```

This evaluates markers for Linux x86-64 and accepts musllinux wheels compatible with musl 1.2.
Changing `libc` changes wheels without changing the PEP 508 environment: that specification has no
libc marker. Such targets must agree on package selections and pins to share a lock.

Each axis sets related marker variables together:

| Matrix setting                         | Marker variables                                                  | Default                                                           |
| -------------------------------------- | ----------------------------------------------------------------- | ----------------------------------------------------------------- |
| `python`                               | `python_version`, `python_full_version`, `implementation_version` | One minor at a time; full version `<minor>.0` unless patch-pinned |
| `implementations`                      | `implementation_name`, `platform_python_implementation`           | `cpython`, `CPython`                                              |
| `platforms` id                         | `sys_platform`, `platform_system`, `platform_machine`, `os_name`  | Values for the declared platform                                  |
| `platform-release`, `platform-version` | `platform_release`, `platform_version`                            | `""`                                                              |

Every PEP 508 environment variable has a value. `extra` is bound while classifying a package's base
and extra dependencies; `extras` and `dependency_groups` are empty during resolution and used in
lockfile selection markers. See the
[marker specification](https://packaging.python.org/en/latest/specifications/dependency-specifiers/#environment-markers).

Wheel tags are computed without a live target interpreter. CPython accepts `cpXY`/`abi3`,
free-threaded CPython accepts `cpXYt`/`abi3t`, and PyPy accepts `pypyXY_ppNN` ABIs; compatible pure
Python wheels remain eligible. A version with no compatible wheel can still use a `.tar.gz` sdist,
subject to build policy. Legacy `.zip` sdists require `allow-zip-sdists = true`. See
[Platform tags](../reference/platform-tags.md) for settings and limits.

### Declare kernel-dependent markers

```toml
[tool.nab.matrix]
python = "==3.15"
platforms = [{ id = "linux_x86_64", platform-release = "5.15.0" }]
```

Without a kernel value, comparisons such as `platform_release >= "5.10"` evaluate false and the
conditional dependency is dropped. Kernel values affect markers, not wheel tags.

Platform IDs and implementations are fixed enumerations. Python ranges select known minors; a range
may extend beyond them, but must match at least one.
