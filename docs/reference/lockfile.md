# Lockfiles

nab is a resolver, not an installer. The artefact it produces is
a lockfile: a file that records every pinned version, every
artefact URL, and every digest needed to reproduce the same
install elsewhere.

`nab lock` writes one of three formats from the same in-memory
result:

* `--format pylock` (default): a [PEP 751] `pylock.toml`.
* `--format requirements`: a pip-compatible `requirements.txt`
  with one `--hash=<algo>:<digest>` line per recorded digest.
* `--format requirements-without-hashes`: a sorted
  `name==version` list with no hashes.

Pass `--output -` to write to stdout instead of a file.

## What is in a lock

Each pinned package carries:

* its name and version,
* one of four pin shapes: an `IndexPin` (PyPI or another simple
  index), a `LocalPin` (a directory on disk), a `VcsPin` (a
  git URL with a commit pin), or an `ArchivePin` (a `.tar.gz` URL
  content-pinned by a required `sha256`),
* every artefact the resolve considered at that pinned version
  (`sdist` and the `wheels` the target can install), each with its
  filename, URL, `sha256`, and optional size.

The digests are the ones the index published, so nothing is hashed
locally to build a lock. A resolve does fetch, but only to read
metadata. Sibling wheels of one version can declare different
dependencies, so where several of them fit the target, the one its
PEP 425 tags rank most specific is the one read. It takes the
cheapest source available: the wheel's [PEP 658] metadata sidecar
when the index publishes one, then an HTTP range read of the remote
wheel when no sidecar is published, otherwise the sdist's PKG-INFO
(built, if its dependencies are dynamic and the
[build policy](build-policy.md) allows it). A wheel served from a
local directory is read straight off disk, with no fetch. VCS and
archive sources are cloned or downloaded for the same reason.

A wheel the target's PEP 425 tags reject was never a candidate, so it
is not in the lock: a lock resolved on `linux_x86_64` carries no
`win_amd64` wheels.

## PEP 751 `pylock.toml`

The PEP 751 lockfile is the format for cross-tool Python
lockfiles. It is what `nab lock --format pylock` produces, and what
`nab lock --locked` checks against. It is an output. `nab download`
resolves from project inputs, so hand it a `pyproject.toml`, not a
lockfile.

A trimmed example, after resolving the
[getting-started](../tutorial/getting-started.md) project:

```toml
lock-version = "1.0"
environments = [
    'python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64" and platform_system == "Linux"',
]
created-by = "nab"

[[packages]]
name = "fastapi"
version = "0.109.1"
requires-python = ">=3.8"
index = "https://pypi.org/simple/"

[packages.sdist]
name = "fastapi-0.109.1.tar.gz"
url = "https://files.pythonhosted.org/.../fastapi-0.109.1.tar.gz"
hashes.sha256 = "..."
size = 11720487

[[packages.wheels]]
name = "fastapi-0.109.1-py3-none-any.whl"
url = "https://files.pythonhosted.org/.../fastapi-0.109.1-py3-none-any.whl"
hashes.sha256 = "..."
size = 92070
```

### Supported keys

* The top-level keys (`lock-version`, `created-by`,
  `requires-python`, `environments`, `extras`,
  `dependency-groups`, `default-groups`, `packages`), plus a
  `[tool.nab]` table of informational provenance.
* The `[[packages]]` shape: `name`, `version`, and optional
  `marker`, `requires-python`, `dependencies`, one source key
  (`index`, `directory`, `vcs`, or `archive`), and for an index
  source `sdist` and `wheels`.
* `sha256`, `sha384`, and `sha512` digests on every recorded
  artefact. Whatever the index publishes is forwarded; nab
  requires at least one of the three so the lockfile is
  consumable by pip's hash-checking mode.

### Extras and dependency groups

`nab lock --extras cli --groups dev` resolves the project, the `cli`
extra and the `dev` group together, and records the selection in the
top-level `extras` and `dependency-groups` keys. PEP 751 has an
installer read those keys as what the lock offers, and install with
no extras and only the `default-groups` unless it is asked for more.
So a package that only `cli` or only `dev` reaches is emitted with a
marker naming the selection that brought it in:

```toml
[[packages]]
name = "mytool"
version = "2.0.0"
marker = "\"cli\" in extras"

[[packages]]
name = "mydev"
version = "3.0.0"
marker = "\"dev\" in dependency_groups"
```

