# Reason about markers

`nab-markersets` reads a PEP 508 marker as the set of environments it
selects. Two markers that cannot both hold are disjoint sets; a marker no
environment satisfies is the empty set; a marker you can drop is the full
one. `packaging` parses and evaluates markers against one environment, and
that is the operation this package adds to.

Install it with `pip install nab-markersets`.

## Can both of these apply?

A lock that lists a package twice has to narrow to one entry at install
time, so the two rows' markers must not overlap.

```pycon
>>> from nab_markersets.markersets import MarkerSet
>>> linux = MarkerSet.from_marker('sys_platform == "linux"')
>>> windows = MarkerSet.from_marker('sys_platform == "win32"')
>>> linux.is_disjoint(windows)
True
```

An absent marker is the full set, and a contradiction is the empty one.
Both are states a marker string cannot hold, which is why the factories
exist.

```pycon
>>> MarkerSet.from_marker('python_version < "3.8" and python_version >= "3.12"').is_empty()
True
>>> MarkerSet.from_marker('python_version >= "3.8" or python_version < "3.8"').is_full()
True
```

## Does one imply the other?

Implication is subset: every environment the first selects, the second
selects too.

```pycon
>>> supported = MarkerSet.from_marker('python_version >= "3.11"')
>>> MarkerSet.from_marker('python_version >= "3.12"').is_subset(supported)
True
>>> MarkerSet.from_marker('python_version >= "3.9"').is_subset(supported)
False
```

## Do these mean the same thing?

`==` on two sets is identity, so use `equivalent`.

```pycon
>>> short = MarkerSet.from_marker('python_version >= "3.11"')
>>> full = MarkerSet.from_marker('python_full_version >= "3.11"')
>>> short.equivalent(full)
False
```

`witness` returns an environment in the set, which is how you find out why.
Here `python_version` truncates 3.11.dev0 to "3.11", so the short marker
holds on a pre-release the long one excludes.

```pycon
>>> (short & ~full).witness()["python_full_version"]
'3.11.dev0'
```

## Working from a partly known environment

`restrict` substitutes the variables you know and leaves the rest.

```pycon
>>> both = MarkerSet.from_marker('sys_platform == "linux" and python_version >= "3.11"')
>>> both.restrict({"sys_platform": "linux"}).to_marker_string()
'python_version >= "3.11"'
>>> both.restrict({"sys_platform": "win32"}).is_empty()
True
```

## Making a marker smaller

`simplify` drops what a universe you name already rules out. Pass the
environments your project supports.

```pycon
>>> wide = MarkerSet.from_marker(
...     'python_version == "3.10" or python_version == "3.11"'
...     ' or python_version >= "3.10" and platform_system != "Linux"'
... )
>>> supported = MarkerSet.from_marker('python_version >= "3.9" and python_version < "3.12"')
>>> wide.simplify(within=supported).to_marker_string()
'python_version == "3.10" or python_version == "3.11"'
```

`MarkerSet.full()` as the universe gives a context-free factoring instead.
The result is not the smallest equivalent set, and it is not always
shorter: clauses are expanded before any come off, so a factored marker
whose clauses are all needed comes back expanded.

## Writing a set back out

`to_marker_string` is the one operation that can fail, because the set
algebra is closed and the grammar is not. It returns `None` for the full
set, and raises for the empty set and for a complement PEP 508 cannot
spell.

```pycon
>>> MarkerSet.full().to_marker_string() is None
True
>>> from nab_markersets.errors import UnserializableMarkerSet
>>> try:
...     (~MarkerSet.from_marker('python_version >= "3.11"')).to_marker_string()
... except UnserializableMarkerSet as exc:
...     print(exc)
no marker string spells the complement of python_version >= "3.11"
```

## Extras and dependency groups

`extra`, `extras` and `dependency_groups` hold a set of names, not one
name, so `extra == "gpu"` is a membership test. Two extras are not
mutually exclusive: asking for `pkg[cpu,gpu]` makes both true.

```pycon
>>> MarkerSet.from_marker('extra == "cpu"').is_disjoint(
...     MarkerSet.from_marker('extra == "gpu"')
... )
False
>>> MarkerSet.from_marker('extra != "gpu"').evaluate(
...     {"extra": frozenset({"cpu", "gpu"})}
... )
False
```

Names are PEP 685 normalised on both sides.

## Sharing work across decisions

A `DecisionStore` is scratch several decisions can share. Answers never
depend on it, so passing none is always correct. It is not safe to share
across threads; build one per piece of work and drop it after.

```pycon
>>> from nab_markersets.markersets import DecisionStore
>>> store = DecisionStore()
>>> linux.is_empty(store=store), linux.is_full(store=store)
(False, False)
```

## Does it hold here?

`evaluate` takes a whole environment, so a marker naming a variable the
environment omits raises rather than guessing. `extra` is the one that
catches people out: `packaging.markers.default_environment()` does not
supply it, and the empty set is what "no extras requested" means.

```pycon
>>> from packaging.markers import default_environment
>>> here = dict(default_environment(), extra=frozenset())
>>> MarkerSet.from_marker('extra == "gui"').evaluate(here)
False
```

## When a marker is too complex

Every decision runs under a cell budget and raises rather than hanging.

```pycon
>>> from nab_markersets.errors import IntractableMarkerSet
>>> axes = ("os_name", "sys_platform", "platform_machine",
...         "platform_system", "platform_release", "implementation_name")
>>> wide = MarkerSet.from_marker(" and ".join(
...     "(" + " or ".join(f'{axis} == "v{i}"' for i in range(12)) + ")"
...     for axis in axes
... ))
>>> try:
...     wide.is_empty()
... except IntractableMarkerSet as exc:
...     print(exc)
cell product exceeds max_cells=100000
```

`restrict` the variables you already know before deciding, and the axes it
substitutes away stop costing anything.

## What the decisions do not decide

Emptiness enumerates representative points rather than solving, and two
constructions read wrong. The first is deliberate: reasoning exactly about
substrings is intractable, so a `"lit" in var` test is decided as its own
free boolean, independent of the same variable's value. A contradiction
between the two is not seen, and the set reads larger than it is.

```pycon
>>> MarkerSet.from_marker('os_name == "posix" and "posix" not in os_name').is_empty()
False
```

The second is a defect, tracked but not yet fixed. Only points around a
set's own version literals enter the partition, so a band between two
adjacent literals holds no representative and the set reads smaller than
it is:

```pycon
>>> narrow = MarkerSet.from_marker(
...     'python_full_version > "3" and python_full_version < "3.1"'
... )
>>> narrow.is_empty()
True
>>> narrow.evaluate({"python_full_version": "3.0.1", "python_version": "3.0"})
True
```

`witness` and `evaluate` inherit neither: both check a concrete
environment against the set. So a `witness` is always right, and only its
`None` is weaker than "empty".

`variable_names` reads the variables a marker string names without building
a set, so it answers for markers `from_marker` refuses.

```pycon
>>> from nab_markersets.markersets import variable_names
>>> sorted(variable_names('python_full_version === "3.11"'))
['python_full_version']
```
