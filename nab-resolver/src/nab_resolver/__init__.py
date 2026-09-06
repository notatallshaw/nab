"""PubGrub dependency resolver.

The supported API is the module paths below.  They will not move without a
major version bump.  Everything else in the package is internal and may be
renamed or relocated in any release.

    nab_resolver.candidate_provider
                           CandidateHost, CandidateProvider,
                           CandidateRequirement, PreparedCandidate
    nab_resolver.errors     ResolutionError
    nab_resolver.priority   CONFLICT_THRESHOLD, CULPRIT_DEMOTE_THRESHOLD,
                            TIER_AFFECTED, TIER_CULPRIT, TIER_NORMAL,
                            compute_tier, is_dominant_culprit
    nab_resolver.ranges     Range
    nab_resolver.resolver   BaseProvider, DEFAULT_MAX_ITERATIONS, Resolver,
                            ResolverObserver, ResolverProvider, Solution
    nab_resolver.root       ROOT
    nab_resolver.types      Incompatibility, IncompatibilityCause,
                            RangeProtocol, RootRequirement, Term

The package root binds no names, so importing ``nab_resolver`` pulls in no
submodules and a caller loads only what it imports.
"""
