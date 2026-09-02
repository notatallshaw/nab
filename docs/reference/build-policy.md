# Build policy

Some Python packages publish dependency metadata that nab can
read without running any code; others require invoking a PEP 517
build backend.  `[tool.nab].build-policy` is the single knob that
controls how far nab is willing to go.

Three levels, strictest first.  Each level reads static metadata
from every source it admits; the difference is what is permitted
to fall through to a backend invocation when the static read
returns nothing usable.

An sdist below means a `.tar.gz`.  nab drops every other format
when it parses an index listing, so no policy sees one.

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
* Other sdists: nab reads the bundled `pyproject.toml` when
  `[project].dynamic` leaves dependencies and optional dependencies
  static. Otherwise look-ahead skips the candidate.
  `trust-unverified-deps` instead trusts pre-2.2 `PKG-INFO`; see
  [Configuration](configuration.md).
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

`never` runs no build backend unless a per-package or per-index override
raises the policy. It still reads configured indexes and source files.

## `build-local` (default)

Adds PEP 517 backend invocation for `[[tool.nab.local-sources]]` and
workspace members. When static metadata is unavailable, the project's
backend runs in an isolated environment and nab reads the resulting
wheel metadata.

Static metadata is unavailable when `[project]` is missing or malformed,
or when `[project].dynamic` names `version`, `requires-python`,
`dependencies`, or `optional-dependencies`.
Remote PyPI sdists, VCS clones, and archives remain static-only.

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

A backend failure surfaces as `UnsupportedSdistError`. For a PyPI sdist,
the resolver rejects that version and tries another candidate; if none
works, the diagnostic includes the build failures. A VCS or archive
source ends the resolve instead because it is the only candidate for its
name.

## A source that cannot be read ends the resolve

A declared local, VCS, archive, or workspace source is the only
candidate for its name. If its required build is forbidden or fails,
nab names the source and exits non-zero. No candidate was formed, so
there is no no-version diagnostic.

A PyPI sdist is one candidate among many, read at look-ahead, so
a forbidden or failed build rejects that version alone and the
resolver moves on to the next.

## Choosing a level

The default `build-local` handles a local checkout with dynamic metadata
without running backends for remote sources. Use global `never` to make
backend execution opt-in through per-package or per-index overrides.

For transitive dependencies that only publish a dynamic sdist
(native or CUDA-heavy wheels are the usual offenders), prefer a
per-package override rather than raising the global to `build-remote`:

```toml
[tool.nab.packages.deepspeed]
build-policy = "build-remote"
```

The override permits a backend only for that package. If its
dependencies are known, a `dependencies` metadata override can avoid the
build instead; see [Configuration](configuration.md).

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

A per-package or per-index `dist-policy` narrows the resolve the other
way.  `sdist-only` and `sdist-install` keep a build requirement's
wheels out of the environment even where the index publishes one this
host installs, so satisfying it means building it, and the refusal
names the policy rather than the listing.

Build output must match the resolved release because its environment was
chosen for that release. A wheel with another name or version is
refused. PEP 440 treats `1.0` and `1.0.0` as one release, but not
`1.0+local`.

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

`build-requires-depth` is inert only where no build can run. A declared
platform makes every explicit non-`never` policy, including overrides, a
config error. Global `build-policy = "never"` alone is insufficient: an
override may still permit a build whose environment reads the same
depth.

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

Declared local, VCS, and archive sources match only an override keyed by
the bare name. Their version is unknown when the build is chosen, so
version-scoped overrides do not apply. Per-index overrides also do not
apply because a declared source has no serving index.

## A declared platform forbids host builds

A PEP 517 backend reports the host's dependencies. Any declared platform
or implementation forbids host builds, even when it names the host.

`build-policy` is forced to `never`; an explicit non-`never` value is a
config error before resolution. This covers both ways to declare a
machine:

* `[tool.nab.environment]` with a `platform` or an `implementation`.
* `mode = "universal"`, where every matrix target declares one.

This matches pip, which requires `--only-binary=:all:` under `--platform`,
`--abi`, or `--implementation`.

A retarget of the Python axis alone (`[tool.nab.environment].python`,
or `--python X.Y`) is different: the machine is still the host.  nab warns
that a build would report the host interpreter's metadata, and permits it.

This is a deliberate deviation from pip: the machine is still the host, so a
build can run at all, and refusing every one of them would take the default
case with it.  Set `build-policy = "never"` to forbid it.
