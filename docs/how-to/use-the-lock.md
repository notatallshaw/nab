# Use a lock

nab resolves and records dependencies; an installer consumes the result.
Choose the path that matches the output you need.

## Install from `pylock.toml`

pip 26.1 and newer can install the dependencies selected from a PEP 751
lock:

```bash
python -m pip install -r pylock.toml
```

pip's `pylock.toml` support is experimental. It selects packages for the
current interpreter and platform, uses the lock's
default dependency groups, and selects no extras.

The command installs the locked dependencies, not the project that
produced the lock. See
[Selecting what to lock](../reference/selection.md) before using extras,
non-default groups, or workspace members.

## Install from hashed requirements

Write pip requirements and require every selected file to match the
lock's hashes:

```bash
nab lock --format requirements pyproject.toml
python -m pip install --require-hashes -r requirements.txt
```

The file records hashes, not the index that supplied each package.
Configure pip to select each package from the same index nab used.

Use this path only when every requirement is hash-checkable. nab refuses
the format if a recorded index file lacks an accepted hash; archive pins
carry their digest in the URL. Local and VCS pins are not hashable.

For mixed sources, `requirements-without-hashes` removes hash lines from
index pins and cannot use pip's `--require-hashes` mode. See
[Output formats](../reference/formats.md) for the exact line shapes.

## Build an offline wheelhouse

For one environment, start with hashed requirements containing only
index pins. Download one compatible wheel for every pin:

```bash
python -m pip download --only-binary=:all: --require-hashes \
    --dest wheelhouse -r requirements.txt
```

Run that command with the index configuration that selects the files nab
hashed, and with the Python and platform you will install on.

It fails if any pin has no compatible wheel, instead of leaving an sdist
whose build dependencies may be missing offline.

Install from the directory without consulting an index:

```bash
python -m pip install --no-index --find-links wheelhouse \
    --require-hashes -r requirements.txt
```

This workflow does not cover local, VCS, or direct archive lines. Those
lines name their source directly rather than selecting a wheel from
`wheelhouse`.

## Check a committed lock in CI

For a single-environment `pylock.toml`, re-resolve and fail if the file
is missing or would change:

```bash
nab lock --locked pyproject.toml
```

The check writes nothing. See [`nab lock`](../reference/cli.md) for its
reproducibility requirements and unsupported modes.

## Download after a fresh resolve

`nab download pyproject.toml` resolves the project again, then fetches
the selected wheels, sdists, and direct archives.
It does not read `pylock.toml`, create `requirements.txt`,
or fetch local and VCS sources.

Use it to collect the files selected across the configured targets. If
the files must come from an existing lock, use one of the workflows
above. See the [lockfile reference](../reference/lockfile.md) for the
exact download output.
