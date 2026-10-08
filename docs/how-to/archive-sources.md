# Use a direct archive source

Declare a `.tar.gz` archive when one package must resolve from a fixed URL instead of an index.

## Declare and lock the source

Name the package as a project dependency, then map it to the archive:

```toml
[project]
dependencies = ["my-fork"]

[[tool.nab.archive-sources]]
name = "my-fork"
url = "https://example.com/my-fork-1.0.tar.gz#sha256=<hex>"
```

The fragment must contain at least one `sha256`, `sha384`, or `sha512` digest. The example uses `sha256`.

Lock and install the result:

```bash
nab lock pyproject.toml
python -m pip install -r pylock.toml
```

The second command needs pip 26.1 or newer, whose `pylock.toml` support is experimental. See [Use a lock](use-the-lock.md) for its selection limits and a hashed-requirements alternative.

The lock records the URL and digest. A mismatch ends the resolve; nab does not fall back to an index because the declared archive is the package's only candidate.

## Select a subdirectory

For a package below the archive root, add `subdirectory` to the same fragment:

```toml
[[tool.nab.archive-sources]]
name = "my-fork"
url = "https://example.com/monorepo-1.0.tar.gz#sha256=<hex>&subdirectory=packages/my-fork"
```

## Set the build policy

nab reads static `[project]` metadata at every build-policy level. Missing or dynamic metadata needs `build-policy = "build-remote"`.

Under `never` or `build-local`, that dynamic source ends the resolve because no index candidate can replace it. See [Build policy](../reference/build-policy.md) for the metadata boundary and [Configuration](../reference/configuration.md) for cache and `file://` behavior.
