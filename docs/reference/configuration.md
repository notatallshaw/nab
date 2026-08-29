# Configuration

Project settings live in `[tool.nab]` inside `pyproject.toml` or at the
top level of a project-directory `nab.toml`. CLI flags apply to one run.

## Find the effective value

```bash
nab config list
nab config explain resolution
```

`list` shows every option, value, and winning source. Use `explain` when
a value is surprising; it shows the sources that lost too.

Project options describe the resolve. Higher sources replace whole
values rather than merging them.

Runtime options such as `offline` and `cache-dir` may also come from
system or user files and `NAB_*` variables. The Layered configuration
sources section below gives the complete order and scope rules.

## Top-level keys

```toml
[tool.nab]
# "specific" (default) or "universal" (opt-in; its multi-target
# lockfile format is experimental).
mode = "specific"

# Constraint strings: a package name with an optional version
# specifier and optional marker.  Bound versions but never pull
# packages into the resolution, so extras and direct-reference URLs
# are rejected.
constraints = ["urllib3<2"]

# PEP 735 dependency groups activated on every resolve, unioned
# with any --groups selection: their dependencies are resolved
# and pinned into the lock.  The names are also recorded in the
# lockfile's default-groups array.  Two members of the same
# at-most-one or exactly-one set in [tool.nab].conflicts cannot
# both be defaults.  A --groups selection that adds a second
# member of such a set to a default forks the run into one
# resolve per member.
default-groups = ["dev"]

# The group name a lock gives the project's own [project.dependencies],
# recorded in both of the lockfile's group arrays.  Unset, they carry no
# marker and install under every selection, so a lock offering groups
# cannot be asked for one group without them.  The name must not be one
# the file being locked declares in [dependency-groups].  It joins the
# lockfile's default-groups only when default-groups above is unset;
# declare it there too to keep it in the default selection.
base-group = "base"

# The group name a lock gives [build-system].requires, so one lock can
# describe the environment the project is built in as well as the one it
# runs in.  Unset, a lock says nothing about how the project is built.
# Set it and those requirements are resolved alongside the project's own,
# and a pylock gates them behind that name, which it records in
# dependency-groups but not in default-groups: an install that asks for
# no group is installing the project, not building it.  The requirements
# output formats carry no markers, so there they render as ordinary
# pins.  The name must not be one [dependency-groups] declares, nor the
# base-group name, and base-group must be set: unnamed, the project's
# own dependencies carry no marker and come with every group, so nothing
# could ask for the build requirements alone.  Either name may be a
# [tool.nab].conflicts member, which forks the resolve so each side gets
# its own pins.
build-group = "build"

# The Python range this project supports.  A declaration: it is
# recorded in the lockfile and checked against the resolve target,
# and it does not choose that target.  Falls back to
# [project].requires-python.  A PEP 440 specifier, not a bare version.
requires-python = ">=3.10"

# Reproducibility cutoff.  Distributions uploaded after this timestamp
# are ignored.  Accepts an ISO 8601 string or a native TOML datetime,
# each with an explicit timezone offset, or a "P<n>D" duration relative
# to the resolve anchor.  Artifacts from a local file:// index carry no
# upload time and are always kept.  An HTML listing rarely carries one
# either, and those files are then excluded; see "Serialization" below.
uploaded-prior-to = "2026-05-01T00:00:00Z"

# Version selection within an allowed range.  Mirrors uv's --resolution.
resolution = "highest"          # "highest" | "lowest" | "lowest-direct"

# Whether listings that have already arrived may steer the decision
# order.  "stable" waits for the listing instead, so the resolve does
# not depend on how warm the HTTP cache was.  Costs wall time.  See
# "Decision order" below.
decision-order = "arrival"      # "arrival" | "stable"

# Distribution policy.  See "Dist policy" below for details.
dist-policy = "wheel-or-sdist"
# "wheel-only" | "prefer-wheel" | "wheel-or-sdist" | "sdist-only" | "sdist-install"

# PEP 517 build policy.
build-policy = "build-local"    # "never" | "build-local" | "build-remote"

# Build environments nab may open beneath the first one, to build a
# build requirement with no installable wheel.  A non-negative integer.
build-requires-depth = 0
```

The global `dist-policy` may instead be a table that folds in the
sdist-trust flag (off by default; trusting a pre-PEP-643 sdist's
PKG-INFO dependencies skips the dynamic-metadata path):

