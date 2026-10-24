# Install nab

Install nab as an isolated tool. It runs on CPython 3.10 and newer.

## uv tool install

```bash
uv tool install nab
```

Check the installation:

```bash
nab --version
```

## pipx

```bash
pipx install nab
```

## Run with uvx

```bash
uvx nab lock pyproject.toml
```

Replace `nab` with `uvx nab` in commands throughout these guides.

With pipx, use `pipx run nab lock pyproject.toml`.

## Picking an HTTP backend

`nab-index` ships urllib3 by default. Install an extra to use httpx or httpx2:

```bash
uv tool install 'nab[httpx2]'
nab lock --http-backend httpx2
```

The selected backend also fetches build dependencies, including dependencies of nested builds.

For httpx, install `nab[httpx]` and select `--http-backend httpx`. Each extra includes the `h2`
package for HTTP/2 support. Missing dependencies produce an installation hint when selecting the
backend.

All backends use system certificates through truststore.

## Installing from a checkout

Use the [development install](../contributing.md) to run an unreleased revision from a checkout.

Once `nab --version` succeeds, [make your first lock](../tutorial/getting-started.md).