Reachability is what decides: unless `base-group` names them (below), a
package the project's own dependencies pull in is unconditional even
when a selected extra also asks for it, and a package that two
selections reach disjoins both
(`"cli" in extras or "dev" in dependency_groups`).
A group named in `[tool.nab].default-groups` still installs by
default, because PEP 751 seeds `dependency_groups` from
`default-groups` when the installer is given no group selection.

The gate is a property of the install context, not of the platform.
A matrix folds the selection into every target, so an extra that
reaches a package on every target gives it the bare membership
clause; one that reaches it on only some targets gets that clause
joined by `and` onto those targets' environment markers.

The versions are one joint resolution, so an extra's constraints
shape the pins of the packages it shares with the project. A
conflict between two selections is a resolution failure, not two
lockfiles, unless it is declared; see
[Conflicting extras and groups](../explanation/conflicts.md).

### Naming the project's own dependencies

A package with no marker installs under every selection, so the project's
own dependencies come with every group, and a lock offering groups cannot
be asked for one group alone. Naming them fixes that:

```toml
[tool.nab]
base-group = "base"
```

The lock carries that name in both group arrays and gates those packages
on it:

```toml
dependency-groups = ["base", "dev"]
default-groups = ["base"]

[[packages]]
name = "core"
version = "1.0.0"
marker = "\"base\" in dependency_groups"
```

An installer given no group selection still gets them, because PEP 751
seeds `dependency_groups` from `default-groups`. One asked for `dev`
alone gets that group and nothing else. One that asks for an empty group
list gets no group at all, not even the project's own. A package both
they and a group reach names both.

The name joins `default-groups` only when the project declares none of
its own. A declared `[tool.nab].default-groups` replaces the default
selection rather than extending it, so a project that wants its
dependencies installed alongside a group names them there too:
`default-groups = ["dev", "base"]`.

Unset, which is the default, they carry no marker. The name must not be
one the project already declares in `[dependency-groups]`, since a
marker naming it could not mean both. It is a name for the lock's
consumers rather than a group of the project's own, so `--groups` does
not accept it; `[tool.nab].default-groups` does, which is how it stays
in the default selection.

### Naming the build requirements

The same mechanism carries a project's `[build-system].requires`, so one
lock pins both the environment the project runs in and the one it is
built in:

```toml
[tool.nab]
base-group = "default"
build-group = "build"
```

```toml
dependency-groups = ["build", "default", "dev"]
default-groups = ["default"]

[[packages]]
name = "hatchling"
version = "1.31.0"
marker = "\"build\" in dependency_groups"
```

The build name lands in `dependency-groups` and not in `default-groups`:
an install that asks for no group is installing the project, not building
it. Only the static list is read, so whatever a backend would add from
`get_requires_for_build_wheel` is not covered.

`base-group` must be set alongside it, as above. Without a name of their own the
project's own dependencies carry no marker, so they install under every
selection and asking for the build group returns them too, which leaves
no way to install the build requirements alone. With both named, an
install that wants them together selects both groups. The name must also
be free of `[dependency-groups]` and of `base-group` itself. The
requirements output formats carry no markers, so there the build
requirements render as ordinary pins.

The build requirements resolve in the same version space as the
project's own, since one install context is all a shared marker can
describe. A project whose backend needs a version its own dependencies
exclude declares the two mutually exclusive; the resolve then forks and
each side gets its own pins under disjoint markers. See
[Conflicting extras and groups](../explanation/conflicts.md).

`nab lock --build-requirements` writes the build side to a file of its
own instead. That is what a consumer needs when it cannot select groups
at all, which covers the requirements output formats and today's pylock
installers.

### Portable paths

A lockfile can reference content on disk. A path nab derives from a
filesystem location is written relative to the lockfile's own directory,
with POSIX separators: a local source or workspace member in
`packages.directory.path`, and a wheel or sdist read from a local index or
find-links directory in `packages.wheels.path` and `packages.sdist.path`.
PEP 751 reads a relative path against the lockfile itself, so the lock and
the tree it points at survive a move as long as they move together. On
Windows a path on another drive has no relative form, and the absolute
path is written instead. `nab lock --output -` has no lockfile to be
relative to and writes those paths relative to the current directory.

