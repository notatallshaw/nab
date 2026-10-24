# nab-markersets

A dependency marker is a condition such as `python_version < "3.11"` that controls when a
requirement applies. This package compares PEP 508 markers as sets of environments. `packaging`
evaluates a marker against one environment. `nab-markersets` checks whether markers overlap, whether
one implies another, and whether a marker is contradictory.

**Experimental.** The API can change in any release, so pin an exact version.

```pycon
>>> from nab_markersets.markersets import MarkerSet
>>> old = MarkerSet.from_marker('python_version < "3.11"')
>>> new = MarkerSet.from_marker('python_version >= "3.12"')
>>> old.is_disjoint(new)
True
```

`MarkerSet` represents an absent marker as all supported environments and a contradiction as no
environments. It can also represent complements that PEP 508 cannot serialize. `witness` returns an
environment satisfying the set; the example below finds one where two markers differ.

```pycon
>>> minor = MarkerSet.from_marker('python_version >= "3.11"')
>>> exact = MarkerSet.from_marker('python_full_version >= "3.11.0"')
>>> minor.equivalent(exact)
False
>>> (minor & ~exact).witness()["python_full_version"]
'3.11.0.dev0'
```

## Installing

The engine runs on `packaging`'s parse tree and its single-atom evaluator. Two copies of `packaging`
exist: the released one, and the fork [`nab`](https://pypi.org/project/nab/) vendors. An extra picks
which.

```bash
pip install "nab-markersets[packaging]"
pip install "nab-markersets[nab-vendored-packaging]"
```

The first copy at version 26.3 or newer is used. The vendored fork is tried first, followed by
released `packaging`.

A `Marker` from the other copy is accepted too. If neither copy is new enough, importing
`nab_markersets.markersets` fails and says what it found.

## When to use it

Whether two lock entries can both apply, whether a dependency is reachable inside your
`requires-python`, or what a marker still says once you fix the platform. The guide covers those
tasks and the limits of its decisions:
<https://nab.readthedocs.io/en/stable/how-to/reason-about-markers.html>

## The public API

The supported API is the module paths below. Everything else in the package is internal and may be
renamed or relocated in any release.

```text
nab_markersets.conversions  MarkerSetCache, PreparedMarkerSets
nab_markersets.errors       IntractableMarkerSet, UnserializableMarkerSet
nab_markersets.markersets   DecisionStore, MarkerSet, variable_names
```

The package root binds no names, so importing `nab_markersets` pulls in no submodules.

`MarkerSet.from_marker_with_variables` returns a set and its original variable names from one parse.
Names include variables removed by normalization.

`pickle.dumps` can write a `MarkerSet`, but `pickle.loads` fails because direct construction is
refused. `==` compares construction trees; use `equivalent` to compare the environments two sets
select.
