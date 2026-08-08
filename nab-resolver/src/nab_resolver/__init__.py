"""PubGrub dependency resolver.

The names re-exported here are the supported API. The submodules they come
from are internal and can be renamed or moved without notice, so import from
``nab_resolver`` rather than from ``nab_resolver.resolver`` or
``nab_resolver.types``.
"""

from .errors import ResolutionError
from .ranges import Range
from .resolver import Resolver, ResolverProvider
from .root import ROOT
from .types import Incompatibility, RangeProtocol, Term

__all__ = [
    "ROOT",
    "Incompatibility",
    "Range",
    "RangeProtocol",
    "ResolutionError",
    "Resolver",
    "ResolverProvider",
    "Term",
]
