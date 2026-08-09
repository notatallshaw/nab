"""PubGrub dependency resolver.

The supported API is the module paths below.  They will not move without a
major version bump.  Everything else in the package is internal and may be
renamed or relocated in any release.

    nab_resolver.errors     ResolutionError
    nab_resolver.ranges     Range
    nab_resolver.resolver   BaseProvider, DEFAULT_MAX_ITERATIONS, Resolver,
                            ResolverObserver, ResolverProvider, Solution
    nab_resolver.root       ROOT
    nab_resolver.types      Incompatibility, IncompatibilityCause,
                            RangeProtocol, RootRequirement, Term

The package root binds no names, so importing ``nab_resolver`` pulls in no
submodules and a caller loads only what it imports.
"""
