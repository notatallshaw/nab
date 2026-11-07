"""PubGrub dependency resolver.

The supported API is the module paths below.  They will not move without a
major version bump.  Everything else in the package is internal and may be
renamed or relocated in any release.

    nab_resolver.diagnostics DiagnosticMessage, Suggestion, error_message,
                             suggestion_lines
    nab_resolver.errors     ResolutionError, ResolutionInvariantError,
                            ResolutionLimitError,
                            ResolutionStalledError, ResolutionTerminatedError
    nab_resolver.predicate_ranges PredicateAlgebra, PredicateRange
    nab_resolver.priority   CONFLICT_THRESHOLD, CULPRIT_DEMOTE_THRESHOLD,
                            TIER_AFFECTED, TIER_CULPRIT, TIER_NORMAL,
                            compute_tier, is_dominant_culprit
    nab_resolver.proofs     DependencyCause, dependency_terms, external_clauses,
                            select_dependency_causes
    nab_resolver.ranges     Range
    nab_resolver.resolver   BaseProvider, DEFAULT_MAX_ITERATIONS, Resolver,
                            ResolverObserver, ResolverProvider, Solution
    nab_resolver.result     IncompleteSelectionError, walk_selected
    nab_resolver.root       ROOT
    nab_resolver.testing    ProviderScenario, check_provider,
                            check_provider_scenarios
    nab_resolver.types      DecisionRetreat, Incompatibility, IncompatibilityCause,
                            RangeProtocol, RootRequirement, Term
    nab_resolver.values     SlottedValue
    nab_resolver.version_spans OrderedVersion, cached_dependency_span

The package root binds no names, so importing ``nab_resolver`` pulls in no
submodules and a caller loads only what it imports.
"""
