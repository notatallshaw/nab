# nab-resolver

Find a combination of package versions that satisfies their dependency constraints, using the
PubGrub algorithm. A `ResolverProvider` supplies the package rules, so the solver needs no
Python-specific knowledge. The Python provider lives in
[`nab-provider`](https://pypi.org/project/nab-provider/) and the user-facing CLI in
[`nab`](https://pypi.org/project/nab/).

It has no runtime dependencies: the standard library is all it needs.

## When to use it

Use `nab-resolver` when you are building some kind of package resolver, Python or otherwise. There
is a worked example in the docs:
<https://nab.readthedocs.io/en/stable/how-to/embed-the-resolver.html>

## The public API

The supported API is the module paths below. They will not move without a major version bump.
Everything else in the package is internal and may be renamed or relocated in any release.

```text
nab_resolver.errors     ResolutionError, ResolutionInvariantError,
                        ResolutionLimitError, ResolutionStalledError,
                        ResolutionTerminatedError
nab_resolver.ranges     Range
nab_resolver.resolver   BaseProvider, DEFAULT_MAX_ITERATIONS, Resolver,
                        ResolverObserver, ResolverProvider, Solution
nab_resolver.root       ROOT
nab_resolver.types      Incompatibility, IncompatibilityCause,
                        RangeProtocol, RootRequirement, Term
```

The package root binds no names, so importing `nab_resolver` pulls in no submodules and a caller
loads only what it imports.

## Checking preferred versions

`try_resolve_preferred` checks a proposed assignment against the current provider's version choices,
dependencies and emitted incompatibilities. It returns the reachable pins on success, or `None` when
ordinary resolution is needed.

```python
trial = Resolver(make_provider())
pins = trial.try_resolve_preferred(requirements, preferred, constraints)
if pins is None:
    pins = Resolver(make_provider()).resolve(requirements, constraints)
```

The trial consumes its resolver and provider. Create both again before falling back; provider
callbacks may have queued facts during the trial. Nab's project layer owns these attempts for each
target and retains package names and alignment preferences between targets.
