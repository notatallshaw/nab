# nab-markersets

A PEP 508 marker read as the set of environments it selects. `packaging`
answers "does this marker hold here"; this answers "can these two ever both
hold", "does one imply the other", and "is this a contradiction".

**Experimental.** Anything here can change in any release, so pin an exact
version. The intent is to land this algebra in `packaging` itself, and the
shape below is what that proposal is being tried against; the discussion is
[pypa/packaging#448](https://github.com/pypa/packaging/issues/448).

```pycon
>>> from nab_markersets.markersets import MarkerSet
>>> old = MarkerSet.from_marker('python_version < "3.11"')
>>> new = MarkerSet.from_marker('python_version >= "3.12"')
>>> old.is_disjoint(new)
True
```

A set also holds what a marker string cannot: the full set of an absent
marker, the empty set of a contradiction, and complements the grammar cannot
spell. `witness` returns a point in the set, which is what separates two
markers that look like one constraint.

```pycon
>>> minor = MarkerSet.from_marker('python_version >= "3.11"')
>>> exact = MarkerSet.from_marker('python_full_version >= "3.11.0"')
>>> minor.equivalent(exact)
False
>>> (minor & ~exact).witness()["python_full_version"]
'3.11.0.dev0'
```

## Installing

The engine runs on `packaging`'s parse tree and its single-atom evaluator, and
two copies of `packaging` exist: the released one, and the fork
[`nab`](https://pypi.org/project/nab/) vendors. An extra picks which.

```bash
pip install "nab-markersets[packaging]"
pip install "nab-markersets[nab-vendored-packaging]"
```

The first copy at `packaging>=26.3` is bound, so with both installed the fork
wins and a `Marker` built inside nab keeps the class the algebra tests against.
A `Marker` from the other copy is accepted too. With nothing that new,
importing `nab_markersets.markersets` fails and says what it found.

## When to use it

Whether two lock entries can both apply, whether a dependency is reachable
inside your `requires-python`, or what a marker still says once you fix the
platform. The guide walks through those, and through what the decisions do not
decide:
<https://nab.readthedocs.io/en/stable/how-to/reason-about-markers.html>

## The public API

The supported API is the module paths below. Everything else in the package is
internal and may be renamed or relocated in any release.

```text
nab_markersets.errors       IntractableMarkerSet, UnserializableMarkerSet
nab_markersets.markersets   DecisionStore, MarkerSet, variable_names
```

The package root binds no names, so importing `nab_markersets` pulls in no
submodules.

Two things before you call it. A `MarkerSet` comes from `from_marker`, `full`
or `empty`, so `pickle.loads` fails on one: the constructor it reaches for
refuses. And `==` is structural, over the tree the set was built from, where
`equivalent` is the semantic test.
