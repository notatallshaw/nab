"""First-party marker algebra: reason about PEP 508 markers as sets.

A :class:`MarkerSet` is the denotation of a marker as a set of environments. It
supports the boolean algebra (``&`` / ``|`` / :meth:`~MarkerSet.complement`) and
the decision procedures (:meth:`~MarkerSet.is_empty`,
:meth:`~MarkerSet.is_disjoint`, :meth:`~MarkerSet.implies`,
:meth:`~MarkerSet.is_tautology`, :meth:`~MarkerSet.equivalent`), with
semantics matching packaging's marker evaluation. ``python_version`` is modeled
as the major.minor truncation of ``python_full_version``, so an environment
supplying both keys is read through ``python_full_version``. The engine is
on-demand cell decomposition, guarded by ``max_cells``.

External consumers import only this package root.
"""

from __future__ import annotations

from .errors import ComplexityLimitError, UnserializableSetError
from .markerset import MarkerSet

__all__ = [
    "ComplexityLimitError",
    "MarkerSet",
    "UnserializableSetError",
]
