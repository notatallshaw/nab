# Configuration

The keys that decide what nab resolves live in `[tool.nab]` inside the
project's `pyproject.toml`, or in a project-directory `nab.toml` that
sets the same keys.  The CLI carries runtime knobs (cache directory,
offline mode, HTTP backend), and can also override a project key for a
single run with a `--project-<key>` flag.

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
# A PEP 440 specifier, not a bare version; for multi-Python
# locking use the [tool.nab.matrix] table below.
requires-python = "==3.12.0"

# Reproducibility cutoff.  Distributions uploaded after this timestamp
# are ignored.  Accepts ISO 8601 strings, native TOML datetimes, or a
# "P<n>D" duration relative to the resolve anchor.  Artifacts from a
# local file:// index carry no upload time and are always kept.
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
* `dependencies`: a list of PEP 508 requirement strings that replaces
  the package's declared runtime dependencies (see below).
* `requires-python`: a PEP 440 specifier that replaces the package's
  declared Python requirement for the selected versions (see below).
* `provides-extra`: a list of extra names that replaces the package's
  declared extras for the selected versions (see below).

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

#### Overriding a package's metadata

`dependencies`, `requires-python`, and `provides-extra` substitute for
what nab would otherwise read from a distribution's metadata, scoped to
the selected version range.  Together they mirror uv's
`dependency-metadata`: you state a package's metadata directly instead of
fetching or building it.  Use them when a package's published metadata is
wrong, absent, dynamic, or unbuildable on your target.  You take
responsibility for correctness: nab trusts the values as written and does
not verify them against the artifact.

Each field replaces its own field independently, so a `dependencies`
override on one range and a `requires-python` override on another
(overlapping) range coexist; only two entries setting the **same** field
over overlapping ranges are an error.

##### `dependencies`

`dependencies` states the runtime dependencies for the selected
versions, replacing whatever the distribution declares.  Each item
is a full PEP 508 requirement, so extras, markers, and version
specifiers are all allowed on a value (unlike the selector key, which
takes only a name and an optional specifier).

```toml
# Replace chumpy's declared runtime deps for every version.
[tool.nab.packages.chumpy]
dependencies = ["numpy>=1.8.1", "scipy>=0.13.0", "six>=1.11.0"]

# Version-scoped: only versions <= 1.0.
[tool.nab.packages."broken-pkg <= 1.0"]
dependencies = ["requests>=2"]

# The many-packages spelling.
[[tool.nab.package-rules]]
match = ["some-pkg <= 2.0"]
dependencies = ["requests>=2"]
```

The list is the complete replacement, not an addition: the declared
dependencies for the matched versions are dropped and the override's
list is used instead.  An empty list removes all runtime dependencies:

```toml
# Resolve broken-pkg <= 1.0 with no runtime dependencies at all.
[tool.nab.packages."broken-pkg <= 1.0"]
dependencies = []
```

An empty list is distinct from omitting the key: the key absent means
the declared dependencies stand, while `[]` means "replace with zero
dependencies."  Both count as setting the field, so both take part in
the same-field overlap rule above (two entries setting `dependencies`
over overlapping ranges are a parse-time error).

##### `requires-python`

`requires-python` is a single PEP 440 specifier that replaces the
package's declared Python requirement for the selected versions.  A bare
version like `"3.13"` is rejected (it is not a specifier); write
`">=3.13"` or `"==3.13"`.

```toml
[tool.nab.packages.flask]
requires-python = ">=3.6"
```

The override applies at the point that filters candidates by Python, so
it both widens and narrows:

* Widen: a package that declares `>=3.10` but actually runs on `3.9` can
  be admitted for a `3.9` resolve with `requires-python = ">=3.9"`.
* Narrow: a package that declares `>=3.6` can be held to `>=3.11` so
  older Pythons reject it.

The override goes through the same comparison a declared
`Requires-Python` value takes (the full target Python version against
the specifier), so it admits or rejects a version exactly as a declared
value would.  An empty string (`requires-python = ""`) removes
the Python requirement entirely (admits every target).  For an index pin
the lock records the overridden specifier, so a widened pin stays
installable by a conforming PEP 751 installer.  A local-path or VCS pin
has no `requires-python` field, but the override is still what its Python
check enforces.

##### `provides-extra`

