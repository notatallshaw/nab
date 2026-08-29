# nab-markersets

A PEP 508 marker read as the set of environments it selects.
`packaging` answers "does this marker hold here"; this answers "can
these two ever both hold", "does one imply the other", and "is this a
contradiction". `packaging` is its only dependency.

```pycon
>>> from nab_markersets.markersets import MarkerSet
>>> old = MarkerSet.from_marker('python_version < "3.11"')
>>> new = MarkerSet.from_marker('python_version >= "3.12"')
>>> old.is_disjoint(new)
True
```

A set also holds what a marker string cannot: the full set of an absent
marker, the empty set of a contradiction, and complements the grammar
cannot spell. `witness` returns a point, which is how two markers that
look like one constraint give up the interpreter that separates them.

```pycon
>>> minor = MarkerSet.from_marker('python_version >= "3.11"')
>>> exact = MarkerSet.from_marker('python_full_version >= "3.11.0"')
>>> minor.equivalent(exact)
False
>>> (minor & ~exact).witness()["python_full_version"]
'3.11.0.dev0'
```

## When to use it

When markers have to be reasoned about rather than tested against one
environment: whether two lock entries can both apply, whether a
dependency is reachable inside your `requires-python`, or what a set of
environment rows leaves uncovered. It is what
[`nab`](https://pypi.org/project/nab/) uses for markers. The guide walks
through those, and through what the decisions do not decide:
<https://nab.readthedocs.io/en/stable/how-to/reason-about-markers.html>

## The public API

The supported API is the module paths below. They will not move without
a major version bump. Everything else in the package is internal and may
be renamed or relocated in any release.

```text
nab_markersets.errors       IntractableMarkerSet, UnserializableMarkerSet
nab_markersets.markersets   DecisionStore, MarkerSet, variable_names
```

The package root binds no names, so importing `nab_markersets` pulls in
no submodules and a caller loads only what it imports.

Three things to know before you call it. A `MarkerSet` is built through
`from_marker`, `full` or `empty`, so it does not survive `pickle`, which
reaches for the constructor. `==` is structural, over the tree the set
was built from, and `equivalent` is the semantic test. And emptiness
enumerates representative points rather than solving, so two
constructions read wrong in opposite directions; `MarkerSet`'s own
docstring shows both.

The engine reads three private `packaging` names so a marker means here
exactly what it means there, which is why the dependency carries a
ceiling. The API is under rapid experimentation: pin an exact version.
