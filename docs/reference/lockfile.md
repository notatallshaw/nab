# Lockfiles

nab resolves packages but does not install them. Its lock records the pinned versions, artifact
locations, and digests for another tool to consume.

`nab lock` writes one of three formats from the same in-memory result:

- `--format pylock` (default): a [PEP 751] `pylock.toml`.
- `--format requirements`: a pip-compatible `requirements.txt`, sorted by name. Index pins carry
  recorded digests; local, VCS, and archive pins are URL lines.
- `--format requirements-without-hashes`: the same output without separate `--hash` lines. An
  archive URL still carries its digest.

Pass `--output -` to write to stdout instead of a file. See [Use a lock](../how-to/use-the-lock.md)
for installation workflows.

## What is in a lock

The lock records package names and versions, their sources (index, local directory, Git or hashed
archive), and the artifacts allowed by the distribution policy and target. Artifact records carry
locations and hashes.

Index digests come from the listing; nab does not fetch every index artifact to re-hash it. A direct
archive's digest comes from its configured URL and is verified before metadata is read.

### Metadata selection

When several wheels fit the target, nab reads the one whose PEP 425 tags rank highest. It prefers a
[PEP 658] metadata sidecar, then reads the wheel itself through HTTP ranges or a full fetch. With no
wheel, it reads the sdist; dynamic metadata may require a [permitted build](build-policy.md).

Local wheels are read from disk; VCS and archive sources are materialised to read their metadata.

A wheel the target's PEP 425 tags reject was never a candidate, so it is not in the lock: a lock
resolved on `linux_x86_64` carries no `win_amd64` wheels.

## PEP 751 `pylock.toml`

The PEP 751 lockfile is the cross-tool format produced by `nab lock --format pylock` and checked by
`nab lock --locked`. `nab download` does not consume it; that command resolves project inputs again.

Nab records the selected extras and groups, environment declarations, package sources and artifact
digests. See the
[pylock specification](https://packaging.python.org/en/latest/specifications/pylock-toml/) for the
schema and installer rules.

[Lockfile extras and groups](lockfile-groups.md) explains Nab's selection settings.
[Lockfile environments](lockfile-environments.md) describes target declarations and patch-release
slices.

### Portable paths

A filesystem path is written relative to the lock's directory with POSIX separators. This covers
local sources and workspace members in `packages.directory.path`, plus local-index and find-links
artifacts in `packages.wheels.path` or `packages.sdist.path`.

PEP 751 resolves the path against the lock, so both can move together. A Windows path on another
drive stays absolute. With `--output -`, paths are relative to the current directory instead.

Configured URLs have embedded credentials removed. Archive sources use `packages.archive.url`
without the fragment, with hashes and any subdirectory recorded separately in
`packages.archive.hashes` and `packages.archive.subdirectory`. Local indexes use `packages.index`,
and local repositories use `packages.vcs.url`. A `file://` URL is not made relative, so a lock that
carries one is usable only where that location exists.

## pip-compatible `requirements.txt`

`nab lock --format requirements` writes one line per package using pip's [hash-checking] format:

```
fastapi==0.109.1 \
    --hash=sha256:... \
    --hash=sha256:...
starlette==0.35.1 \
    --hash=sha256:... \
    --hash=sha256:...
```

Each recorded digest gets one `--hash` line. nab records `sha256`, `sha384`, and `sha512`, then
sorts by algorithm and digest rather than artifact.

Output therefore does not depend on discovery order. Continuation backslashes match pip-compile.

### Source URL lines

Local and VCS pins have no hashes. Editable local pins use `-e`; workspace members are editable by
default, while `[[tool.nab.local-sources]]` entries are not unless configured. A subdirectory uses
`#subdirectory=`.

Archive pins put their accepted digests in the URL fragment, with any subdirectory appended by `&`.
The examples below use `sha256`. Local, VCS, and archive pins render the same in both requirements
formats:

```
my-fork @ file:///abs/path/to/checkout
-e file:///abs/path/to/monorepo#subdirectory=packages/foo
some-pkg @ git+https://github.com/me/x.git@<sha>
my-archive @ https://example.com/my-archive-1.0.tar.gz#sha256=<hex>
mono @ https://example.com/mono-3.0.tar.gz#sha256=<hex>&subdirectory=packages/foo
```

pip's [hash-checking] mode accepts the file only when every dependency is hashed. Local and VCS pins
make the output mixed and therefore unsuitable for that mode.
[Use a lock](../how-to/use-the-lock.md) gives the install commands.

[Reproducible locks](reproducibility.md) covers upload cutoffs, stable decision order and
`nab lock --locked` in CI.

## `nab download`

`nab download` resolves project inputs again; it does not read an existing lock. It fetches the
resulting wheels, sdists, and direct-URL archives into `--output` (default `wheels/`) and verifies
each file against one selected recorded digest. A local file matching that digest is kept; local and
VCS pins are skipped.

The directory is an artifact set for that fresh resolve, not necessarily a complete offline
installation set. Skipped sources still need their original locations, and an sdist may need build
requirements not present there. See [Use a lock](../how-to/use-the-lock.md) for an exact
one-environment offline workflow and the [CLI reference](cli.md) for download options.

```{toctree}
---
hidden:
---
lockfile-groups
lockfile-environments
reproducibility
```

[hash-checking]: https://pip.pypa.io/en/stable/topics/secure-installs/#hash-checking-mode
[pep 658]: https://packaging.python.org/en/latest/specifications/simple-repository-api/
[pep 751]: https://packaging.python.org/en/latest/specifications/pylock-toml/
