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

# Override the host's Python for a single-environment resolve.
# Single value, not a range; for multi-Python locking use the
# [tool.nab.matrix] table below.
requires-python = "3.12.0"

# Reproducibility cutoff.  Distributions uploaded after this timestamp
# are ignored.  Accepts ISO 8601 strings or native TOML datetimes.
uploaded-prior-to = "2026-05-01T00:00:00Z"

# Distribution policy.  See "Dist policy" below for details.
dist-policy = "wheel-or-sdist"
# "no-sdist" | "prefer-binary" | "wheel-or-sdist" | "sdist-only" | "sdist-install"

# PEP 517 build policy.
build-policy = "build-local"    # "never" | "build-local" | "build-remote"
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

A marker overlay requires `build-policy = "never"` at the global level
and in every per-package override; invoking a backend on the host
produces metadata that does not match the impersonated target.

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

## Per-package index routing

`[[tool.nab.index-overrides]]` pins a package to one named index.  The
named index becomes the *only* source consulted for that package; nab
will not fall through to the global ordering on a miss.

```toml
[[tool.nab.index-overrides]]
name   = "torch"
index  = "torch-cpu"
marker = "platform_machine == 'x86_64'"
```

`marker` is optional.  Multiple entries for the same `name` are
evaluated in declaration order; the first whose marker holds wins.

## Dist policy

`[tool.nab].dist-policy` controls which artifact kinds the resolver
considers and which end up in the lockfile.  The default
`wheel-or-sdist` treats wheels and sdists symmetrically.

| Value | Wheel admitted to resolve? | Sdist admitted to resolve? | What ends up in the lock |
|---|---|---|---|
| `no-sdist` | yes | no | wheel |
| `prefer-binary` | yes (preferred) | yes (fallback) | whichever was used |
| `wheel-or-sdist` (default) | yes | yes | both |
| `sdist-only` | no | yes | sdist |
| `sdist-install` | yes | yes | sdist |

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

Per-package overrides live under `[tool.nab.dist-policy-package]`:

```toml
[tool.nab.dist-policy-package]
lxml = "sdist-install"
xmlsec = "sdist-install"
```

The same five values are accepted.  A per-package value overrides
the global; package names are canonicalised so `Foo-Bar`,
`foo_bar`, and `foo-bar` collapse to one entry.

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
  --http-backend X        # urllib3 (default) | httpx | niquests
```

Anything that defines *what* gets resolved goes in `[tool.nab]`; the
CLI only carries knobs about *how this run executes*.