A URL declared in the configuration is written as it was declared, minus
any embedded credentials: an archive source in `packages.archive.url`, a
local index in `packages.index`, and a local repository in
`packages.vcs.url`. A `file://` URL is not made relative, so a lock that
carries one is usable only where that location exists.

### The environments the lock is for

A resolve answers for the environments it targeted: the
[resolve target](configuration.md), or the targets of a declared
matrix. Every dependency whose PEP 508 marker was false on a target
was dropped there, so the pins are not a package set another
environment can install. The lock says so in the top-level
`environments`, one entry per environment resolved, and a PEP 751
consumer refuses a lock whose declared environments none of its own
satisfies. nab refuses to emit such a lock in the first place: when the
`environments` declaration would not cover a target the resolve ran for,
it raises and names the uncovered interpreter.

Each declaration always pins `python_version`, `sys_platform` and
`platform_machine`, plus `implementation_name` when the target runs
an interpreter other than CPython or the matrix models more than one.
It also pins every other PEP 508 variable that a marker in the resolve
consulted: a dependency, root requirement, or constraint marker on
`platform_system` pins `platform_system`. This is deliberately narrow.
A marker nab evaluated is a question whose answer changed the package
set, so an installer that answers it differently must not use this
lock. The variables below are the exceptions.

`python_full_version` and `implementation_version` are never declared
by value. A target that names a minor (a matrix target, a declared
environment, or a `--python <minor>` target) stands for every micro
of that minor, so pinning one micro would refuse every other real
one. Instead the minor covers all its micros, and a consulted
`python_full_version` marker whose boundary lies inside it splits it
at that boundary. Each side becomes its own slice with its own
`environments` row and pins: `pytest ; python_full_version >= "3.13.4"`
on a 3.13 target cuts the minor into a `< "3.13.4"` slice and a
`>= "3.13.4.dev0"` slice, and `pytest` joins only the upper one (see
Universal mode below).

The two slice bounds meet at `3.13.4.dev0`: the lower slice ends at
`< "3.13.4"` and the upper starts at `>= "3.13.4.dev0"`, which together
cover the minor with no gap and no overlap. The `.dev0` on the lower
edge is deliberate. A verbatim `< "3.13.4"` / `>= "3.13.4"` pair would
leave the prereleases of 3.13.4 (`3.13.4rc1`) in neither slice, because
PEP 440 keeps them out of both; snapping the lower edge to
`>= "3.13.4.dev0"` puts them on the upper side, so a user on a
prerelease of 3.13.4 gets the pins intended for 3.13.4.

On CPython `implementation_version` is the same release as
`python_full_version` (it comes from `sys.implementation.version`), so
it is read the same way: a consulted marker on it names an in-minor
boundary and splits the minor there, and a slice whose resolve consulted
it carries the slice bounds under that name as well. Reaching the 3.13.4
split above through `pytest ; implementation_version >= "3.13.4"` leaves
the lower slice with `python_full_version < "3.13.4"` and
`implementation_version < "3.13.4"`, and the upper with the matching
`>= "3.13.4.dev0"` pair. A slice whose resolve never read the variable
carries the `python_full_version` bounds alone.

A minor no marker split reverts to a plain `python_version == "3.13"`
row with no `python_full_version` clause: a marker whose boundary lies
outside the minor, or a prerelease of the minor's floor
(`<= "3.11.0a6"`), reads the same for every real micro, so it names no
in-minor boundary and its dependency is simply kept or dropped for the
whole minor.

A whole target is never split: the host interpreter nab reads names a
real micro, and a `[tool.nab.matrix.python-patches]` pin names one
concrete deployment micro. Both resolve at that single micro and emit
the plain `python_version == "X.Y"` row.

A consulted `python_full_version` marker, or on CPython an
`implementation_version` one, that cannot be tiled into an interval is
a loud error rather than a pin of the whole minor to one answer: a
membership (`in` / `not in`), a verbatim `===`, a non-version string
comparison, a comparison against another variable, a literal PEP 440
refuses under the operator it is written with (`< "3.12.*"`, `~= "3"`),
or a pre- or post-release literal strictly inside the minor on one of
the operators below.

