# Lockfiles

nab resolves packages but does not install them. Its lock records the pinned versions, artifact
locations, and digests for another tool to consume.

Choose the output format with `--format`:

| Format                        | Output                                                                                |
| ----------------------------- | ------------------------------------------------------------------------------------- |
| `pylock` (default)            | A [PEP 751] `pylock.toml`                                                             |
| `requirements`                | A pip-compatible requirements file with recorded hashes                               |
| `requirements-without-hashes` | Requirements without separate `--hash` options; direct archive URLs keep their digest |

Pass `--output -` to write to stdout instead of a file. See [Use a lock](../how-to/use-the-lock.md)
for installation workflows.

## What is in a lock

The lock records package names and versions, their sources (index, local directory, Git or hashed
archive), and the artifacts allowed by the distribution policy and target. Artifact records carry
locations and hashes.

Index digests come from the listing; nab does not fetch every index artifact to re-hash it. A direct
archive's digest comes from its configured URL and is verified before metadata is read.

### Metadata selection

Nab reads the compatible wheel whose PEP 425 tags rank highest. Among equally ranked wheels, it
prefers one with a [PEP 658] metadata sidecar. Otherwise it reads `METADATA` with HTTP ranges or a
full wheel download.

With `dist-policy = "sdist-only"`, the default metadata source is the sdist. Set
`metadata-policy = "prefer-wheel"` to allow wheel metadata instead. Source metadata may require a
[permitted build](build-policy.md); see [Distribution policy](distribution-policy.md) for the
choices.

Local wheels are read from disk. VCS repositories and direct archives are prepared before reading
their metadata.

Artifact filtering follows the declared targets. A universal lock can include Windows wheels when
generated on Linux.

## PEP 751 `pylock.toml`

`nab lock --format pylock` writes the lock, and `nab lock --locked` checks it against project
inputs. `nab download` resolves the project again; it does not read this file.

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

Nab records `sha256`, `sha384` and `sha512` digests. Each gets a `--hash` option, sorted by
algorithm and digest. Continuation backslashes match pip-compile.

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

## Validation record

`tool.nab.validation` records compact input fingerprints bound to the standard lock contents.
Package selections, environments and sources remain in the standard Pylock fields.

For validation format 1, compute `contents-sha256` as follows:

1. Remove the top-level `tool` table from the parsed lock.
2. Encode the remaining data as JSON: sort keys, omit whitespace between items and escape non-ASCII
   characters.
3. Encode that JSON as UTF-8 and calculate its SHA-256 digest.

Datetime values use `YYYY-MM-DDTHH:MM:SS[.ffffff]` followed by any UTC offset. Include six
fractional digits when microseconds are nonzero; otherwise omit the fractional part. Write UTC as
`+00:00`.

On Python 3.11 or newer:

```python
import hashlib
import json
import tomllib
from datetime import datetime


def scalar_text(value):
    if isinstance(value, datetime):
        return datetime.isoformat(value)
    return str(value)


with open("pylock.toml", "rb") as stream:
    document = tomllib.load(stream)
validation = document.pop("tool")["nab"]["validation"]
assert validation["version"] == 1
encoded = json.dumps(
    document,
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=True,
    default=scalar_text,
).encode("utf-8")
assert hashlib.sha256(encoded).hexdigest() == validation["contents-sha256"]
```

[hash-checking]: https://pip.pypa.io/en/stable/topics/secure-installs/#hash-checking-mode
[pep 658]: https://packaging.python.org/en/latest/specifications/simple-repository-api/
[pep 751]: https://packaging.python.org/en/latest/specifications/pylock-toml/
