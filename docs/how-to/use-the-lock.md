# Use a lock

nab resolves and records dependencies; an installer consumes the result.
Choose the path that matches the output you need.

| Starting point | Next step |
| --- | --- |
| A committed `pylock.toml` | Install from it with pip 26.1 or newer. |
| An index-only project needing `requirements.txt` | Resolve to hashed requirements, then install with `--require-hashes`. |
| Hashed requirements for an offline machine | Download compatible wheels before disconnecting. |
| A committed lock to check in CI | Re-resolve with `--locked` under the same target and settings. |

Run installation commands in the project's virtual environment, using a Python and platform covered by the lock. See {ref}`Getting started <install-the-locked-dependencies>` for environment setup.

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

If you want requirements output, resolve directly to that format and require every selected file to match its recorded hashes:

```bash
nab lock --format requirements pyproject.toml
python -m pip install --require-hashes -r requirements.txt
```

The first command performs a fresh resolve; it does not convert `pylock.toml`. Keep the generated `requirements.txt` if you want to reuse those exact pins.

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

(check-a-committed-lock-in-ci)=
## Check a committed lock in CI

Use this check for a single-environment `pylock.toml`. It fails if the file is missing or a fresh resolve would change it.

Set a stable search order before generating the lock you will commit:

```toml
[tool.nab]
decision-order = "stable"
```

Generate it locally with `nab lock pyproject.toml`, then run the check in CI with the same target and settings:

```bash
nab lock --locked pyproject.toml
```

The check writes nothing, but it can contact indexes and select newer releases even when `pyproject.toml` has not changed. It checks what a fresh resolve would choose, not just whether the old pins satisfy your declarations.

To exclude later uploads, configure `uploaded-prior-to` when generating the lock. See {ref}`Reproducibility <reproducibility>` for cutoff behavior and index changes it cannot freeze, and [`nab lock`](../reference/cli.md) for unsupported check modes.

## Download after a fresh resolve

`nab download pyproject.toml` resolves the project again, then fetches
the selected wheels, sdists, and direct archives.
It does not read `pylock.toml`, create `requirements.txt`,
or fetch local and VCS sources.

Use it to collect the files selected across the configured targets. If
the files must come from an existing lock, use one of the workflows
above. See the [lockfile reference](../reference/lockfile.md) for the
exact download output.
