# nab-project

nab's own host for [`nab-provider`](https://pypi.org/project/nab-provider/):
it supplies the provider's I/O over
[`nab-index`](https://pypi.org/project/nab-index/), and adds the resolve
orchestration, the `[tool.nab]` config ladder, workspace discovery, the
PEP 517 build path, the lockfile emitter and the downloader.

## When to use it

Use `nab-project` to embed Python package resolution in another tool, with
nab's own fetching, config and lockfile emitter but without the CLI. If you
already own your networking, take `nab-provider` instead.

The API is currently under rapid experimentation, if you use it
pin to an exact version.
