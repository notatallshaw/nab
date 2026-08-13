# nab-provider

The Python packaging provider that drives
[`nab-resolver`](https://pypi.org/project/nab-resolver/), with no I/O of its
own: no socket, no filesystem, no subprocess, no clock.

It owns the mechanics of how the resolver interacts with standards based
Python package indexes and packages, and implements the build, distribution
and VCS policies that let a user control resolver and install behavior.

Everything it needs arrives through `nab_provider.fetch_port.FetchPort`, which
its host implements; `nab_provider.testing` ships one over a store you fill
yourself.

## When to use it

Use `nab-provider` to embed nab's resolution in a host that already owns its
networking and caching, keeping
[`nab-index`](https://pypi.org/project/nab-index/) out of the import graph.
Most users want [`nab`](https://pypi.org/project/nab/) instead.

The API is currently under rapid experimentation, if you use it
pin to an exact version.