```toml
[tool.nab]
dist-policy = { policy = "wheel-or-sdist", trust-unverified-deps = false }
```

## The resolve environment

nab resolves for the interpreter it is running on.  The host is the
target, like pip: the same lock command on a Python 3.14 machine
resolves the markers a Python 3.14 install evaluates.

`[tool.nab.environment]` retargets it.  The table declares one target's
axes, the same axes a matrix entry carries; every axis it leaves out
keeps the host's value:

```toml
[tool.nab.environment]
python = "3.10"             # a version, not a specifier ("3.10" or "3.10.5")
platform = "linux_x86_64"   # a matrix platform id
implementation = "cpython"  # "cpython" (default) or "pypy"
```

* `python` alone keeps the host machine and moves only the interpreter,
  which is what pip's `--python-version` does.  `nab lock --python 3.10`
  does it for one run, as does
  `nab lock --project-environment-python 3.10`.
* `platform` (with or without `implementation`) declares the machine, so
  the PEP 508 markers are synthesized from the platform id rather than
  read off the host.  `implementation` needs a `platform`: an interpreter
  is modelled on a declared machine, never on the host's.
* A declared platform forbids host builds, so `build-policy` must be
  `never`.  A python-only retarget warns and permits.  See
  [Build policy](build-policy.md).

`platform` takes the two shapes a `[tool.nab.matrix]` platform takes: a
bare id at that platform's default tag knobs, or a table declaring them.

```toml
[tool.nab.environment]
python = "3.13"
platform = { id = "macos_arm64", runs-on-macos = "14.0" }
```

The knobs, their defaults and their rules are the ones the matrix's
"Platform tag knobs" section below lists.  A bare id names no system, so
a wheel of any level is accepted; declare `runs-on-macos` to name a macOS
the lock must run on: the target then accepts that macOS and every older
tag and drops a wheel that needs a newer one.

The target declares both halves of the environment: its PEP 508 markers
gate every dependency, and its PEP 425 wheel tags gate every candidate.
A version whose only wheels the target cannot install, and which ships no
sdist, is not a candidate: the resolve fails on it rather than pinning a
wheel that will not install (`pywin32` on Linux).  The lockfile records
only the wheels the target can install.

Tags gate wheels only, so a `.tar.gz` sdist keeps a version alive for any
target.  nab drops every other sdist format when it parses the listing,
so a version whose only sdist is a `.zip` and which ships no wheel is not
a candidate either (`pyreadline==2.1`).

The resolve is still for one environment.  A lock made for
`linux_x86_64` is a lock for `linux_x86_64`; it says so in its PEP 751
`environments` (see [lockfiles](lockfile.md)), and a conforming installer
refuses it elsewhere.  To lock for several machines at once, use
`mode = "universal"`.

`[tool.nab.environment]` and `[tool.nab.matrix]` cannot both be set: the
matrix already declares one environment per target.  The rule holds
however each was declared, in a file or by a flag.

### `[tool.nab.marker-environment]` (deprecated)

The old overlay set PEP 508 marker variables one at a time, so a partial
declaration (`sys_platform` alone) left the rest of the machine on the
host and resolved for a machine that does not exist.  It is translated
to `[tool.nab.environment]` with a warning, and a key that names no
environment axis, or a `(sys_platform, platform_machine)` pair that names
no platform nab models, is a config error.  Declare the environment
instead.

`platform_release` and `platform_version` are knobs of the machine the
pair names, so they translate into the platform table
(`platform-release`, `platform-version`) and need the pair alongside
them.

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

[[tool.nab.indexes]]
name = "internal"
url  = "https://artifactory.example.com/api/pypi/pypi/simple/"
# Which Simple-API serialization to ask this index for.
serialization = "html"   # "negotiate" (default) | "json" | "html"
```

When `[[tool.nab.indexes]]` is omitted entirely, nab defaults to a
single PyPI entry.

### Serialization

By default nab negotiates: it advertises the PEP 691 JSON listing, the
PEP 691 HTML listing and PEP 503 `text/html`, and decodes whichever one
the index serves.  `serialization` pins a single choice, for an index
that does not answer both reliably.

The pin covers the `Accept` header and the decoder together.  A pinned
index that answers in the other serialization is an error: reading it
anyway would hide the behaviour the pin was set to stop.  An index that
holds only the other serialization and negotiates strictly answers 406,
which fails the same way.  Either error ends the resolve rather than
falling through to the next index in the list.

Listings fetched under one setting are not reused under another: a
pinned index keeps its own listing cache.

`serialization` is not settable on a `file://` index, `"negotiate"`
included.  A local index is read from disk with no `Accept`
negotiation, so the key is rejected rather than accepted and ignored.

