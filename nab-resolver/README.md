# nab-resolver

Generic PubGrub dependency resolver, parameterised over a
`ResolverProvider` protocol.  No Python-specific knowledge: this
package is a SAT-style solver core.  The Python provider lives in
[`nab-python`](https://pypi.org/project/nab-python/) and the
user-facing CLI in [`nab`](https://pypi.org/project/nab/).

It has no runtime dependencies: the standard library is all it needs.

## When to use it

Use `nab-resolver` when you are building some kind of package
resolver, Python or otherwise.

## The public API

The supported API is the module paths below.  They will not move
without a major version bump.  Everything else in the package is
internal and may be renamed or relocated in any release.

```text
nab_resolver.errors     ResolutionError
nab_resolver.ranges     Range
nab_resolver.resolver   BaseProvider, DEFAULT_MAX_ITERATIONS, Resolver,
                        ResolverObserver, ResolverProvider
nab_resolver.root       ROOT
nab_resolver.types      Incompatibility, IncompatibilityCause,
                        RangeProtocol, RootRequirement, Term
```

The package root binds no names, so importing `nab_resolver` pulls in
no submodules and a caller loads only what it imports.