`provides-extra` is the list of extra names the package declares for the
selected versions, normalised per PEP 685 (so spelling does not matter).

```toml
[tool.nab.packages.flask]
dependencies = ["werkzeug>=0.14", "click>=5.1 ; extra == 'dotenv'"]
provides-extra = ["dotenv"]
```

When `provides-extra` is set it is authoritative for the whole extra set:
it is never merged with the package's declared extras.  An extra then
exists iff `provides-extra` lists it, and its dependencies are exactly
the dependency lines carrying that extra's `; extra == "name"` marker
(the override's `dependencies` when set, else the parsed
`Requires-Dist`).

* When `provides-extra` is absent, the extras fall back to the package's
  parsed extras, unless `dependencies` is also replaced.  A
  `requires-python`-only override therefore keeps the package's declared
  extras and their dependencies.  Replacing `dependencies` without
  declaring `provides-extra` drops the extras, since the parsed
  extra-gated dependency lines are gone once the list is replaced.
* A dropped extra has no effect unless it is requested.  Requesting a
  dropped extra directly (a root/user `flask[async]`) raises in the
  error-user and backtrack extras modes; a transitive request (another
  package depending on `flask[async]`) warns and drops the extra's
  dependencies, except in the backtrack mode, which skips versions
  that lack the extra instead.
* An empty list (`provides-extra = []`) declares no extras and is
  distinct from omitting the key.

##### Skip-fetch: resolving without the artifact

When an entry sets `dependencies` (an empty list counts), nab resolves
the matched versions from the declared metadata alone: the resolver
computes their dependencies without fetching or building the per-version
metadata.  This is what lets a package that nab cannot fetch or
build (an sdist-only or dynamic-metadata package under
`build-policy = "never"`) still resolve:

```toml
# Resolve a dynamic-metadata, sdist-only package without building it.
[tool.nab.packages.legacy-pkg]
dependencies = ["numpy>=1.8.1"]
```

The package listing is still fetched (the lock needs the files, hashes,
and upload times, and the Python filter still runs), but the resolver
does not fetch or build the per-version metadata to compute
dependencies.  A partial override that sets only `requires-python`
still needs the artifact for its dependencies and does not skip.  Under
skip-fetch a co-set `trust-unverified-deps` becomes moot, since no
sdist is parsed.  A co-set `dist-policy` is not: it still filters the
candidate listing, so `dist-policy = "wheel-only"` on an sdist-only
package removes every version before the override can apply.

##### Scope and the per-index rule

These are per-package fields with no per-index form: an index serves many
packages, so a single dependency list, Python requirement, or extra set
for all of them is meaningless.  Writing any of them under
`[tool.nab.index.<name>]` is rejected.

A metadata override annotates versions the resolve already reaches; it
never introduces a version.  An override scoped to a version no candidate
has, or a package the resolve never visits, does nothing.  Local-path and
VCS sources have no listing and their version is not known until the
source is materialised, so a metadata override governs a source only when
it uses a bare-name selector (full range); a version-scoped override does
not match a source.

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

## Archive sources

`[[tool.nab.archive-sources]]` pins a package to a direct `.tar.gz`
URL.  The URL fragment must carry a `sha256` (or `sha384`/`sha512`)
hash; nab downloads the archive, verifies it against the hash, and
fails the resolve on a mismatch.

```toml
[[tool.nab.archive-sources]]
name = "my-fork"
url  = "https://example.com/my-fork-1.0.tar.gz#sha256=<hex>"
```

Only `.tar.gz` source archives are supported; a wheel or other
format is refused at parse.  A `&subdirectory=` fragment locates the
package below the archive root, for monorepo layouts.  Reading static
dependencies works at any `build-policy`; dynamic dependencies require
`build-policy = "build-remote"`, like a remote sdist.

The URL is read by its own scheme, whatever the configured indexes use:
a `file://` archive is read from disk, and any other archive is fetched
over HTTP.

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

### Platform tag knobs

A `platforms` entry is either a bare platform id, which takes that
platform's defaults, or a table declaring the wheel-tag knobs:

```toml
[tool.nab.matrix]
python = ">=3.11,<3.14"
platforms = [
    "windows_amd64",
    { id = "linux_x86_64", libc = "musl", libc-version = "1.2" },
    { id = "macos_arm64", macos-min = "14.0" },
    { id = "linux_aarch64", platform-release = "5.15.0" },
]
```

