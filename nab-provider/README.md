# nab-provider

Apply Python packaging rules during dependency resolution. This package supplies candidate versions
and their dependency constraints to [`nab-resolver`](https://pypi.org/project/nab-resolver/),
including environment markers and build, distribution, and VCS policies.

It performs no networking, file access, or subprocess calls.

Package listings and metadata arrive through `nab_provider.fetch_port.FetchPort`, an interface
implemented by the application using the provider. That application is called the host;
`nab-project` supplies it for the nab CLI. `nab_provider.testing` supplies an in-memory
implementation for tests.

## When to use it

Use `nab-provider` to embed nab's resolution in a host that already owns its networking and caching,
keeping [`nab-index`](https://pypi.org/project/nab-index/) out of the import graph. Most users want
[`nab`](https://pypi.org/project/nab/) instead.

The API is currently under rapid experimentation, if you use it pin to an exact version.