A prerelease literal is an error on `<`, `>=`, `==`, `!=` and `~=`, and
a literal that is only a post-release on `>=`, `==`, `!=` and `~=`.
Every other operator lands the boundary on a real micro:
`<= "3.12.4rc1"` and `> "3.12.4rc1"` cut at 3.12.4, and
`< "3.12.4.post1"`, `<= "3.12.4.post1"` and `> "3.12.4.post1"` at
3.12.5.

`platform_release` and `platform_version` are never declared: they
name one machine's kernel build, so a lock carrying the resolving
machine's value would refuse every other machine. A non-CPython target
drops `implementation_version` the same way. nab models it there as the
target's Python level, while a released PyPy reports its own release
(7.3.x), so a bound built from the modelled value would refuse the
interpreter the lock was resolved for. Nothing splits such a target's
minor on that axis either, so its rows carry no
`implementation_version` clause.

A marker that consults a dropped axis is reported as a warning at lock
time, one for the kernel pair and one for `implementation_version` on a
non-CPython target. The lock stays open on that axis: an installer
whose value differs still accepts the lock, and misses the dependencies
that marker gated.

`requires-python` is the project's declaration
(`[tool.nab].requires-python`, or `[project].requires-python`), not
the target's Python. It bounds what the project supports; the
`environments` entries name what was resolved.

### Universal mode

Under `[tool.nab].mode = "universal"`, `nab lock --format pylock`
writes a single file covering every
`(python, platform, implementation)` target in the matrix. Packages
whose pinned version is identical across every target appear once with
no marker; packages that diverge appear as multiple `Package` entries
with PEP 508 markers built from each target's `python_version`,
`sys_platform` and `platform_machine`, plus `implementation_name` on
the same terms as the `environments` declarations above. A slice of a
split minor adds its `python_full_version` bounds. Each such marker is
emitted in its shortest form equivalent over the lock's declared
environments, and the emitted bytes are checked against the original
over those environments before the lock is written, one declared
environment at a time so that a matrix spanning many platforms stays
decidable. A marker whose shortening or check runs out of budget is
emitted unsimplified, as is one that selects nothing inside the
declared environments.

`environments` carries a declaration per target, built the same way a
single-environment lock builds its one: from the markers that target's
resolve consulted. A target whose minor an in-minor micro boundary
splits carries one declaration per slice, so a target can contribute
more than one entry (see
[Universal resolution](../explanation/universal.md)). A target's
`packages.dependencies` edges are the union across the targets an entry
covers.

## Pip-compatible `requirements.txt`

`nab lock --format requirements` writes one line per package
using pip's [hash-checking] format:

```
fastapi==0.109.1 \
    --hash=sha256:... \
    --hash=sha256:...
starlette==0.35.1 \
    --hash=sha256:... \
    --hash=sha256:...
```

A package carries one `--hash` line per recorded digest, not one
per artefact. Only `sha256`, `sha384`, and `sha512` are recorded,
so a wheel the index publishes with an `md5` and a `sha256`
contributes one line, and one published with a `sha256` and a
`sha512` contributes two. The lines are sorted by algorithm then
digest rather than grouped by artefact, so an sdist hash lands
wherever its digest sorts and the output does not depend on the
order the artefacts were found in. The continuation backslashes
match what pip-compile emits.

Local and VCS pins are rendered without hashes, mirroring pip's
behaviour. An editable local pin becomes a `-e` line and a
`subdirectory` a `#subdirectory=` fragment. Workspace members are
editable by default, so they render as `-e`; a `[[tool.nab.local-sources]]`
pin is non-editable unless its `editable` key is set. An archive pin is a
third form, `name @ <url>#sha256=...`, carrying its hash in the
URL fragment so the line stays hash-checkable. Any
`subdirectory` is appended to that fragment with `&`. Local,
VCS, and archive pins render the same with or without hashes:

```
my-fork @ file:///abs/path/to/checkout
-e file:///abs/path/to/monorepo#subdirectory=packages/foo
some-pkg @ git+https://github.com/me/x.git@<sha>
my-archive @ https://example.com/my-archive-1.0.tar.gz#sha256=<hex>
mono @ https://example.com/mono-3.0.tar.gz#sha256=<hex>&subdirectory=packages/foo
```

