# nab-markersets

PEP 508 marker algebra: a marker read as the set of environments it
selects, so markers can be combined and compared instead of only
evaluated.

`MarkerSet` holds the states a marker string cannot: the full set of an
absent marker, the empty set of a contradiction, and complements the
grammar cannot spell. Intersection, union and complement are closed and
total. Emptiness, subset, superset, disjointness and equivalence are
decided, not approximated, by partitioning each referenced variable's
domain into cells on which every atom is constant and evaluating the
marker once per cell. `to_marker_string` is the one boundary back to the
grammar, and the one partial operation.

Parsing and evaluation come from
[`packaging`](https://pypi.org/project/packaging/), so a marker means
the same thing here as it does there. It is the only dependency.

## When to use it

Use `nab-markersets` when markers have to be reasoned about rather than
just tested against one environment: whether two requirements can apply
at once, whether one marker implies another, or what a lock's
environment rows leave uncovered. It is what
[`nab`](https://pypi.org/project/nab/) uses for markers.

## The public API

```text
nab_markersets.DecisionStore
nab_markersets.IntractableMarkerSet
nab_markersets.MarkerSet
nab_markersets.UnserializableMarkerSet
nab_markersets.variable_names
```

A `DecisionStore` is scratch that several decisions can share: build one,
pass it to any method that takes one, and drop it when that piece of
work is done. Answers never depend on it. Only the object is API;
what it holds is internal and unversioned.

`MarkerSet` is built through `from_marker`, `full` and `empty`; calling
the class raises `TypeError`. That also means an instance does not
survive `pickle`, which reaches for the constructor on load.

The API is currently under rapid experimentation, if you use it
pin to an exact version.