Pinning `html` gives up the extra data PEP 700 defines for the JSON
listing.  Two of those losses matter:

* A PEP 503 page carries no upload time.  Unless the index emits the
  non-standard `data-upload-time` attribute, every file it serves is
  excluded once `uploaded-prior-to` applies.
* A page publishes its hashes in the URL fragment.  A file whose only
  fragment digest is md5 gives `nab lock` nothing to record, and the
  PEP 751 and requirements writers then fail on the missing hash.

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
* `uploaded-prior-to`: a datetime with an explicit timezone offset, a
  `P<n>D` duration, or `false` (no cutoff for the selected versions).
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

The override uses the same comparison as a declared `Requires-Python`
value.  That comparison is made at the language version: a micro segment
neither admits a target nor excludes one, so
`>=3.13.2`, `==3.13.4` and `==3.13.*` all admit a 3.13 target, while
`!=3.13` excludes every 3.13 interpreter.

An empty string (`requires-python = ""`) removes the Python requirement
and admits every target.

For an index pin the lock records the overridden
specifier, so a widened pin stays installable by a conforming PEP 751
installer, which enforces it in full.  A local-path or VCS pin
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
dependencies.  A partial override that sets only `requires-python` still
needs the artifact for its dependencies and does not skip.

Under skip-fetch a co-set `trust-unverified-deps` becomes moot, since no
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
has, or a package the resolve never visits, does nothing.  Local-path,
VCS, and archive sources have no listing and their version is not known
until the source is materialised, so a metadata override governs a source
only when it uses a bare-name selector (full range); a version-scoped
override does not match a source.

### Per-index overrides

`[tool.nab.index.<name>]` is keyed by a declared index name.  Each entry
sets policy only (`dist-policy`, `build-policy`, `uploaded-prior-to`,
`assume-fresh-seconds`) and applies to every package served from that
index.  It carries no routing and no version scope.

```toml
# Everything served from PyPI is wheel-only.
[tool.nab.index.pypi]
dist-policy = "wheel-only"

# A longer body.
[tool.nab.index.internal]
build-policy = "build-remote"
uploaded-prior-to = "2026-05-01T00:00:00Z"
# Trust this index's package listings as fresh for an hour.
assume-fresh-seconds = 3600
```

`assume-fresh-seconds` treats a cached package listing from this index as
fresh for at least that many seconds, skipping the revalidation a
short-lived server `max-age` would otherwise force.  It only extends
freshness, never shortens it, and reaches only the mutable listing: a
release published inside the window stays hidden until it lapses.  It
never affects the metadata and artifacts nab caches by hash.

### Conflicts across the two surfaces

Per-package and per-index overrides have equal precedence. If both set
the same field for a candidate version, the resolve fails instead of
choosing one. Remove one setting.

The same package at a version *outside* the per-package range is
governed only by the per-index entry, with no conflict.

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

nab drops every sdist format but `.tar.gz` when it parses an index
listing, before any policy sees it, so a version whose only sdist is a
`.zip` counts as shipping none.  A wheel-admitting policy sees only its
wheels, and `sdist-only` and `sdist-install` skip it, since both need an
sdist to lock.

`wheel-only` is the equivalent of pip's `--only-binary :all:` and
`sdist-only` of `--no-binary :all:`, scoped per package or per index
via an override.

A wheel without a PEP 658 sidecar still resolves under any
wheel-admitting policy. nab recovers its METADATA with an HTTP range
read. If the index cannot serve usable ranges, nab downloads the whole
wheel instead.

Use `sdist-install` when the lock must select an sdist, typically so an
installer can link the package against system libraries.

The lockfile pins only the sdist, so `pip install --require-hashes`
materialises that archive.

For resolution, nab prefers wheel metadata and falls back to the sdist
when no wheel exists. Dynamic sdist metadata can still require a build
allowed by `build-policy`.

A version with no sdist is skipped, as under `sdist-only`, and the
resolver settles on the newest version that ships one.

Scope the policy to a subset of packages with a per-package override:

