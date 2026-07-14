# Lockfiles

nab is a resolver, not an installer. The artefact it produces is
a lockfile: a file that records every pinned version, every
artefact URL, and every digest needed to reproduce the same
install elsewhere.

`nab lock` writes one of three formats from the same in-memory
result:

* `--format pylock` (default): a [PEP 751] `pylock.toml`.
* `--format requirements`: a pip-compatible `requirements.txt`
  with `--hash=sha256:...` per recorded artefact.
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
metadata, and it takes the cheapest source available: a wheel's
[PEP 658] metadata sidecar first, then the wheel itself when the
index publishes no sidecar, and the sdist only when no wheel is
published (built, if its dependencies are dynamic and the
[build policy](build-policy.md) allows it). VCS and archive sources
are cloned or downloaded for the same reason.

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
  `requires-python`, `environments`, `extras`, `packages`).
* The `[[packages]]` shape: `name`, `version`, optional `index`,
  `sdist`, and `wheels`.
* `sha256`, `sha384`, and `sha512` digests on every recorded
  artefact. Whatever the index publishes is forwarded; nab
  requires at least one of the three so the lockfile is
  consumable by pip's hash-checking mode.

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
[resolve target](configuration.md), or one per tuple of a declared
matrix. Every dependency whose PEP 508 marker was false on a target
was dropped there, so the pins are not a package set another
environment can install. The lock says so in the top-level
`environments`, one entry per environment resolved, and a PEP 751
consumer refuses a lock whose declared environments none of its own
satisfies.

Each declaration always pins `python_version`, `sys_platform` and
`platform_machine`, plus `implementation_name` when the target runs
an interpreter other than CPython or the matrix models more than one.
It also pins every other PEP 508 variable that a marker in the resolve
consulted: a dependency, root requirement, or constraint marker on
`platform_system` pins `platform_system`. This is deliberately narrow.
A marker nab evaluated is a question whose answer changed the package
set, so an installer that answers it differently must not use this
lock.

`python_full_version` is the exception: it is declared by
constraint, not by value. The pins do not depend on the micro
release, they depend on how each marker clause reading it answered,
so that is what the lock declares. A clause that held is declared as
it stands; one that did not is declared complemented, since PEP 508
has no `not`:

```text
tomli ; python_full_version <= "3.11.0a6"   read false
    -> python_full_version > "3.11.0a6"
```

The lock is then installable on every micro release that reads the
resolve's markers the way the resolve did, and a marker that
genuinely splits the micros (`python_full_version >= "3.13.4"`)
still partitions them. Pinning the value instead would refuse every
other micro, including every real one when the target names a minor:
`--python 3.13` synthesizes `3.13.0`, which no released interpreter
reports. A clause whose complement cannot be stated as a clause (an
unusual operator such as `~=`, or a PEP 440 prerelease boundary)
falls back to pinning the exact value.

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
`(python, platform, implementation)` tuple in the matrix. Packages
whose pinned version is identical across every tuple appear once with
no marker; packages that diverge appear as multiple `Package` entries
with PEP 508 markers built from each tuple's `python_version`,
`sys_platform` and `platform_machine`, plus `implementation_name` on
the same terms as the `environments` declarations above.

`environments` carries one declaration per tuple, built the same way
a single-environment lock builds its one: from the markers that
tuple's resolve consulted. A tuple's `packages.dependencies` edges
are the union across the tuples an entry covers.

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

The hash count per package equals the number of artefacts
recorded in the resolve. An sdist hash, when present, comes
first; wheel hashes follow. The continuation backslashes match
what pip-compile emits.

Local and VCS pins are rendered without hashes, mirroring pip's
behaviour. An editable local pin becomes a `-e` line and a
`subdirectory` a `#subdirectory=` fragment:

```
my-fork @ file:///abs/path/to/checkout
-e file:///abs/path/to/monorepo#subdirectory=packages/foo
some-pkg @ git+https://github.com/me/x.git@<sha>
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

## `nab download`

`nab download` resolves the project again, then fetches every wheel
and sdist on the resulting pins into the `--output` directory
(defaults to `wheels/`), verifying each file's `sha256` against the
digest the index published. Local and VCS pins are skipped. The
download is idempotent: a file whose digest already matches a local
copy is left alone.

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
