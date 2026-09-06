# nab-resolver

Generic PubGrub dependency resolver, parameterised over a
`ResolverProvider` protocol.  No Python-specific knowledge: this
package is a SAT-style solver core.  The Python provider lives in
[`nab-provider`](https://pypi.org/project/nab-provider/) and the
user-facing CLI in [`nab`](https://pypi.org/project/nab/).

It has no runtime dependencies: the standard library is all it needs.

## When to use it

Use `nab-resolver` when you are building some kind of package
resolver, Python or otherwise.  There is a worked example in the docs:
<https://nab.readthedocs.io/en/stable/how-to/embed-the-resolver.html>

## The public API

The supported API is the module paths below.  They will not move
without a major version bump.  Everything else in the package is
internal and may be renamed or relocated in any release.

```text
nab_resolver.candidate_provider
                       CandidateHost, CandidateProvider,
                       CandidateRequirement, PreparedCandidate
nab_resolver.errors     ProvisionalResolutionError, ResolutionError
nab_resolver.priority   CONFLICT_THRESHOLD, CULPRIT_DEMOTE_THRESHOLD,
                        MAX_PRECHECK_BACKTRACKS, PRECHECK_REJECTION_THRESHOLD,
                        TIER_AFFECTED, TIER_CULPRIT, TIER_NORMAL,
                        compute_tier, is_dominant_culprit
nab_resolver.ranges     Range
nab_resolver.resolver   BaseProvider, DEFAULT_MAX_ITERATIONS, Resolver,
                        ResolverObserver, ResolverProvider, Solution
nab_resolver.root       ROOT
nab_resolver.types      Incompatibility, IncompatibilityCause,
                        RangeProtocol, RootRequirement, Term
```

The package root binds no names, so importing `nab_resolver` pulls in
no submodules and a caller loads only what it imports.
