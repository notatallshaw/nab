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
created-by = "nab"
requires-python = ">=3.10"
environments = [
    'python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64"',
]

[[packages]]
name = "fastapi"
version = "0.115.2"
index = "https://pypi.org/simple/"

[[packages.wheels]]
name = "fastapi-0.115.2-py3-none-any.whl"
url = "https://files.pythonhosted.org/.../fastapi-0.115.2-py3-none-any.whl"
hashes.sha256 = "..."
size = 94918

[packages.sdist]
name = "fastapi-0.115.2.tar.gz"
url = "https://files.pythonhosted.org/.../fastapi-0.115.2.tar.gz"
hashes.sha256 = "..."
size = 286433
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

Reachability is what decides: a package the project's own
dependencies pull in is unconditional even when a selected extra
also asks for it, and a package that two selections reach disjoins
both (`"cli" in extras or "dev" in dependency_groups`). A group
named in `[tool.nab].default-groups` still installs by default,
because PEP 751 seeds `dependency_groups` from `default-groups`
when the installer is given no group selection.

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

### Portable paths

A lockfile can reference content on disk: a `LocalPin`'s
directory, or a wheel or sdist served from a local find-links
directory. nab writes those paths relative to the lockfile's
own directory, with POSIX separators, as PEP 751 requires. A
committed lockfile therefore stays usable on another machine as
long as the surrounding layout is preserved; it never carries an
absolute, machine-specific path.

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
lock.

`python_full_version` is the exception: it is never declared by
value. A target that names a minor (a matrix target, a declared
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

A consulted `python_full_version` marker that cannot be tiled into an
interval is a loud error rather than a pin of the whole minor to one
answer: a membership (`in` / `not in`), a verbatim `===`, a non-version
string comparison, a comparison against another variable, or a
prerelease-version literal strictly inside the minor on an operator that
fixes the boundary at the literal (`<`, `>=`, `==`, `!=`, `~=`). On `<=`
or `>` a prerelease literal lands at the next release and tiles cleanly.

Two variables are never declared: `platform_release` and
`platform_version` name one machine's kernel build, so a lock
carrying the resolving machine's value would refuse every other
machine. A marker that consults one is reported as a warning at
lock time; the lock stays open on that axis.

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
the same terms as the `environments` declarations above.

`environments` carries a declaration per target, built the same way a
single-environment lock builds its one: from the markers that target's
resolve consulted. A target whose minor an in-minor `python_full_version`
boundary splits carries one declaration per slice, so a target can
contribute more than one entry (see
[Universal resolution](../explanation/universal.md)). A target's
`packages.dependencies` edges are the union across the targets an entry
covers.

## Pip-compatible `requirements.txt`

`nab lock --format requirements` writes one line per package
using pip's [hash-checking] format:

```
fastapi==0.115.2 \
    --hash=sha256:... \
    --hash=sha256:...
starlette==0.36.0 \
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
`subdirectory` a `#subdirectory=` fragment. An archive pin is a
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

Use `[tool.nab].uploaded-prior-to` to make the resolve
time-bounded. Distributions uploaded after that timestamp are
ignored, even if newer files exist on the index when you run.
This pairs naturally with the lockfile: a fresh resolve with the
same `uploaded-prior-to` produces the same pin set, so the
lockfile is truly reproducible rather than "reproducible until
upstream re-uploads".

When `uploaded-prior-to` is an absolute timestamp, that timestamp
also becomes the lockfile's `created-at`, so two locks from
identical inputs are byte-for-byte identical. A relative `P<n>D`
cutoff is anchored to the wall clock, so `created-at` stays the
run time; `--upgrade` always re-anchors to now.

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
ignored. `--locked` applies to a `pylock.toml` file in
single-environment mode.

Some mismatches are provable from your inputs without resolving, so
`--locked` can fail fast with the reason before it re-resolves; see the
[CLI reference](cli.md).

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
