# Install nab

`nab` ships as a CLI plus five importable libraries; see
[the six distributions](../explanation/packages.md). The recommended path is to install it as an
isolated tool.

## uv tool install

```bash
uv tool install nab
```

Installs `nab` in a uv-managed tool environment and exposes the console script on `PATH`. uv
resolves and installs all six distributions together. Confirm with:

```bash
nab --version
```

`nab` runs on CPython 3.10 and newer. Other interpreters are not tested.

## pipx

```bash
pipx install nab
```

pipx creates a per-tool virtual environment and exposes `nab` on `PATH`.

## Run without installing a persistent command

```bash
uvx nab lock pyproject.toml
```

[`uvx` is an alias for `uv tool run`](https://docs.astral.sh/uv/guides/tools/). It runs nab in an
isolated tool environment. Replace `nab` with `uvx nab` in commands throughout these guides.

With pipx, use `pipx run nab lock pyproject.toml`.

## Picking an HTTP backend

`nab-index` ships urllib3 by default. Install an extra to use httpx or httpx2:

```bash
pip install 'nab[httpx2]'
nab lock --http-backend httpx2
```

The selected backend also fetches build dependencies, including dependencies of nested builds.

For httpx, install `nab[httpx]` and select `--http-backend httpx`. Each extra includes the `h2`
package for HTTP/2 support. Missing dependencies produce an installation hint when selecting the
backend.

All backends use system certificates through truststore and send `User-Agent: nab-index/<version>`.

## Installing from a checkout

Use the [development install](../contributing.md) to run an unreleased revision from a checkout.
Hatch installs the six workspace distributions together.

Once `nab --version` succeeds, [make your first lock](../tutorial/getting-started.md).
