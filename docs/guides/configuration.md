# Configuration

All project shape lives in `[tool.nab]` inside the project's
`pyproject.toml`.  The CLI is intentionally narrow: it carries only
runtime knobs (cache directory, offline mode, HTTP backend).

## Top-level keys

```toml
[tool.nab]
# "specific" (default) or "universal" (experimental, opt-in).
mode = "specific"

# PEP 508 constraint strings.  Bound versions but never pull packages
# into the resolution.
constraints = ["urllib3<2"]

# Dependency-group names recorded in the lockfile's
# default-groups array.
default-groups = ["dev"]

# Override the host's Python for a single-environment resolve.
# Single value, not a range; for multi-Python locking use the
# [tool.nab.matrix] table below.
requires-python = "3.12.0"

# Reproducibility cutoff.  Distributions uploaded after this timestamp
# are ignored.  Accepts ISO 8601 strings, native TOML datetimes, or a
# "P<n>D" duration relative to the resolve anchor.
uploaded-prior-to = "2026-05-01T00:00:00Z"

# Version selection within an allowed range.  Mirrors uv's --resolution.
resolution = "highest"          # "highest" | "lowest" | "lowest-direct"

# Distribution policy.  See "Dist policy" below for details.
dist-policy = "wheel-or-sdist"
# "wheel-only" | "prefer-wheel" | "wheel-or-sdist" | "sdist-only" | "sdist-install"

# PEP 517 build policy.
build-policy = "build-local"    # "never" | "build-local" | "build-remote"
```

The global `dist-policy` may instead be a table that folds in the
sdist-trust flag (off by default; trusting a pre-PEP-643 sdist's
PKG-INFO dependencies skips the dynamic-metadata path):

```toml
[tool.nab]
dist-policy = { policy = "wheel-or-sdist", trust-unverified-deps = false }
```

## Marker environment overlay

`[tool.nab.marker-environment]` impersonates a non-host environment
for a single-environment resolve.  Each key overrides the corresponding
PEP 508 marker variable; keys not listed keep their host value.

```toml
[tool.nab.marker-environment]
platform_system = "Linux"
sys_platform = "linux"
platform_machine = "x86_64"
```

Setting this overlay requires `build-policy = "never"` at the global
level and in every override that sets `build-policy`: a PEP 517 backend
runs on the host, so a build cannot reflect the impersonated target.
See [Build policy](build-policy.md) for the same rule under universal
mode.

## Indexes

