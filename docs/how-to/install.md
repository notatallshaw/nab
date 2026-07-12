# Install nab

`nab` ships as a CLI plus three importable libraries. The
recommended path is to install it as an isolated tool.

## uv tool install

```bash
uv tool install nab
```

Drops `nab` into a uv-managed tool venv and exposes the console
script on `PATH`. uv resolves and installs the four workspace
distributions (`nab`, `nab-resolver`, `nab-python`, `nab-index`)
together. Confirm with:

```bash
nab --version
```

`nab` runs on CPython 3.10 and newer. Other interpreters are not
tested.

## pipx

```bash
pipx install nab
```

pipx creates a per-tool virtual environment. The default backend
is `uv` on recent pipx; the `pip` backend works equivalently.

## Picking an HTTP backend

`nab-index` ships urllib3 by default. `httpx` is opt-in via an extra:

```bash
uv tool install 'nab[httpx]'
```

Pick it at run-time with `--http-backend httpx`. Selecting a backend
that was not installed surfaces a helpful `ImportError`.

## Throw-away invocations

```bash
uvx nab --help
pipx run nab --help
```

Both fetch the wheel into an ephemeral environment and run the CLI
against it.

## Installing from a checkout

To run a revision that has not been released, build the four wheels
from a checkout and install from the result:

```bash
git clone https://github.com/notatallshaw/nab.git
cd nab
mkdir -p /tmp/nab-wheels
uv build --wheel --out-dir /tmp/nab-wheels nab-resolver
uv build --wheel --out-dir /tmp/nab-wheels nab-python
uv build --wheel --out-dir /tmp/nab-wheels nab-index
uv build --wheel --out-dir /tmp/nab-wheels .
uv tool install --find-links /tmp/nab-wheels nab
```

To work on nab itself, see [contributing](../contributing.md).
