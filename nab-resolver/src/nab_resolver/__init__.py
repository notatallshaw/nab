"""PubGrub dependency resolver.

This release supports the module paths below. Import paths and APIs can change
between 0.0.x releases. Other module paths are internal.

    nab_resolver.diagnostics DiagnosticMessage, Suggestion, error_message,
                             suggestion_lines
    nab_resolver.errors     ResolutionError, ResolutionInvariantError,
                            ResolutionLimitError,
                            ResolutionStalledError, ResolutionTerminatedError
    nab_resolver.priority   compute_tier
    nab_resolver.ranges     IntegerRangeMembership, Range
    nab_resolver.proofs     DependencyCause, dependency_terms, external_clauses,
                            select_dependency_causes
    nab_resolver.resolver   BaseProvider, DEFAULT_MAX_ITERATIONS, Resolver,
                            ResolverObserver, ResolverProvider, Solution
    nab_resolver.result     IncompleteSelectionError, walk_selected
    nab_resolver.root       ROOT
    nab_resolver.testing    ProviderConformanceError, ProviderScenario, check_provider,
                            check_provider_scenarios
    nab_resolver.types      Incompatibility, IncompatibilityCause,
                            RangeProtocol, RootRequirement, Term

The package root binds no names, so importing ``nab_resolver`` pulls in no
submodules and a caller loads only what it imports.
"""
