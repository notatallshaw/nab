# What nab locks

nab generates universal lockfiles for Python projects. You declare the Python versions and platforms
your project supports; nab selects compatible dependencies for those targets and records them in one
PEP 751 `pylock.toml`. An installer uses that file to install the dependencies for its environment.

Start with [your first universal lock](../tutorial/getting-started.md). nab and its multi-target
lockfile support are experimental.

## From requirements to a lock

`pyproject.toml` describes what the project needs. A requirement such as `requests>=2,<3` permits
many versions. The packages you list are direct dependencies; the packages they need are transitive
dependencies. nab resolves both together.

The lock records selected versions and their sources, including artifact hashes. Commit it alongside
`pyproject.toml` to share that selection with development machines and CI. Installing from the lock
reuses the recorded versions. Running nab again performs a fresh resolve.

nab does not create virtual environments, install dependencies or switch your Python interpreter.
Use your environment tools and an installer such as pip for those steps.

## Requirements can be conditional

A [version specifier](https://packaging.python.org/en/latest/specifications/version-specifiers/)
such as `>=2,<3` limits releases. A marker such as `sys_platform == "win32"` makes a dependency
apply only to matching targets. Extras and dependency groups select optional sets of requirements.
[Choose dependencies and conditions](../how-to/choose-dependencies.md) shows how to combine them;
[Find a feature](features.md) covers the other workflows.

## One lock, several targets

A target is a Python version and platform, with an interpreter implementation such as CPython. A
matrix can cover Python 3.12 through 3.15 on Linux x86-64 and Apple Silicon macOS. That describes
eight targets in one lock.

The Python running nab can differ from those targets. nab computes their dependency markers and
wheel compatibility without launching each target interpreter. You can generate that matrix on Linux
without a Mac.

Universal locking is opt-in: set `mode = "universal"` and declare a
[target matrix](../reference/target-matrix.md). Without that configuration, `nab lock` defaults to
one environment: the Python and platform running nab.

## Why the lock can contain different versions

A dependency may need an older release on an older Python, or exist only on Windows. nab resolves
each target, then combines the results. It prefers to reuse earlier targets' pins when they remain
compatible, but versions can differ.

Environment markers in the lock tell an installer which entries apply. An installer running Python
3.13 on Linux selects that environment's packages from the same file a Mac uses for its selection.
[Extras and groups](../reference/lockfile-groups.md) add conditions for optional dependency sets.

If any declared target cannot resolve, the command fails without writing a new lock. See
[Resolution failures](../reference/diagnostics.md) for how to find the conflicting requirements.

## Choosing the environments you support

`[project].requires-python` declares your project's supported Python range.
`[tool.nab.matrix].python` chooses which Python minors to lock, within that range. `platforms`
chooses the machines. Those settings have different jobs even when the two Python ranges are
identical.

A bare platform ID accepts wheels without a minimum operating-system or libc version. Declare
[platform tag settings](../reference/platform-tags.md) when you need that boundary, such as glibc
2.28 or macOS 14. The lock can only cover the targets and compatibility settings you declare.

Universal mode reads metadata without running build backends. Packages needing dynamic source
metadata may need a [metadata override](../reference/metadata-overrides.md) or a different
dependency selection. An installer may still need to build an sdist recorded in the lock; locking
does not supply its build environment.

## Sharing and refreshing the result

Use the same committed `pylock.toml` on machines it covers. [Use a lock](../how-to/use-the-lock.md)
describes pip installation and its current extra and group selection limits.

After changing dependencies or targets, run `nab lock` again and review the diff. Index contents can
change between resolves; [Reproducible locks](../reference/reproducibility.md) explains upload
cutoffs and stable decision order. The current `--locked` check supports single-environment locks
only.
