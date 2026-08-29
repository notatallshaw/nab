"""The per-package index routing rule.

Kept out of :mod:`nab_project.fetch` so the config layer can name a route
without importing the coordinator and its async stack.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["IndexRoute"]


@dataclass(frozen=True, slots=True)
class IndexRoute:
    """Per-package index routing rule (a strict pin to one index).

    ``name`` is the package name (canonicalised internally).  ``index``
    is the *name* of an :class:`~nab_provider.records.IndexConfig`
    declared in the coordinator's ordered list.  Routing decides where to
    fetch a package's listing before any version is known, so a route
    carries no version scope and no marker; the override layer guarantees
    at most one route per package.
    """

    name: str
    index: str
