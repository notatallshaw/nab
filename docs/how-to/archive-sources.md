# Use a wheel or source archive

Use a wheel or source archive when a package must resolve from a fixed URL instead of an index. ZIP
and tar archives are supported, including gzip, bzip2 and xz compression when Python provides the
corresponding reader.

## Declare and lock the source

Name the package as a project dependency, then map it to the archive:

```toml
[project]
dependencies = ["my-fork"]

[develop.sources]
my-fork = { url = "https://example.com/my-fork-1.0.tar.gz", hashes = { sha256 = "<hex>" } }

```

The entry permits its URL; no second allowlist is needed. A local artifact declared with `path`
works the same way. Permission applies to that selected source, not to other dependencies' URLs.

`archive.policy = "declared"` is the default. Set `policy = "block"` under `[tool.nab.archive]` to
refuse even declared archives and wheels. For backend requirements and library callers admitting
other direct URLs, `policy = "allow"` uses `allowed-urls` for additional permitted locations.

`require-hashes = true` is the default. Declare `hashes` to reject changed contents; nab verifies
every supported algorithm listed. These settings also apply to project-root direct URLs. Normal
artifacts selected from configured indexes use index policy instead.

The standard table permits omitting hashes. To accept this deliberately, add
`require-hashes = false` under `[tool.nab.archive]`. nab then records the downloaded bytes' SHA-256
in the lock, but a new lock can accept changed bytes. A previously observed digest does not become
an expected input hash.

Inspect the effective permission with `nab config explain archive --path pyproject.toml`. An unused
table entry does not require acquisition permission.

Lock and install the result:

```bash
nab lock pyproject.toml
python -m pip install -r pylock.toml
```

The second command needs pip 26.1 or newer, whose `pylock.toml` support is experimental. See
[Use a lock](use-the-lock.md) for its selection limits and a hashed-requirements alternative.

The lock records the URL and digest. A mismatch ends the resolve; nab does not fall back to an index
because the declared archive is the package's only candidate.

## Use a wheel

```toml
[develop.sources]
my-fork = { url = "https://example.com/my_fork-1.0-py3-none-any.whl", hashes = { sha256 = "<hex>" } }

```

A `.whl` filename must be a valid wheel filename. The wheel's name and version must match its
metadata, and its tags must fit the target. nab reads static metadata without invoking a backend.
Wheels cannot use `subdirectory` or `editable`.

A local wheel or archive can use `path` instead of `url`.

## Select a subdirectory

For a package below the archive root, set `subdirectory`:

```toml
[develop.sources]
my-fork = { url = "https://example.com/monorepo-1.0.tar.gz", subdirectory = "packages/my-fork", hashes = { sha256 = "<hex>" } }
```

## Set the build policy

Archive permission allows fetching, not backend execution. `dist-policy = "wheel-only"` refuses
source archives; `sdist-only` and `sdist-install` refuse wheels. Per-package overrides still apply.
Directory and Git sources select projects rather than distribution artifacts.

nab reads static `[project]` metadata at every build-policy level. Missing or dynamic metadata needs
`build-policy = "build-remote"`.

Under `never` or `build-local`, that dynamic source ends the resolve because no index candidate can
replace it. See [Build policy](../reference/build-policy.md) for the metadata boundary and
[Configuration](../reference/configuration.md) for cache and `file://` behavior.