| Key | Default | Meaning |
| --- | --- | --- |
| `id` | required | `linux_x86_64`, `linux_aarch64`, `macos_arm64`, `macos_x86_64`, or `windows_amd64` |
| `libc` | `"glibc"` | The Linux C library: `"glibc"` or `"musl"` |
| `libc-version` | glibc `2.28`, musl `1.2` | The libc version the target runs |
| `macos-min` | arm64 `12.0`, x86_64 `10.13` | The macOS deployment target |
| `platform-release` | `""` | The `platform_release` marker value |
| `platform-version` | `""` | The `platform_version` marker value |
| `free-threaded` | `false` | Target the free-threaded (`cp3XXt`) CPython build |

A machine links one C library, so a target accepts one family's wheels:
a `glibc` target takes manylinux wheels and never musllinux ones, and a
`musl` target the reverse.  `libc-version` is the version the target
guarantees, and a wheel built against an older libc runs on a newer one,
so the target accepts every version at or below it.  A higher value
therefore accepts more wheels, not fewer.  The glibc default is 2.28,
the `manylinux_2_28` build image; a target that runs a newer glibc
should say so, because a wheel built above 2.28 is otherwise rejected.
Its major has to match the family (glibc `2.x`, musl `1.x`): those are
the only majors either has shipped, so no wheel targets another.

`macos-min` reads the same way, as the newest macOS a wheel may target.
Below the oldest macOS the architecture ever ran (10.4 on x86_64, 11.0
on Apple Silicon) there is no machine to model and no tag to name, so
that is a config error.

A knob belongs to its platform.  `libc` and `libc-version` are Linux
knobs and `macos-min` is a macOS one; declaring one on a platform that
cannot read it is a config error, not a target carrying a setting that
selects no wheel and still names the machine in the lock.

`free-threaded` picks the `cp3XXt` ABI, so the target takes the
free-threaded wheels and neither the ordinary `cp3XX` ones nor `abi3`
(a free-threaded interpreter cannot load either).  It needs CPython
3.13 or newer, the first release with a free-threaded build; a matrix
that admits an older minor, or a non-CPython implementation, is a
config error.

An id may appear once.  A lockfile entry is selected by a PEP 508
marker, and PEP 508 has no variable for the libc family or the
free-threaded build, so two targets sharing an id would render the same
marker and the lock could not tell their pins apart.  Locking a second
libc family, or a free-threaded build alongside the GIL one, is a
second lock run with its own config and output file.

Each tuple impersonates a platform, so universal mode cannot build on
the host: `build-policy` defaults to `never` and cannot be raised.  An
explicit non-`never` value (global or in any override) is a config
error.  See [Build policy](build-policy.md).

### `requires-python` vs `[tool.nab.matrix].python`

The two `python` knobs cover different shapes of resolve:

* `[tool.nab].requires-python`: a PEP 440 specifier like `==3.12.0`
  or `>=3.12`.  Treated as a host-environment
  override for a single-environment resolve.  Mostly useful when
  you need to lock against a different Python than the one running
  nab and you only need one lock.
* `[tool.nab.matrix].python`: a range like `>=3.11,<3.14`, expanded
  into one tuple per minor version.  Used only by universal mode.
  Pair with `[tool.nab.matrix].platforms` and (optionally)
  `[tool.nab.matrix].python-patches` to control the resolve and
  marker shape across all tuples.

Declaring a `[tool.nab.matrix]` table while `mode` is `specific` is an
error: the matrix is the universal resolver's input, so leaving mode
behind is almost always an oversight rather than an intent.

The one exception is an explicit `--project-mode specific` on the
command line.  A CLI override outranks the `[tool.nab]` table, so it
selects a single-environment resolve for that run and the declared
matrix does not apply; `requires-python` then chooses the Python to
resolve against.  This is how a universal project takes one
single-environment lock without editing its `pyproject.toml`:

```bash
nab lock --project-mode specific --project-requires-python "==3.13.*"
```

## CLI flags

