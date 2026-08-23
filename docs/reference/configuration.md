# Configuration

The keys that decide what nab resolves live in `[tool.nab]` inside the
project's `pyproject.toml`, or in a project-directory `nab.toml` that
sets the same keys.  A CLI flag applies to a single run, and some change
the resolve rather than just how the run executes; the CLI flags section
below groups them and says which ones reach a `[tool.nab]` key.

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
# are ignored.  Accepts ISO 8601 strings, native TOML datetimes, or a
# "P<n>D" duration relative to the resolve anchor.  Artifacts from a
# local file:// index carry no upload time and are always kept.  An
# HTML listing rarely carries one either, and those files are then
# excluded; see "Serialization" below.
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
  is the same thing for one run.
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
wheel that will not install (`pywin32` on Linux).  An sdist keeps a
version alive whatever the build policy, so a pure-source package still
resolves.  The lockfile records only the wheels the target can install.

The resolve is still for one environment.  A lock made for
`linux_x86_64` is a lock for `linux_x86_64`; it says so in its PEP 751
`environments` (see [lockfiles](lockfile.md)), and a conforming installer
refuses it elsewhere.  To lock for several machines at once, use
`mode = "universal"`.

`[tool.nab.environment]` and `[tool.nab.matrix]` cannot both be set: the
matrix already declares one environment per target.

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
`Requires-Python` value takes, so it admits or rejects a version exactly
as a declared value would.  That comparison is made at the language
version: a micro segment neither admits a target nor excludes one, so
`>=3.13.2`, `==3.13.4` and `==3.13.*` all admit a 3.13 target, while
`!=3.13` excludes every 3.13 interpreter.  An empty string
(`requires-python = ""`) removes the Python requirement entirely (admits
every target).  For an index pin the lock records the overridden
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
via an override.  A wheel that ships no PEP 658 sidecar still resolves
under any wheel-admitting policy: its METADATA is recovered by an HTTP
range read of the remote wheel rather than the version being skipped.
An index that cannot serve usable ranges has the whole wheel fetched
instead, so recovery still works there at the cost of a full download
per sidecar-less wheel the resolver visits.

`sdist-install` is the policy to reach for when an installer needs
to build the package from source (typically to link against
system libraries like libxml2, libxmlsec, or system OpenSSL), but you
do not want to pay the cost of building it twice (once during the
resolve to learn its dependencies, once at install time).  The
lockfile pins only the sdist so `pip install --require-hashes`
materialises that archive; the resolver, meanwhile, reads
dependency metadata from whichever source is cheapest at the
chosen version.  In practice that means the wheel's METADATA via
PEP 658 when one is published, or an HTTP range read of the remote
wheel when no sidecar is published, falling back to the sdist's
PKG-INFO (with the usual PEP 643 and `pyproject.toml` fallbacks)
when no wheel exists.  Equivalent in spirit to pip's
`--no-binary <pkg>` for the install side, without paying the
build cost on the resolve side.  A version that publishes no sdist
has no source to install, so it is skipped the way `sdist-only`
skips a wheel-only release and the resolver settles on the newest
version that ships one.

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
a version the resolve settled on elsewhere.  The look-ahead runs under
both settings, and it reads the metadata and the decisions taken so
far rather than what has arrived, so `stable` still gives one
lockfile.  What it changes is the answer against a resolver that does
not look ahead: the two can pin different versions of one project, and
both satisfy every requirement.

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
`false`) records a PEP 660 editable install in the lockfile.  This
default is specific to `[[tool.nab.local-sources]]`; workspace
members discovered from `[tool.nab.workspace]` are recorded as
editable by default (see [Lock a workspace](../how-to/workspaces.md)).
`subdirectory` locates the package below `path`, for monorepo
layouts.

Reading static dependencies from a local pyproject.toml works at
every `build-policy` level.  Dynamic dependencies require
`build-policy = "build-local"` or `"build-remote"`.

## Pinned VCS sources

`[[tool.nab.vcs-sources]]` pins a package to a VCS URL.  Declaring one
while `vcs.policy` is left at its default `block` is a contradiction and
is rejected when the config is read, before any resolve starts.  Each URL
then passes the same `[tool.nab.vcs]` gate as a direct-URL requirement:
beyond `vcs.policy = "allow"`, the URL's scheme must be listed in
`vcs.allowed-schemes` and its repository in `vcs.allowed-repos`, both
empty by default so each denies every URL until an entry is added, and
the URL must pin a 40-char commit hash unless `vcs.require-pin = false`.
Reading static dependencies works at any `build-policy`.  Dynamic
dependencies on a VCS clone require `build-policy = "build-remote"`.

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
over HTTP.  The extracted tree is cached alongside the hashes it was
verified against, so a later resolve that declares those same hashes
reuses it and downloads nothing, `--offline` included.  Declaring a hash
the cached tree was not checked against downloads the archive again.