`[[tool.nab.indexes]]` declares the ordered list of named package
indexes.  Order is significant: nab consults them left to right and
takes the *first* one that lists a given package (presence-based,
matching uv's `--index-strategy first-index` default).

```toml
[[tool.nab.indexes]]
name = "pypi"
url  = "https://pypi.org/simple/"

[[tool.nab.indexes]]
name = "torch-cpu"
url  = "https://download.pytorch.org/whl/cpu"
```

When `[[tool.nab.indexes]]` is omitted entirely, nab defaults to a
single PyPI entry.

## Overrides

Two surfaces scope policy to a subset of packages.  One is keyed by
*package* (`[tool.nab.packages.<name>]` and `[[tool.nab.package-rules]]`),
the other by *index name* (`[tool.nab.index.<name>]`).  The flat
top-level keys remain the global defaults; an override narrows them.

### Per-package overrides

A per-package override is a policy body applied to a requirement.  It can
be written two ways, and both may appear in one file.

The name-keyed table is the terse form for a single package:

```toml
# Build lxml from source and trust its (pre-PEP-643) PKG-INFO deps.
[tool.nab.packages.lxml]
dist-policy = { policy = "sdist-only", trust-unverified-deps = true }

# Quote the key to carry a version specifier on the requirement.
[tool.nab.packages."numpy > 2"]
dist-policy = "wheel-only"
```

`[[tool.nab.package-rules]]` is an array whose `match` selector lists the
requirements one body applies to.  The same overrides written as rules:

```toml
[[tool.nab.package-rules]]
match = ["lxml"]
dist-policy = { policy = "sdist-only", trust-unverified-deps = true }

# A match entry takes the same name-plus-specifier requirement.
[[tool.nab.package-rules]]
match = ["numpy > 2"]
dist-policy = "wheel-only"
```

A rule is the form to reach for when one body covers several packages at
once, most often routing a set of internal packages to one index:

```toml
[[tool.nab.package-rules]]
match = ["acme-core", "acme-plugins", "acme-utils"]
index = "internal"
```

To scope an override to a range of versions, put a PEP 508 specifier in
the (quoted) table key or in a `match` entry.  Two non-overlapping ranges
for one package are two entries, in either form:

```toml
# Both forms take specifiers and mix in one file: old numpy as sdists,
# newer numpy as wheels.
[tool.nab.packages."numpy <= 1.21"]
dist-policy = "sdist-only"

[[tool.nab.package-rules]]
match = ["numpy >= 1.22"]
dist-policy = "wheel-only"
```

A body sets any combination of:

* `dist-policy`: an enum string, or `{ policy = "...",
  trust-unverified-deps = true|false }`.
* `build-policy`: an enum string.
* `uploaded-prior-to`: a datetime, a `P<n>D` duration, or `false` (no
  cutoff for the selected versions).
* `index`: route the selected packages to this declared index (a
  strict pin: only that index is consulted).  Routing requires
  bare-name selectors, because the routing decision happens before any
  version is known; a version specifier alongside `index` is rejected.

A selector is a name plus an optional version specifier, with no extras,
marker, or URL.  Package names are canonicalised, so `Foo-Bar`,
`foo_bar`, and `foo-bar` name the same package; two entries that set the
same field for it are an overlap error (below).

The version ranges of two per-package entries (from either form) that
set the **same field** for the **same package** must not overlap.
Overlapping ranges are a parse-time error rather than a precedence call:

```toml
# ERROR: <= 2 and >= 1 overlap on [1, 2], and both set dist-policy.
[tool.nab.packages."lxml <= 2"]
dist-policy = "sdist-only"

[tool.nab.packages."lxml >= 1"]
dist-policy = "wheel-only"
```

By that guarantee, at most one per-package entry governs a given
(package, version) for a given field.  Two routes for one package always
overlap (routing needs the full range), so a package may have at most
one route.

### Per-index overrides

`[tool.nab.index.<name>]` is keyed by a declared index name.  Each entry
sets policy only (`dist-policy`, `build-policy`, `uploaded-prior-to`) and
applies to every package served from that index.  It carries no routing
and no version scope.

```toml
# Everything served from PyPI is wheel-only.
[tool.nab.index.pypi]
dist-policy = "wheel-only"

# A longer body.
[tool.nab.index.internal]
build-policy = "build-remote"
uploaded-prior-to = "2026-05-01T00:00:00Z"
```

### Conflicts across the two surfaces

The per-package and per-index surfaces are not ranked.  For a candidate
`(package P, version V)` served from index `I` and a field `F`: if a
per-package entry whose range contains `V` sets `F` **and** the
per-index entry for `I` also sets `F`, the resolve raises a clear
error instead of silently choosing one.  Drop one of the two settings
for that field.  The same package at a version *outside* the
per-package range is governed only by the per-index entry, with no
conflict.

When no override sets a field, the flat global value (then the built-in
default) applies.

## Dist policy

`[tool.nab].dist-policy` controls which artifact kinds the resolver
considers and which end up in the lockfile.  The default
`wheel-or-sdist` treats wheels and sdists symmetrically.

| Value | Wheel admitted to resolve? | Sdist admitted to resolve? | What ends up in the lock |
|---|---|---|---|
| `wheel-only` | yes | no | wheel |
| `prefer-wheel` | yes (preferred) | yes (fallback) | whichever was used |
| `wheel-or-sdist` (default) | yes | yes | both |
| `sdist-only` | no | yes | sdist |
| `sdist-install` | yes | yes | sdist |

`wheel-only` is the equivalent of pip's `--only-binary :all:` and
`sdist-only` of `--no-binary :all:`, scoped per package or per index
via an override.

`sdist-install` is the policy to reach for when an installer needs
to build the package from source (typically to link against
system libraries like libxml2, libxmlsec, or system OpenSSL), but you
do not want to pay the cost of building it twice (once during the
resolve to learn its dependencies, once at install time).  The
lockfile pins only the sdist so `pip install --require-hashes`
materialises that archive; the resolver, meanwhile, reads
dependency metadata from whichever source is cheapest at the
chosen version.  In practice that means the wheel's METADATA via
PEP 658 when one is published, falling back to the sdist's
PKG-INFO (with the usual PEP 643 and `pyproject.toml` fallbacks)
when no wheel exists.  Equivalent in spirit to pip's
`--no-binary <pkg>` for the install side, without paying the
build cost on the resolve side.

Scope the policy to a subset of packages with a per-package override:

```toml
[[tool.nab.package-rules]]
match = ["lxml", "xmlsec"]
dist-policy = "sdist-install"
```

The same five values are accepted.  Package names are canonicalised, so
`Foo-Bar`, `foo_bar`, and `foo-bar` name the same package.

## VCS policy

`[tool.nab.vcs]` controls whether direct-URL VCS requirements
(`pkg @ git+https://...`) are honored.  Default posture is fully
restrictive.

```toml
[tool.nab.vcs]
policy = "block"                # "block" | "allow"
allowed-schemes = ["git+https"]
allowed-repos = ["github.com/me/x"]
require-pin = true
```

## Local checkouts as sources

`[[tool.nab.local-sources]]` treats a directory on disk as the only
candidate for the named package.

```toml
[[tool.nab.local-sources]]
name = "my-fork"
path = "../my-fork"
```

Two optional keys are accepted.  `editable` (boolean, default
`false`) records a PEP 660 editable install in the lockfile.
`subdirectory` locates the package below `path`, for monorepo
layouts.

Reading static dependencies from a local pyproject.toml works at
every `build-policy` level.  Dynamic dependencies require
`build-policy = "build-local"` or `"build-remote"`.

## Pinned VCS sources

`[[tool.nab.vcs-sources]]` pins a package to a VCS URL.  Requires
`vcs.policy = "allow"`; reading static dependencies works at any
`build-policy`.  Dynamic dependencies on a VCS clone require
`build-policy = "build-remote"`.

```toml
[[tool.nab.vcs-sources]]
name = "my-fork"
url  = "git+https://github.com/me/x.git@<sha>"
```

## Universal mode (experimental)

Universal resolution runs the single-environment resolver once per
declared `(python, platform)` tuple, sharing one fetcher so metadata
is fetched at most once per package.  Output and API are still subject
to change.

```toml
[tool.nab]
mode = "universal"

[tool.nab.matrix]
python = ">=3.11,<3.14"
platforms = ["linux_x86_64", "macos_arm64"]
python-order = "asc"            # "asc" | "desc"
python-patches = { "3.11" = "3.11.4" }
```

`python-order` controls cross-tuple alignment direction (`asc` mirrors
uv's `fork-strategy=fewest`; `desc` mirrors `fork-strategy=
requires-python`).  `python-patches` overrides the per-minor
`python_full_version` marker value for marker evaluation.

Each tuple impersonates a platform, so universal mode cannot build on
the host: `build-policy` defaults to `never` and cannot be raised.  An
explicit non-`never` value (global or in any override) is a config
error.  See [Build policy](build-policy.md).

### `requires-python` vs `[tool.nab.matrix].python`

The two `python` knobs cover different shapes of resolve:

* `[tool.nab].requires-python`: a single Python version (or a bare
  specifier like `>=3.12.0`).  Treated as a host-environment
  override for a single-environment resolve.  Mostly useful when
  you need to lock against a different Python than the one running
  nab and you only need one lock.
* `[tool.nab.matrix].python`: a range like `>=3.11,<3.14`, expanded
  into one tuple per minor version.  Used only by universal mode.
  Pair with `[tool.nab.matrix].platforms` and (optionally)
  `[tool.nab.matrix].python-patches` to control the resolve and
  marker shape across all tuples.

If both are present, the matrix `python` wins under universal mode
and `requires-python` wins under specific mode.  Pick one shape
based on whether you want one lock or many.

## CLI flags (runtime only)

```
nab lock [PATH]
  --output PATH           # output file (or "-" for stdout)
  --format FORMAT         # pylock | requirements | requirements-without-hashes
  --cache-dir PATH        # override on-disk cache location
  --no-cache              # disable cache for this run
  --offline               # use cache only, no network
  --http-backend X        # urllib3 (default) | httpx
```

Anything that defines *what* gets resolved goes in `[tool.nab]`; the
CLI only carries knobs about *how this run executes*.