```
nab lock [PATH]
  --output PATH                # output file (or "-" for stdout)
  --format FORMAT              # pylock | requirements | requirements-without-hashes
  --cache-dir PATH             # override on-disk cache location
  --no-cache                   # disable cache for this run
  --offline True|False         # use cache only, no network (or bare --offline / --no-offline)
  --http-backend X             # urllib3 (default) | httpx (layered)
  --locked                     # verify the committed pylock is current; write nothing
  --upgrade                    # re-anchor the P<n>D window to now
  --project-<key> VALUE        # override a project option for this run
```

A project option can be overridden for a single run with a
`--project-<key>` flag (for example `--project-resolution`,
`--project-dist-policy`, or the repeatable `--project-constraint`); the
structured table options stay file-only. A `--project-*` override changes
the resolved set, so it prints a notice and is recorded in the lockfile.
`--project-dist-policy` takes a bare policy, so it replaces the whole
`dist-policy` value and resets `trust-unverified-deps`; set the table form
in a file to keep that flag.

The CLI carries knobs about *how this run executes*; the one exception
is a `--project-*` flag, which overrides a *what-gets-resolved* key for
the single run.

## Layered configuration sources

A few options can be set from more than one place. Each option has a
scope that fixes where it may come from:

* Project-scope options describe the resolve itself, so they live with
  the project. Every `[tool.nab]` key is project-scope (`mode`,
  `indexes`, `constraints`, `vcs`, `workspace`, `dist-policy`,
  `build-policy`, `marker-environment`, `conflicts`, `matrix`,
  `packages`, `resolution`, and the rest), and each may be set in
  either `pyproject.toml`'s `[tool.nab]` or a project-directory
  `nab.toml`. They are never read from a user/system file or an
  environment variable. Each project option with a scalar or list form
  also takes a CLI override under a `--project-` prefix (for example
  `--project-resolution`); the structured table options stay file-only.
* User-scope options (`offline`, `cache-dir`, `http-backend`,
  `max-concurrency`) describe how a run executes on this machine, so they
  may come from a system, user, or project `nab.toml`, a `NAB_*`
  environment variable, or the CLI. They are rejected in
  `pyproject.toml`'s `[tool.nab]`, which is project-scope only.

Sources are consulted low to high; a higher source wins:

1. built-in default
2. system `nab.toml` (`/etc/nab/nab.toml`)
3. user `nab.toml` (`$XDG_CONFIG_HOME/nab/nab.toml`, else
   `~/.config/nab/nab.toml`)
4. `pyproject.toml` `[tool.nab]` and the project-directory `nab.toml`
   (same precedence; setting one key to conflicting values across the two
   files is a hard error, identical values are fine, and array options
   from both files concatenate)
5. `NAB_*` environment variables
6. the CLI flag

The standalone `nab.toml` files use the same key names as
`[tool.nab]`, but at the top level (no `[tool.nab]` table):

```toml
# ~/.config/nab/nab.toml
offline = true
cache-dir = "/fast/disk/nab-cache"
```

A project-directory `nab.toml` may set any project-scope key the same
way, so it can drive the resolve without touching `pyproject.toml`:

```toml
# nab.toml next to pyproject.toml
mode = "universal"
constraints = ["urllib3<2"]

[[indexes]]
name = "internal"
url = "https://pypi.example.com/simple/"
```

Project-directory discovery is the directory of the `pyproject.toml`
only; there is no walk-up.

`nab config` reports the *configured* value for each key. Two keys take
a value derived from other config at resolve time, so the resolved
value can differ from what `nab config` shows: `build-policy` floors to
`never` under `mode = "universal"` (and promotes workspace members to
`build-local`), and `local-sources` gains the workspace members found
by discovery. `nab config` shows the value as written, like
`git config` reporting configured rather than derived state.

### Environment variables

| Variable | Option | Effect |
| -------- | ------ | ------ |
| `NAB_OFFLINE` | `offline` | `1`/`0`/`true`/`false`. |
| `NAB_CACHE_DIR` | `cache-dir` | Cache root path. |
| `NAB_HTTP_BACKEND` | `http-backend` | `urllib3` or `httpx`. |
| `NAB_MAX_CONCURRENCY` | `max-concurrency` | Parallel HTTP fetches for `nab download` (at least `1`). |

A `NAB_*` name that is not one of these (a typo, or `NAB_RESOLUTION`
for a project-scope option) is an error rather than being silently
ignored. Run `nab config list` to see every effective value and where
it came from.