## Universal mode

Universal resolution resolves one target per declared
`(python, platform, implementation)` point, on the same engine a
single-environment resolve uses, sharing one fetcher so metadata is
fetched at most once per package.  The multi-target lockfile format it
produces is experimental and may change without notice.

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
`musl` target the reverse.  Left unset, `runs-on-libc` names no system:
the target accepts a wheel of any manylinux/musllinux level and whether
it runs is left to install time, the way uv resolves.  Set it to name the
glibc or musl the lock must run on.  Its major has to match the family
(glibc `2.x`, musl `1.x`): those are the only majors either has shipped,
so no wheel targets another.

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
  target; it does not choose that target.  The check reads the
  declaration at the language version, the same way a candidate's
  `Requires-Python` is read, so `==3.13` and `>=3.13.2` both admit a
  3.13 target however precisely that target names its interpreter, and
  `!=3.13` excludes one however it is named.  A
  declaration that excludes the target is a config error naming the knob
  that moves it:
  `[tool.nab.environment] python` for a single-environment resolve,
  `[tool.nab.matrix].python` and `[tool.nab.matrix.python-patches]` for a
  matrix target, since neither `--python` nor `[tool.nab.environment]` is
  allowed alongside a matrix.
* `[tool.nab.environment].python`: the Python to resolve for, defaulting
  to the host's.  A single version, not a specifier.  `--python X.Y`
  overrides it for one run.
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

## CLI flags

```
nab lock [PATH]
  # what gets resolved
  --python X.Y                 # resolve for this Python on this machine
  --groups NAME ...            # fold these dependency groups into the resolve
  --all-groups                 # fold in every group the project declares
  --extras NAME ...            # fold these extras into the resolve
  --all-extras                 # fold in every extra the project declares
  --build-requirements         # lock [build-system].requires, not the dependencies
  --no-workspace-discovery     # resolve this project alone, ignoring [tool.nab.workspace]
  --upgrade                    # re-anchor the P<n>D window to now
  --project-<key> VALUE        # override a project option for this run

  # what comes out
  --output PATH                # output file (or "-" for stdout)
  --format FORMAT              # pylock | requirements | requirements-without-hashes
  --no-emit-workspace          # leave workspace members out of the lockfile
  --locked                     # verify the committed pylock is current; write nothing

  # how the run executes
  --cache-dir PATH             # override on-disk cache location
  --no-cache                   # disable cache for this run
  --offline True|False         # use cache only, no network (or bare --offline / --no-offline)
  --http-backend X             # urllib3 (default) | httpx (layered)
```

What each flag in the first group does to `[tool.nab]`:

* `--project-<key>` replaces the key it names.
* `--python` overrides `[tool.nab.environment].python`.
* `--build-requirements` clears `conflicts`, `default-groups`,
  `base-group` and `build-group` for the run.
* `--no-workspace-discovery` skips `[tool.nab.workspace]`, the project's
  own table included.
* `--upgrade` re-anchors a relative `uploaded-prior-to` window without
  changing its value.
* `--groups`, `--all-groups`, `--extras` and `--all-extras` add to the
  selection rather than replacing a key.

A project option can be overridden for a single run with a
`--project-<key>` flag (for example `--project-resolution`,
`--project-dist-policy`, or the repeatable `--project-constraint`); the
structured table options stay file-only. The flag replaces the file
value, list flags included: passing `--project-constraint` twice
resolves against exactly those two constraints, whatever `constraints`
the files declare. A `--project-*` override changes the resolved set, so
it prints a notice and is recorded in the lockfile.
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
   (same precedence; setting one key in both files is covered below)
5. `NAB_*` environment variables
6. the CLI flag

Winning is all-or-nothing. Whatever the key's type, the highest source
that sets it supplies the whole value and nothing from a lower source
survives: a `constraints` list on the CLI is the entire constraint set
for that run, and an `[environment]` table in a file is the entire
environment. No source adds to the one beneath it, so the value the
resolve uses is the one you can read in a single place. To extend a
list, edit the file that declares it.

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
| `NAB_MAX_CONCURRENCY` | `max-concurrency` | Parallel HTTP fetches for `nab download` (at least `1`). |

A `NAB_*` name that is not one of these (a typo, or `NAB_RESOLUTION`
for a project-scope option) is ignored with a warning naming the
variable. `NAB_VERBOSITY` and `NAB_NO_PROGRESS` belong to the
[output layer](cli.md) and pass through silently. Run
`nab config list` to see every effective value and where it came from.
