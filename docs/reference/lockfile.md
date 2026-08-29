# Lockfiles

nab resolves packages but does not install them. Its lock records the pinned
versions, artifact locations, and digests for another tool to consume.

`nab lock` writes one of three formats from the same in-memory
result:

* `--format pylock` (default): a [PEP 751] `pylock.toml`.
* `--format requirements`: a pip-compatible `requirements.txt`, sorted by
  name. Index pins carry recorded digests; local, VCS, and archive pins are
  URL lines.
* `--format requirements-without-hashes`: the same output without separate
  `--hash` lines. An archive URL still carries its digest.

Pass `--output -` to write to stdout instead of a file.
See [Use a lock](../how-to/use-the-lock.md) for installation workflows.

## What is in a lock

Each pinned package carries:

* its name and version,
* one of four pin shapes: an `IndexPin` (PyPI or another simple
  index), a `LocalPin` (a directory on disk), a `VcsPin` (a
  git URL with a commit pin), or an `ArchivePin` (a `.tar.gz` URL
  content-pinned by at least one accepted digest),
* every artifact the resolve considered at that pinned version
  (`sdist` and the `wheels` the target can install), each with its
  filename, URL, recorded digests, and optional size.

Index digests come from the listing; nab does not fetch every index artifact to re-hash it. A direct archive's digest comes from its configured URL and is verified before metadata is read.

When several wheels fit the target, nab reads the one whose PEP 425 tags rank
highest. It prefers a [PEP 658] metadata sidecar, then reads the wheel itself
through HTTP ranges or a full fetch. With no wheel, it reads the sdist;
dynamic metadata may require a [permitted build](build-policy.md).

Local wheels are read from disk; VCS and archive sources are materialised to read their metadata.

A wheel the target's PEP 425 tags reject was never a candidate, so it
is not in the lock: a lock resolved on `linux_x86_64` carries no
`win_amd64` wheels.

## PEP 751 `pylock.toml`

The PEP 751 lockfile is the cross-tool format produced by
`nab lock --format pylock` and checked by `nab lock --locked`.
`nab download` does not consume it; that command resolves project inputs again.

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
  artifact. Whatever the index publishes is forwarded; nab
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

`base-group` is required. Without it, the project's dependencies have no
marker and accompany every selection, including the build group. Naming both
lets a consumer select them separately or together.

`build-group` must differ from `base-group` and every declared
`[dependency-groups]` name. Requirements output carries no group markers, so
build requirements render as ordinary pins there.

The build requirements resolve in the same version space as the
project's own, since one install context is all a shared marker can
describe. A project whose backend needs a version its own dependencies
exclude declares the two mutually exclusive; the resolve then forks and
each side gets its own pins under disjoint markers. See
[Conflicting extras and groups](../explanation/conflicts.md).

`nab lock --build-requirements` writes the build side separately. Use it
when the consumer cannot select groups, including the requirements formats
and pip's current pylock interface.

### Portable paths

A filesystem path is written relative to the lock's directory with POSIX
separators. This covers local sources and workspace members in
`packages.directory.path`, plus local-index and find-links artifacts in
`packages.wheels.path` or `packages.sdist.path`.

PEP 751 resolves the path against the lock, so both can move together. A
Windows path on another drive stays absolute. With `--output -`, paths are
relative to the current directory instead.

A URL declared in the configuration is written as it was declared, minus
any embedded credentials: an archive source in `packages.archive.url`, a
local index in `packages.index`, and a local repository in
`packages.vcs.url`. A `file://` URL is not made relative, so a lock that
carries one is usable only where that location exists.

### The environments the lock is for

A resolve drops dependencies whose PEP 508 markers are false on its
[target](configuration.md). The resulting pins therefore belong to that
environment, or to the targets declared by a matrix.

The top-level `environments` entries record that scope. A PEP 751 consumer
refuses a lock that does not cover its environment. nab also refuses to emit a
lock whose declaration misses a resolved target and names the uncovered
interpreter.

Each declaration pins `python_version`, `sys_platform`, and
`platform_machine`. It adds `implementation_name` for a non-CPython target or
a matrix with several implementations.

It also pins each PEP 508 variable consulted by a dependency, root
requirement, or constraint marker. This prevents a consumer that answers a
relevant marker differently from using the lock. The variables below are
exceptions.

`python_full_version` and `implementation_version` are not declared as one
micro version. A target naming a minor stands for every micro in that minor,
so a single value would reject the others.

A consulted `python_full_version` boundary inside the minor splits it into
slices. On a 3.13 target, `pytest ; python_full_version >= "3.13.4"` creates
`< "3.13.4"` and `>= "3.13.4.dev0"` slices; only the upper one includes
`pytest`.

The bounds meet at `3.13.4.dev0` with no gap or overlap. A verbatim
`< "3.13.4"` / `>= "3.13.4"` pair would exclude prereleases such as
`3.13.4rc1` from both slices. Starting the upper slice at
`3.13.4.dev0` gives those interpreters the 3.13.4 pins.

On CPython, `implementation_version` matches `python_full_version` and can
split the minor the same way. A slice that consulted it carries both sets of
bounds; otherwise it carries only `python_full_version`.

For example, `implementation_version >= "3.13.4"` gives the lower slice
matching `< "3.13.4"` clauses and the upper slice matching
`>= "3.13.4.dev0"` clauses for both variables.

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

A consulted full-version marker must describe intervals. nab rejects:

