# nab-python

nab's own host for [`nab-provider`](https://pypi.org/project/nab-provider/):
it supplies the I/O the resolution logic asks for, over
[`nab-index`](https://pypi.org/project/nab-index/), and adds everything a
command-line resolver needs beyond resolving.

It owns the `[tool.nab]` config ladder, workspace discovery, the PEP 517 build
path, the lockfile emitter and the downloader. The provider, the policies and
the resolve target live in `nab-provider`, which this package installs.

## When to use it

Use `nab-python` if you need to embed Python package resolution in another tool
and want nab's own fetching, config and lockfile emitter without the CLI. If
you already own your networking and want only the resolution logic, take
`nab-provider` instead.

The API is currently under rapid experimentation, use exact version
pinning.