```toml
[[tool.nab.package-rules]]
match = ["lxml", "xmlsec"]
dist-policy = "sdist-install"
```

The same five values are accepted.  Package names are canonicalised, so
`Foo-Bar`, `foo_bar`, and `foo-bar` name the same package.

## Decision order

`[tool.nab].decision-order` controls whether the listings that have
arrived so far may steer which package the resolver decides next.

nab fetches listings on a background thread while it resolves, and the
decision scan prefers packages whose listing has already landed so the
search keeps moving while the rest are in flight.  Which ones have
landed depends on what the HTTP cache held, so one project resolved
twice against one index can search differently, and on some inputs
settle on a different valid answer.

| Value | Behaviour |
|---|---|
| `arrival` (default) | Rank a package on the listings that have already landed. |
| `stable` | Wait for the listing, then rank on its real version count. |

Under `stable` nothing about fetch timing reaches the decision scan, so
one project resolved twice against one index gives one lockfile.  This
is the setting to reach for when `nab lock --locked` fails in CI on a
commit nobody changed.

It costs wall time: a few percent on nab's slower benchmark scenarios,
with a wider run-to-run spread even though the answer stops moving.  The
wait is usually on a fetch already in flight rather than one the scan
issues, so fetching is not serialised.

`stable` settles the resolver; the index can still move under you.  See
"Reproducibility" in the [lockfile reference](lockfile.md) for the
conditions that remain.

Decision order is not the only heuristic that picks among valid
answers.  nab also looks ahead before it decides: for a package
without extras it reads the candidate version's dependencies, and
skips that version when they already contradict a root requirement or
a version the resolve settled on elsewhere.

The look-ahead runs under both settings.  It reads metadata and the
decisions taken so far rather than arrival timing, so `stable` still
gives one lockfile.

A resolver that does not look ahead can pin a different version of one
project.  Both answers can satisfy every requirement.

## VCS policy

`[tool.nab.vcs]` is the gate a VCS URL passes before nab clones it.
Default posture is fully restrictive.  The form that resolves is a
`[[tool.nab.vcs-sources]]` entry, described under "Pinned VCS sources"
below.

A direct-URL requirement (`pkg @ git+https://...`) at the project root
or in a dependency's metadata passes the same gate, then fails the
resolve, because nab has no resolver path for that form.  A requirement
whose marker excludes it, or one behind an extra the resolve never
requests, never reaches the gate.  See
[Add a VCS dependency](../how-to/vcs.md).

```toml
[tool.nab.vcs]
policy = "block"                # "block" | "allow"
allowed-schemes = ["git+https"]
allowed-repos = ["https://github.com/me/x"]
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
`false`) records a PEP 660 editable install in the lockfile.  This
default is specific to `[[tool.nab.local-sources]]`; workspace
members discovered from `[tool.nab.workspace]` are recorded as
editable by default (see [Lock a workspace](../how-to/workspaces.md)).
`subdirectory` locates the package below `path`, for monorepo
layouts.

Reading static metadata from a local pyproject.toml works at every
`build-policy` level.  When the static read comes up empty, nab builds
the checkout instead, which needs `build-policy = "build-local"` or
`"build-remote"`.  See [Build policy](build-policy.md) for what counts
as empty.

## Pinned VCS sources

`[[tool.nab.vcs-sources]]` pins a package to a VCS URL.  Declaring one
while `vcs.policy` is left at its default `block` is a contradiction and
is rejected when the config is read, before any resolve starts.

Each URL passes the same `[tool.nab.vcs]` gate as a direct-URL
requirement. Beyond `vcs.policy = "allow"`, its scheme must be in
`vcs.allowed-schemes`, its repository in `vcs.allowed-repos`, and it
must pin a 40-char commit hash unless `vcs.require-pin = false`.  Both
allow lists are empty by default, so each denies every URL until
configured.

Reading static metadata works at any `build-policy`.  Building a clone
whose static read comes up empty needs `build-policy = "build-remote"`;
see [Build policy](build-policy.md).

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
metadata works at any `build-policy`.  Building an extracted tree whose
static read comes up empty needs `build-policy = "build-remote"`, like a
remote sdist; see [Build policy](build-policy.md).

The URL is read by its own scheme, whatever the configured indexes use:
a `file://` archive is read from disk, and any other archive is fetched
over HTTP.  The extracted tree is cached alongside the hashes it was
verified against, so a later resolve that declares those same hashes
reuses it and downloads nothing, `--offline` included.  Declaring a hash
the cached tree was not checked against downloads the archive again.

