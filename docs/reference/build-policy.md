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
needs a real build (`[tool.nab.packages.<name>]`); keep
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
* Archive sources declared via `[[tool.nab.archive-sources]]`:
  the `.tar.gz` is downloaded, hash-verified, and extracted, then
  its `pyproject.toml` is read statically.  Same skip behaviour
  for dynamic deps.

Picks the most reproducible posture: every input to the SAT
problem is a file read, not a sandboxed subprocess.  Use `never`
when you want a lockdown resolve with no backend invocations at
all.

## `build-local` (default)

Adds PEP 517 backend invocation on local checkouts.  When a
`[[tool.nab.local-sources]]` entry (or a workspace member) has
`dynamic = ["dependencies"]`, the project's
`[build-system].build-backend` runs inside an isolated venv via
`nab_python._build.runner` and the
resulting wheel `METADATA` is used.  Remote PyPI sdists, VCS
clones, and archive sources remain static-only.

## `build-remote`

Builds extend to VCS clones and remote PyPI sdists.  On top of
`build-local`:

* VCS-cloned trees with dynamic deps have the backend invoked on
  the clone.
* Archive sources declared via `[[tool.nab.archive-sources]]` with
  dynamic deps have the backend invoked on the extracted tree; the
  bytes are network-fetched, so they count as remote.
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
per-package override rather than raising the global to `build-remote`:

```toml
[tool.nab.packages.deepspeed]
build-policy = "build-remote"
```

That keeps the rest of the graph in the hermetic default while
permitting the one package you actually need to build.  When you
know the package's dependencies, a `dependencies` metadata override
(see the [configuration reference](configuration.md)) resolves it under
`never` without building at all.

## Overrides

A per-package override replaces the global build policy for its selected
packages, in either direction.  Key it by name:

```toml
[tool.nab]
build-policy = "never"

[tool.nab.packages.deepspeed]
build-policy = "build-remote"
```

Or list several packages in one `[[tool.nab.package-rules]]` entry:

```toml
[[tool.nab.package-rules]]
match = ["deepspeed", "flash-attn"]
build-policy = "build-remote"
```

Set the build policy for every package served from a given index with a
per-index override instead:

```toml
[tool.nab.index.internal]
build-policy = "build-remote"
```

A build-policy override for a local checkout or VCS clone is matched by
bare name only.  A source build is decided before any version is
resolved, so a version-scoped per-package override (a quoted
`"name <specifier>"` key) does not govern a local or VCS source build,
and per-index overrides do not apply to sources (a local source has no
serving index).  Use a bare-name key to govern a source build.

## A declared platform forbids host builds

A PEP 517 backend always runs on the host nab runs on, so it reports the
host's dependencies.  That is correct when you resolve for the host, but
wrong when you resolve *as if* you were on another machine.  Every target
that moves the platform axis therefore forbids host builds:
`build-policy` is forced to `never`, and an explicit non-`never` value
(global or in any override) is a config error, checked before the resolve
starts.  That covers both surfaces that declare a machine:

* `[tool.nab.environment]` with a `platform` or an `implementation`.
* `mode = "universal"`, where every matrix tuple declares one.

This matches pip, which requires `--only-binary=:all:` under `--platform`,
`--abi`, or `--implementation`.

A retarget of the **python axis alone** (`[tool.nab.environment].python`,
or `--python X.Y`) is different: the machine is still the host.  nab warns
that a build would report the host interpreter's metadata, and permits it.
This is a deliberate deviation from pip: the workspace `build-local` floor
exists for members with dynamic metadata, and forbidding a build here would
break every workspace that also pins a Python.  Set `build-policy = "never"`
to forbid it.
