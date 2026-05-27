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
* one of three pin shapes: an `IndexPin` (PyPI or another simple
  index), a `LocalPin` (a directory on disk), or a `VcsPin` (a
  git URL with a commit pin),
* every artefact downloaded for that pin (`sdist` and `wheels`),
  each with its filename, URL, `sha256`, and optional size.

The resolver records exactly what it considered, so
`nab download` can fetch the same files back without consulting
the index again.

## PEP 751 `pylock.toml`

The PEP 751 lockfile is the format for cross-tool Python
lockfiles. It is what `nab lock --format pylock` produces and
what `nab download` consumes when handed a lockfile.

A trimmed example, after resolving the
[getting-started](getting-started.md) project:

```toml
lock-version = "1.0"
created-by = "nab"
requires-python = ">=3.10"

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

### Universal mode

Under `[tool.nab].mode = "universal"`, `nab lock --format pylock`
writes a single file covering every `(python, platform)` tuple
in the matrix. Packages whose pinned version is identical across
every tuple appear once with no marker; packages that diverge
appear as multiple `Package` entries with PEP 508 markers built
from each tuple's `python_version`, `sys_platform`, and
`platform_machine`.

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
behaviour:

```
my-fork @ file:///abs/path/to/checkout
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

## `nab download`

`nab download` walks the same pin set, fetches every wheel and
sdist into the `--output` directory (defaults to `wheels/`), and
verifies each file's `sha256` against the recorded digest. Local
and VCS pins are skipped. The download is idempotent: a file
whose digest already matches a local copy is left alone.

The result is a per-resolve directory of artefacts that any
installer can consume offline:

```bash
nab download
pip install --no-index --find-links wheels/ -r requirements.txt
```

This pairs naturally with `--require-hashes`: hashes are baked
into the requirements file, the artefacts on disk are the exact
ones verified during resolve, and pip refuses anything else.

[PEP 751]: https://peps.python.org/pep-0751/
[hash-checking]: https://pip.pypa.io/en/stable/topics/secure-installs/#hash-checking-mode