## Universal mode

Universal resolution resolves one target per declared
`(python, platform, implementation)` point, on the same engine a
single-environment resolve uses.  The targets share one fetcher: a
package's listing is read once for the whole matrix, a version's wheel
metadata once per wheel the targets pick, and an sdist's PKG-INFO once
for the version.  The multi-target lockfile format it produces is
experimental and may change without notice.

```toml
[tool.nab]
mode = "universal"

[tool.nab.matrix]
python = ">=3.11,<3.14"
platforms = ["linux_x86_64", "macos_arm64"]
implementations = ["cpython", "pypy"]   # default ["cpython"]
python-order = "asc"                    # "asc" | "desc"
python-patches = { "3.11" = "3.11.4" }
```

The matrix has three axes, and nab resolves the full cross product:
the example above declares 3 pythons, 2 platforms and 2
implementations, so it plans 12 targets.

| Key | Default | Meaning |
| --- | --- | --- |
| `python` | required | A PEP 440 range like `>=3.11,<3.14`, expanded into one target per minor |
| `platforms` | required | The platforms to model; a bare id, or a table of the tag knobs below |
| `implementations` | `["cpython"]` | The interpreter implementations to model: `"cpython"`, `"pypy"` |
| `python-order` | `"asc"` | Cross-target alignment direction |
| `python-patches` | none | Pins a minor to one patch release, resolved whole instead of split into slices |

`python-order` sets the alignment direction: `asc` mirrors uv's
`fork-strategy=fewest`, `desc` mirrors `fork-strategy=requires-python`.

### Interpreter implementations

`implementations` names the interpreters to model, and each entry
multiplies the target count.  An unknown implementation, a duplicate,
and an empty list are each a config error.  See
[Universal resolution](../explanation/universal.md) for how the axis is
modelled and what it puts on the lockfile markers.

### Platform tag knobs

A `platforms` entry is either a bare platform id, which takes that
platform's defaults, or a table declaring the wheel-tag knobs:

```toml
[tool.nab.matrix]
python = ">=3.11,<3.14"
platforms = [
    "windows_amd64",
    { id = "linux_x86_64", libc = "musl", runs-on-libc = "1.2" },
    { id = "macos_arm64", runs-on-macos = "14.0" },
    { id = "linux_aarch64", platform-release = "5.15.0" },
]
```

| Key | Default | Meaning |
| --- | --- | --- |
| `id` | required | `linux_x86_64`, `linux_aarch64`, `linux_i686`, `linux_armv7l`, `macos_arm64`, `macos_x86_64`, `windows_amd64`, or `windows_arm64` |
| `libc` | `"glibc"` | The Linux C library: `"glibc"` or `"musl"` |
| `runs-on-libc` | unset (accept any level) | The glibc/musl the lock must run on; wheels needing newer are dropped |
| `runs-on-macos` | unset (accept any level) | The macOS the lock must run on; wheels needing newer are dropped |
| `platform-release` | `""` | The `platform_release` marker value |
| `platform-version` | `""` | The `platform_version` marker value |
| `free-threaded` | `false` | Target the free-threaded (`cp3XXt`) CPython build |

A machine links one C library, so a target accepts one family's wheels:
a `glibc` target takes manylinux wheels and never musllinux ones, and a
`musl` target the reverse.

Left unset, `runs-on-libc` accepts wheels of any manylinux or musllinux
level and leaves compatibility to install time. Set it to the oldest
glibc or musl the lock must support. Its major must be glibc `2` or musl
`1`.

`runs-on-libc = "2.28"` means the lock must run on glibc 2.28.  A wheel is
lockable only if it runs on every target machine, so:

- `manylinux_2_17` and `manylinux_2_28` wheels run there and are accepted.
- a `manylinux_2_34` wheel cannot run there and is dropped: it needs a
  newer glibc.

Older wheels are never excluded; the knob only rules out wheels that need
something newer than the declared system, so a higher `runs-on-libc`
accepts more wheels, not fewer.  Newer systems are always fine: a lock
that runs on glibc 2.28 runs on anything newer (the same holds for macOS).