* membership (`in` or `not in`), verbatim `===`, non-version strings, and
  comparisons with another variable;
* invalid PEP 440 operands such as `< "3.12.*"` or `~= "3"`;
* the pre- and post-release boundaries listed below.

A prerelease literal is an error on `<`, `>=`, `==`, `!=` and `~=`, and
a literal that is only a post-release on `>=`, `==`, `!=` and `~=`.
Every other operator lands the boundary on a real micro:
`<= "3.12.4rc1"` and `> "3.12.4rc1"` cut at 3.12.4, and
`< "3.12.4.post1"`, `<= "3.12.4.post1"` and `> "3.12.4.post1"` at
3.12.5.

`platform_release` and `platform_version` are never declared because they
name one machine's kernel build. Recording the resolving machine would reject
other machines.

A non-CPython target also drops `implementation_version`. nab models it as the
Python level, while released PyPy reports its own version, so recording the
modelled value would reject the intended interpreter. This axis does not split
the target's minor.

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

Under `[tool.nab].mode = "universal"`, one pylock covers every matrix target.
A pin shared by all targets appears once without a marker. Divergent pins use
PEP 508 markers over the target axes; a split minor also uses
`python_full_version` bounds.

nab shortens each marker only after checking equivalence over every declared
environment. It keeps the original marker if simplification or verification
runs out of budget, or if the marker selects no declared environment.

`environments` is built from the marker variables each target consulted. A
minor split at a micro boundary contributes one declaration per slice.

An entry's `packages.dependencies` edges are the union across every target it covers. See [Universal resolution](../explanation/universal.md).

## pip-compatible `requirements.txt`

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

Each recorded digest gets one `--hash` line. nab records `sha256`, `sha384`,
and `sha512`, then sorts by algorithm and digest rather than artifact.

Output therefore does not depend on discovery order. Continuation backslashes match pip-compile.

Local and VCS pins have no hashes. Editable local pins use `-e`; workspace
members are editable by default, while `[[tool.nab.local-sources]]` entries
are not unless configured. A subdirectory uses `#subdirectory=`.

Archive pins put their accepted digests in the URL fragment, with any subdirectory appended by `&`. The examples below use `sha256`. Local, VCS, and archive pins render the same in both requirements formats:

```
my-fork @ file:///abs/path/to/checkout
-e file:///abs/path/to/monorepo#subdirectory=packages/foo
some-pkg @ git+https://github.com/me/x.git@<sha>
my-archive @ https://example.com/my-archive-1.0.tar.gz#sha256=<hex>
mono @ https://example.com/mono-3.0.tar.gz#sha256=<hex>&subdirectory=packages/foo
```

pip's [hash-checking] mode accepts the file only when every dependency is
hashed. Local and VCS pins make the output mixed and therefore unsuitable for
that mode. [Use a lock](../how-to/use-the-lock.md) gives the install commands.

## Reproducibility

Repeatable locking needs a fixed index view and a fixed search order:

* `[tool.nab].uploaded-prior-to` excludes distributions uploaded after its
  timestamp.
* `[tool.nab].decision-order = "stable"` prevents response timing and cache
  warmth from steering package order. See [Configuration](configuration.md)
  for its cost.

With the index response held fixed, both settings reproduce the pin set. An
absolute cutoff also becomes the UTC `created-at`, so identical inputs produce
identical lock bytes. `--upgrade` replaces that timestamp with the run time.

The cutoff does not freeze an index. A later yank changes the listing, a
deleted file is unavailable, and nab trusts reported upload times. Files with
no upload time are excluded, except local `file://` and find-links artifacts,
which remain eligible.

A resolve mixing those sources with an index is only partly time-bounded.

A relative `P<n>D` cutoff is measured from `created-at`. Re-locking reuses the
existing lock's timestamp; `--upgrade` moves the window. A first lock, stdout,
requirements output, or a pylock without `created-at` anchors to the run time.

## Checking the lock in CI

`nab lock --locked` re-resolves project inputs and compares the result with the
committed pylock without writing. It applies to file output in
single-environment mode. The existing pins do not seed the resolve, and
volatile provenance is ignored.

Extras and groups are sorted in the lock. Reordering the same names, or using
`--all-extras` or `--all-groups` for that set, does not change the arrays.

Input mismatches that can be proved without resolving fail early. Otherwise
the full resolve runs, so `decision-order = "arrival"` can produce a different
answer on a colder cache.

Use `decision-order = "stable"` for this check. See [Use a lock](../how-to/use-the-lock.md) for the CI command and the [CLI reference](cli.md) for mismatch behavior.

## `nab download`

`nab download` resolves project inputs again; it does not read an existing
lock. It fetches the resulting wheels, sdists, and direct-URL archives into
`--output` (default `wheels/`) and verifies each file against one selected
recorded digest. A local file matching that digest is kept; local and VCS pins
are skipped.

The directory is an artifact set for that fresh resolve, not necessarily a
complete offline installation set. Skipped sources still need their original
locations, and an sdist may need build requirements not present there. See
[Use a lock](../how-to/use-the-lock.md) for an exact one-environment offline
workflow and the [CLI reference](cli.md) for download options.

[PEP 751]: https://peps.python.org/pep-0751/
[PEP 658]: https://peps.python.org/pep-0658/
[hash-checking]: https://pip.pypa.io/en/stable/topics/secure-installs/#hash-checking-mode
