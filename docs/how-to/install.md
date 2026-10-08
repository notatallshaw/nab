# Install nab

`nab` ships as a CLI plus five importable libraries; see [the six distributions](../explanation/packages.md). The recommended path is to install it as an isolated tool.

## uv tool install

```bash
uv tool install nab
```

Drops `nab` into a uv-managed tool venv and exposes the console script on `PATH`. uv resolves and installs all six distributions together. Confirm with:

```bash
nab --version
```

`nab` runs on CPython 3.10 and newer. Other interpreters are not tested.

## pipx

```bash
pipx install nab
```

pipx creates a per-tool virtual environment. The default backend is `uv` on recent pipx; the `pip` backend works equivalently.

## Picking an HTTP backend

`nab-index` ships urllib3 by default. Install an extra to use httpx or httpx2:

```bash
pip install 'nab[httpx2]'
nab lock --http-backend httpx2
```

The selected backend also fetches build dependencies, including dependencies of nested builds.

For httpx, install `nab[httpx]` and select `--http-backend httpx`. Each extra includes the `h2` package for HTTP/2 support. Missing dependencies produce an installation hint when selecting the backend.

All backends use system certificates through truststore and send `User-Agent: nab-index/<version>`.

## Throw-away invocations

```bash
uvx nab --help
pipx run nab --help
```

Both fetch the wheel into an ephemeral environment and run the CLI against it.

## Installing from a checkout

To run a revision that has not been released, build the six wheels and install from the result:

```bash
git clone https://github.com/notatallshaw/nab.git
cd nab
mkdir -p /tmp/nab-wheels
uv build --wheel --out-dir /tmp/nab-wheels nab-resolver
uv build --wheel --out-dir /tmp/nab-wheels nab-markersets
uv build --wheel --out-dir /tmp/nab-wheels nab-provider
uv build --wheel --out-dir /tmp/nab-wheels nab-index
uv build --wheel --out-dir /tmp/nab-wheels nab-project
uv build --wheel --out-dir /tmp/nab-wheels .
uv tool install --find-links /tmp/nab-wheels nab
```

Once `nab --version` succeeds, [make your first lock](../tutorial/getting-started.md). To work on nab itself, see [contributing](../contributing.md).