`runs-on-macos` reads the same way: `runs-on-macos = "14.0"` means the
lock must run on macOS 14.0, so a `macosx_10_9` or `macosx_14_0` wheel is
accepted and a `macosx_15_0` wheel is dropped.  It borrows Apple's
`MACOSX_DEPLOYMENT_TARGET`, a machine minimum.  Below the oldest macOS the
architecture ever ran (10.4 on x86_64, 11.0 on Apple Silicon) there is
no machine to model and no tag to name, so that is a config error.

A knob belongs to its platform.  `libc` and `runs-on-libc` are Linux
knobs and `runs-on-macos` is a macOS one, so declaring one on a platform
that cannot read it is a config error.  It would select no wheel, and it
would still name the machine the lock was resolved for.

`platform-release` and `platform-version` are the exception: they set
PEP 508 marker values and never enter wheel-tag selection.  Left empty,
every comparison against them evaluates False, so a dependency gated on
`platform_release >= "5.10"` (or on any other comparison) is dropped.  A
target that does run that kernel has to say so.

`free-threaded` picks the `cp3XXt` ABI, so the target takes the
free-threaded wheels and neither the ordinary `cp3XX` ones nor `abi3`
(a free-threaded interpreter cannot load either).  It needs CPython
3.13 or newer, the first release with a free-threaded build; a matrix
whose `python` admits an older minor, or whose `implementations` names
anything but `cpython`, is a config error, and so is a
`[tool.nab.environment]` whose `python` or `implementation` says the
same.

An id may appear once.  A lockfile entry is selected by a PEP 508
marker, and PEP 508 has no variable for the libc family or the
free-threaded build, so two targets sharing an id would render the same
marker and the lock could not tell their pins apart.  Locking a second
libc family, or a free-threaded build alongside the GIL one, is a
second lock run with its own config and output file.

Each target impersonates a platform, so universal mode cannot build on
the host: `build-policy` defaults to `never` and cannot be raised.  An
explicit non-`never` value (global or in any override) is a config
error.  See [Build policy](build-policy.md).

### The three `python` knobs

* `[tool.nab].requires-python` (or `[project].requires-python`): the
  range the project supports.  A declaration.  It is recorded as the
  lockfile's top-level `requires-python` and checked against the resolve
  target; it does not choose that target.

  The check reads the
  declaration at the language version, the same way a candidate's
  `Requires-Python` is read, so `==3.13` and `>=3.13.2` both admit a
  3.13 target however precisely that target names its interpreter, and
  `!=3.13` excludes one however it is named.

  A declaration that excludes the target is a config error naming the
  knob that moves it: `[tool.nab.environment] python` for a
  single-environment resolve, `[tool.nab.matrix].python` and
  `[tool.nab.matrix.python-patches]` for a matrix target, since neither
  `--python` nor `[tool.nab.environment]` is allowed alongside a matrix.
* `[tool.nab.environment].python`: the Python to resolve for, defaulting
  to the host's.  A single version, not a specifier.  `--python X.Y` and
  `--project-environment-python X.Y` set it for one run.
* `[tool.nab.matrix].python`: a range like `>=3.11,<3.14`, expanded into
  one target per minor version.  Used only by universal mode.  Pair with
  `[tool.nab.matrix].platforms`, `[tool.nab.matrix].implementations` and
  (optionally) `[tool.nab.matrix].python-patches` to control the resolve
  and marker shape across all targets.

Declaring a `[tool.nab.matrix]` table while `mode` is `specific` is an
error: the matrix is the list of targets to resolve, so leaving mode
behind is almost always an oversight rather than an intent.

The one exception is an explicit `--project-mode specific` on the
command line.  A CLI override outranks the `[tool.nab]` table, so it
selects a single-environment resolve for that run and the declared
matrix does not apply.  This is how a universal project takes one
single-environment lock without editing its `pyproject.toml`:

```bash
nab lock --project-mode specific --python 3.13
```

### Declaring the matrix on the command line

```bash
nab lock --project-mode universal \
  --project-matrix-python '>=3.11,<3.14' \
  --project-matrix-platforms windows_amd64 linux_x86_64 libc=musl runs-on-libc=1.2 \
  --project-matrix-python-patches 3.11=3.11.4
```

`--project-matrix-platforms` takes every token up to the next flag.  A bare
token is a platform id, `id=VALUE` is the same thing written out, and any
other `KEY=VALUE` sets a tag knob on the platform before it.  A knob value
`true` or `false` is the boolean.  `--project-matrix-python-patches` takes
`MINOR=FULL` tokens and `--project-matrix-implementations` takes bare names.

