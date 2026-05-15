# nab-python

The Python package index provider that drives [`nab-resolver`](https://pypi.org/project/nab-resolver/)
for Python packages.

It owns all the mechanics of how the resolver needs to interact with standards
based Python package indexes and packages.

It implements build and distribution policies to allow the user to control
resolver and install behavior.

## When to use it

Use `nab-python` if you need to embed Python package resolution in
another tool and want the resolver, provider, and lockfile emitter
without the CLI.

The API is currently under rapid experimentation, use exact version
pinning.
