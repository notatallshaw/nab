# Build policy

Some Python packages publish dependency metadata that nab can
read without running any code; others require invoking a PEP 517
build backend.  `[tool.nab].build-policy` is the single knob that
controls how far nab is willing to go.

Three levels, strictest first.  Each level reads static metadata
from every source it admits; the difference is what is permitted
to fall through to a backend invocation when the static read
returns nothing usable.

The default is `build-local`: local checkouts and workspace
members may invoke a backend, but remote sources (PyPI sdists,
VCS clones) are read statically only.  Lift a specific package
to `build-remote` with a per-package override when you know it
needs a real build (`[tool.nab.build-policy-package]`); keep
the global default tight rather than enabling builds for the
whole graph.

## `never`

Static metadata only, from any source:

* Wheels: always fine; their `METADATA` is static by definition.
* PEP 643 (Metadata 2.2+) sdists: fine; the resolver reads the
  static `Requires-Dist` lines from `PKG-INFO`.
* Sdists whose `PKG-INFO` declares `Dynamic: Requires-Dist`: nab
  falls back to reading `[project].dependencies` from the
  bundled `pyproject.toml` when the field is not listed under
  `[project].dynamic`.  Anything beyond that is skipped at
  look-ahead and surfaces as a no-version diagnostic if no
  candidate ultimately works.
* Local checkouts declared via `[[tool.nab.local-sources]]`:
  the directory's `pyproject.toml` is read statically.  A
  member that requires `dynamic = ["dependencies"]` is skipped.
* VCS clones declared via `[[tool.nab.vcs-sources]]`: the clone
  is fetched and its `pyproject.toml` is read statically.  Same
  skip behaviour for dynamic deps.

Picks the most reproducible posture: every input to the SAT
problem is a file read, not a sandboxed subprocess.  Use `never`
when you want a lockdown resolve with no backend invocations at
all.

## `build-local` (default)

Adds PEP 517 backend invocation on local checkouts.  When a
`[[tool.nab.local-sources]]` entry (or a workspace member) has
`dynamic = ["dependencies"]`, the project's
`[build-system].build-backend` runs inside an isolated venv via
[`nab_python._build.runner`](../reference/build.md) and the
resulting wheel `METADATA` is used.  Remote PyPI sdists and VCS
clones remain static-only.

## `build-remote`

Builds extend to VCS clones and remote PyPI sdists.  On top of
`build-local`:

* VCS-cloned trees with dynamic deps have the backend invoked on
  the clone.
* PyPI sdists whose `PKG-INFO` is dynamic and which have no
  static `pyproject.toml` fallback are downloaded, extracted to a
  temp directory, and built.

A backend failure on any of these surfaces as
`UnsupportedSdistError`; the resolver skips that version, then
either picks the next candidate or, if no candidate works,
reports the accumulated build failures as a no-version
diagnostic.  Honesty over silence: a version that needs a build
which fails is treated as unbuildable, not as having zero
dependencies.

## Choosing a level

The default `build-local` handles the common case (a local
checkout with `dynamic = ["version"]` from hatch-vcs or similar)
without opening the door to remote-sdist builds.  Lower to
`never` when you want a fully hermetic resolve.

For transitive dependencies that only publish a dynamic sdist
(native or CUDA-heavy wheels are the usual offenders), prefer a
per-package override rather than raising the global to
`build-remote`:

```toml
[tool.nab.build-policy-package]
deepspeed = "build-remote"
```

That keeps the rest of the graph in the hermetic default while
permitting the one package you actually need to build.

## Per-package overrides

`[tool.nab.build-policy-package]` replaces the global on a
per-package basis, in either direction:

```toml
[tool.nab]
build-policy = "never"

[tool.nab.build-policy-package]
deepspeed = "build-remote"
```

The override participates in the `marker_environment` guard:
when impersonating a non-host target, every override must be
`never`, because backends run on the host and cannot reflect the
impersonated target's metadata.

## Interaction with `marker_environment`

The marker overlay (used by universal resolution and by manual
platform impersonation) is incompatible with anything other than
`never` at both the global and the override level: invoking a
backend on the host produces metadata that does not match the
impersonated target, so the combination is rejected at
config-load time.
