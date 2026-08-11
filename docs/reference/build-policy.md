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
VCS clones, archive sources) are read statically only.  Lift
a specific package to `build-remote` with a per-package
override when you know it needs a real build
(`[tool.nab.packages.<name>]`); keep the global default tight
rather than enabling builds for the whole graph.

## `never`

Static metadata only, from any source:

* Wheels: always fine; their `METADATA` is static by definition.
* PEP 643 (Metadata 2.2+) sdists: fine when `Dynamic:` lists
  neither `Requires-Dist` nor `Provides-Extra`; the resolver reads
  the static `Requires-Dist` lines from `PKG-INFO`.
* Every other sdist, whether it lists one of those fields under
  `Dynamic:` or predates Metadata 2.2: nab falls back to the
  bundled `pyproject.toml`, reading `[project].dependencies` and
  `[project].optional-dependencies` when `[project].dynamic`
  lists neither.  Anything beyond that is skipped at look-ahead
  and surfaces as a no-version diagnostic if no candidate works.
  Setting `trust-unverified-deps` in the `dist-policy` table
  trusts a pre-2.2 `PKG-INFO` instead; see the
  [configuration reference](configuration.md).
* Local checkouts declared via `[[tool.nab.local-sources]]`:
  the directory's `pyproject.toml` is read statically.  A missing
  or malformed `[project]`, or a `dynamic` list covering
  `version`, `requires-python`, `dependencies`, or
  `optional-dependencies`, ends the resolve.
* VCS clones declared via `[[tool.nab.vcs-sources]]`: the clone
  is fetched and its `pyproject.toml` is read statically.  Same
  failure when the static read comes up empty.
* Archive sources declared via `[[tool.nab.archive-sources]]`:
  the `.tar.gz` is downloaded, hash-verified, and extracted, then
  its `pyproject.toml` is read statically.  Same failure when the
  static read comes up empty.

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

Builds extend to VCS clones, archive sources, and remote PyPI
sdists.  On top of `build-local`:

* VCS-cloned trees with dynamic deps have the backend invoked on
  the clone.
* Archive sources declared via `[[tool.nab.archive-sources]]` with
  dynamic deps have the backend invoked on the extracted tree; the
  bytes are network-fetched, so they count as remote.
* PyPI sdists are downloaded, extracted to a temp directory, and
  built when their `PKG-INFO` deps are not PEP 643 static and the
  bundled `pyproject.toml` offers no static fallback.

A backend failure on any of these surfaces as
`UnsupportedSdistError`; for a PyPI sdist the resolver skips that
version, then either picks the next candidate or, if no candidate
works, reports the accumulated build failures as a no-version
diagnostic.  Honesty over silence: a version that needs a build
which fails is treated as unbuildable, not as having zero
dependencies.  A VCS clone or an archive source ends the resolve
instead; see below.

## A source that cannot be read ends the resolve

A declared source (`[[tool.nab.local-sources]]`,
`[[tool.nab.vcs-sources]]`, `[[tool.nab.archive-sources]]`, or a
workspace member) is the only candidate for its name, and nab
reads its metadata while listing that one version.  If the
effective policy forbids the build that read needs, or the
backend runs and fails, nab names the source it could not read
and exits non-zero.  No candidate was formed, so there is nothing
to skip and no no-version diagnostic to report.

A PyPI sdist is one candidate among many, read at look-ahead, so
a forbidden or failed build rejects that version alone and the
resolver moves on to the next.

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

## Building a build requirement

A backend runs in a venv holding its project's `[build-system].requires`,
and a venv takes wheels.  When the version a requirement resolves to
publishes none this host can install, satisfying it means building that
first.  `build-requires-depth` counts the build environments nab may
open beneath the first one:

```toml
[tool.nab]
build-requires-depth = 0   # the default
```

`0` refuses, naming the requirement and the chain of builds that
reached it.  `1` builds it from its sdist, and its own build
requirements must then be wheels.  `n` allows `n` levels.

The resolve is never narrowed to wheels; that would settle on an older
backend than `[build-system].requires` asked for without saying so.  The
requirement resolves as written and the refusal names the version it
landed on.

A build already in the chain cannot be re-entered:

```text
cyclic build requirement: a 1.0 -> b 2.0 -> a 1.0
```

Two versions of one package in a chain are not a cycle; that terminates.

A build environment reads its build requirements' metadata statically,
so a per-package or per-index `build-policy` override may forbid a
build there but never permit one.  To keep a build requirement from
being built, pin it to wheels:

```toml
[tool.nab.packages.meson]
dist-policy = "wheel-only"
```

The setting is inert where builds cannot run: under
`build-policy = "never"`, and for any target that declares a platform.

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

A build-policy override for a local checkout, VCS clone, or archive
source is matched by bare name only.  A source build is decided before
any version is resolved, so a version-scoped per-package override (a
quoted `"name <specifier>"` key) does not govern a local, VCS, or
archive source build, and per-index overrides do not apply to sources
(a local source has no serving index).  Use a bare-name key to govern
a source build.

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
This is a deliberate deviation from pip: the machine is still the host, so a
build can run at all, and refusing every one of them would take the default
case with it.  Set `build-policy = "never"` to forbid it.
