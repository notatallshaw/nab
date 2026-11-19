# Use a local checkout as a source

`[[tool.nab.local-sources]]` lets you treat a directory on disk as the only candidate for a named
package. The typical use case is iterating on a fork of a dependency without publishing wheels or
pushing to a VCS host.

## Declaring a local source

Declare the dependency, then tell Nab where to find its checkout:

```toml
[project]
dependencies = ["my-fork"]

[[tool.nab.local-sources]]
name = "my-fork"
path = "../my-fork"
```

Nab resolves a relative `path` from the directory containing the project's `pyproject.toml`. The
checkout's project name must match the declared `name` after name normalization. The checkout
supplies the only candidate for that name, so its version must satisfy the project's requirements.

## Reading the checkout's metadata

At every build-policy level, Nab first reads the checkout's `pyproject.toml`. A static `[project]`
table supplies the name, version, supported Python versions and dependencies. Other fields, such as
`description`, can be dynamic without requiring a build.

Nab needs the build backend when the `[project]` table is missing, when its name or version is
missing or has the wrong type, or when `dynamic` includes `version`, `requires-python`,
`dependencies` or `optional-dependencies`. An unreadable file, invalid TOML or an invalid static
version, Python requirement or dependency is an error. Fix the checkout before trying the lock
again.

## When Nab needs the build backend

For a host resolve, `build-local` (the default) and `build-remote` both allow a local checkout's
backend to run. Nab creates an isolated environment, installs the build requirements and asks the
backend for wheel metadata. If the backend has no separate metadata hook, Nab builds the wheel. A
checkout needs a `pyproject.toml` or a `setup.py` for this step.

With `build-policy = "never"`, the resolve stops if the checkout needs a build. See
[build permissions](../reference/build-policy.md#declared-targets-and-host-builds) for declared
targets, including universal locks.

## What nab records in the lock

The lock records the checkout's path with POSIX separators, relative to the lockfile's own
directory, so a committed lockfile keeps working on another machine as long as the checkout moves
with it. Nab records no artifact hash for the directory and skips it during `nab download`.

See [Portable paths](../reference/lockfile.md) for Windows paths on another drive and locks written
to standard output.