`pip install --require-hashes -r requirements.txt` will accept
the output as-is when every dependency has at least one hash.
Mixed input (some hashed, some not) is rejected by
`--require-hashes`; add `--no-deps` and resolve the un-hashed
entries some other way if that is the workflow you need.

## Reproducibility

Two things have to hold for a lock to reproduce: the index has to
give the same answer, and the resolver has to search the same
way. They are separate settings.

`[tool.nab].uploaded-prior-to` bounds the index view.
Distributions uploaded after that timestamp are ignored, even if
newer files exist on the index when you run, so the lockfile is
truly reproducible rather than "reproducible until upstream
re-uploads".

`[tool.nab].decision-order = "stable"` bounds the search. On the
default `arrival`, nab decides packages whose listing has already
arrived ahead of ones still in flight, so a machine with a cold
HTTP cache can search differently, and on some inputs pin
differently, from one with a warm cache on the same inputs and
the same frozen index. See "Decision order" in the
[configuration reference](configuration.md) for what it costs.

With both set, a fresh resolve produces the same pin set, and an
absolute `uploaded-prior-to` also becomes the lockfile's
`created-at`, written in UTC, so two locks from identical inputs
are byte-for-byte identical. `--upgrade` is the exception: it
stamps the run time instead.

Both settings assume an index that does not move. Several things
move it. Yanking is a property of the listing as it stands, not
of the upload, so a file yanked after you locked changes the
resolve and `uploaded-prior-to` does not bring it back. A deleted
file cannot be resolved to at all.

The cutoff also trusts the upload times the index reports. An
index that rewrites them changes what a past cutoff admits, and
nab does not detect that. A distribution reported without an
upload time is excluded once a cutoff is set, so an index that
reports none serves nothing at all. Local `file://` and
find-links artifacts carry no upload time either, and those are
kept rather than excluded, so a resolve mixing a wheelhouse with
an index is only partly time-bounded.

A relative `P<n>D` cutoff is measured back from `created-at`
rather than from the clock, and a re-lock reuses the `created-at`
recorded in the pylock file it will write. A re-lock therefore
resolves against the window the first lock used, and `--upgrade`
is what moves that window forward. An absolute cutoff bounds the
resolve the same way either run.

A first lock, a pylock with no `created-at`, output to stdout,
and the requirements formats have no recorded timestamp to reuse,
so they anchor to the run time.

## Checking the lock in CI

`nab lock --locked` re-resolves from your project inputs,
checks that the committed `pylock.toml` already matches, and
writes nothing. It exits non-zero if the lock would change or
is missing, so a CI step can assert the lock is current:

```bash
nab lock --locked
```

The committed pins are never seeded into the resolve, so the
check confirms the lock still reflects those inputs rather than
that it round-trips through itself. Only a real change to the
locked packages fails it; volatile provenance metadata is
ignored. The lock sorts the extras and groups it records, so
listing the same names in another order, or naming that set with
`--all-extras` or `--all-groups`, writes the same arrays.
`--locked` applies to a `pylock.toml` file in single-environment
mode.

Some mismatches are provable from your inputs without resolving, so
`--locked` can fail fast with the reason before it re-resolves; see the
[CLI reference](cli.md).

`--locked` re-resolves, so it depends on everything the resolve
depends on. On the default `decision-order = "arrival"` the check
can fail on a commit nobody changed, when the CI runner's HTTP
cache is colder than the machine that wrote the lock. Set
`decision-order = "stable"` on any project that runs it.

## `nab download`

`nab download` resolves the project again, then fetches every wheel,
sdist, and direct-URL archive on the resulting pins into the
`--output` directory (defaults to `wheels/`), verifying each file's
`sha256` against the digest recorded on the pin. Local and VCS pins
are skipped. The download is idempotent: a file whose digest already
matches a local copy is left alone.

The result is a per-resolve directory of artefacts that any
installer can consume offline:

```bash
nab download
pip install --no-index --find-links wheels/ -r requirements.txt
```

This pairs naturally with `--require-hashes`: hashes are baked
into the requirements file, the artefacts on disk are verified
against those same digests on the way down, and pip refuses
anything else.

[PEP 751]: https://peps.python.org/pep-0751/
[PEP 658]: https://peps.python.org/pep-0658/
[hash-checking]: https://pip.pypa.io/en/stable/topics/secure-installs/#hash-checking-mode