Each flag replaces the key it names inside the table the project files
declare and leaves the other keys alone, so a universal project narrows one
axis with one flag:

```bash
nab lock --project-matrix-platforms macos_arm64
```

With no file matrix there is nothing to narrow, so `--project-matrix-python`
and `--project-matrix-platforms` are both required and the other three take
the table's documented defaults when left out.

### Declaring the environment on the command line

```bash
nab lock --project-environment-python 3.12 \
  --project-environment-platform macos_arm64 runs-on-macos=14.0 \
  --project-environment-implementation cpython
```

`--project-environment-platform` reads its tokens the way
`--project-matrix-platforms` does, except that the environment holds one
machine: a second bare id is an error rather than a second platform.  An
axis no flag names keeps the value `[tool.nab.environment]` declares, or
the host's where no file declares one.

## CLI overrides

The [CLI reference](cli.md) lists every flag. See
[Selecting what to lock](selection.md) for groups, extras, and
workspaces, and [Output formats](formats.md) for `--format` and
`--output`.

`--project-<key>` overrides a scalar or list project option for one run.
The matrix and environment flags replace one key in the file's table,
leaving its other keys in place. Passing `--project-constraint` twice
replaces the file's entire `constraints` list with those two values. An
override prints a notice and is recorded in the lockfile.

`--project-dist-policy` takes a bare policy, so it replaces the whole
`dist-policy` value and resets `trust-unverified-deps`; set the table form
in a file to keep that flag.

## Layered configuration sources

A few options can be set from more than one place. Each option has a
scope that fixes where it may come from:

* Project-scope options describe the resolve itself, so they live with
  the project. Every `[tool.nab]` key is project-scope (`mode`,
  `indexes`, `constraints`, `vcs`, `workspace`, `dist-policy`,
  `build-policy`, `environment`, `conflicts`, `matrix`,
  `packages`, `resolution`, and the rest), and each may be set in
  either `pyproject.toml`'s `[tool.nab]` or a project-directory
  `nab.toml`.

  They are never read from a user/system file or an
  environment variable. Each project option with a scalar or list form
  also takes a CLI override under a `--project-` prefix (for example
  `--project-resolution`); the structured table options stay file-only,
  except `matrix` and `environment`, which the `--project-<table>-<key>`
  flags set one key at a time.
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
   (same precedence; setting one key in both files is covered below)
5. `NAB_*` environment variables
6. the CLI flag

Winning is all-or-nothing. Whatever the key's type, the highest source
that sets it supplies the whole value and nothing from a lower source
survives.

A `constraints` list on the CLI is the entire constraint set for that
run, and an `[environment]` table in a file is the entire environment.
No source adds to the one beneath it. To extend a list, edit the file
that declares it. A `--project-<table>-<key>` flag instead replaces one
key in the file's table.

Rung 4 is two files at one precedence, so neither can win. Setting one
key in both is allowed only if the two values are identical; different
values are a hard error naming both files. That applies to every key:
two `constraints` lists, two `[[indexes]]` arrays, and `python` under
`[tool.nab.environment]` against `platform` under `[environment]` in the
project `nab.toml` are all the same conflict, one key set twice. Set the
key in one of the two files.

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
value can differ from what `nab config` shows: `build-policy` is forced
to `never` under `mode = "universal"`, and `local-sources` gains the
workspace members found by discovery. `nab config` shows the value as
written, like `git config` reporting configured rather than derived state.

### Environment variables

| Variable | Option | Effect |
| -------- | ------ | ------ |
| `NAB_OFFLINE` | `offline` | `1`/`0`/`true`/`false`. |
| `NAB_CACHE_DIR` | `cache-dir` | Cache root path. |
| `NAB_HTTP_BACKEND` | `http-backend` | `urllib3` or `httpx`. |
| `NAB_MAX_CONCURRENCY` | `max-concurrency` | Parallel HTTP fetches, for the resolve as well as the downloads (at least `1`). |

A `NAB_*` name that is not one of these (a typo, or `NAB_RESOLUTION`
for a project-scope option) is ignored with a warning naming the
variable; `-qq` and `NAB_VERBOSITY=silent` turn it off along with
every other warning. `NAB_VERBOSITY` and `NAB_NO_PROGRESS` belong to the
[output layer](cli.md) and pass through silently. Run
`nab config list` to see every effective value and where it came from.
