# nab-resolver

Generic PubGrub dependency resolver, parameterised over a
`ResolverProvider` protocol.  No Python-specific knowledge: this
package is a SAT-style solver core.  The Python provider lives in
[`nab-python`](https://pypi.org/project/nab-python/) and the
user-facing CLI in [`nab`](https://pypi.org/project/nab/).

## When to use it

Use `nab-resolver` when you are building some kind of package
resolver, Python or otherwise.

The public API is what the `nab_resolver` package exports: the
`Resolver` class, the `ResolverProvider` and `RangeProtocol`
protocols, the `Range`, `Term` and `Incompatibility` types, the
`ResolutionError` exception, and the `ROOT` sentinel.  Import them
from `nab_resolver` itself; its submodules are internal.
