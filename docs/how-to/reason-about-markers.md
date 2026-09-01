# Reason about markers

`nab-markersets` reads a PEP 508 marker as the set of environments it
selects. Two markers that cannot both hold are disjoint sets; a marker no
environment satisfies is the empty set; a marker you can drop is the full
one. `packaging` evaluates a marker against one environment, and these are
the questions you cannot answer that way.

Install it with `pip install nab-markersets`. A set comes from
`MarkerSet.from_marker`, `MarkerSet.full` or `MarkerSet.empty`, and the
examples below spell `intersection`, `union`, `complement` and `difference`
as `&`, `|`, `~` and `-`.

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

`==` is structural, over the tree a set was built from, so two spellings
of one set compare unequal. `equivalent` is the semantic test.

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

The algebra is closed and the grammar is not, so `to_marker_string`
returns `None` for the full set and raises for the empty set and for a
complement PEP 508 cannot spell. `simplify` raises the same way, since it
serialises what it factors.

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
>>> not_gpu = MarkerSet.from_marker('extra != "gpu"')
>>> not_gpu.evaluate({"extra": frozenset({"cpu", "gpu"})})
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
>>> axes = "os_name sys_platform platform_machine platform_system".split()
>>> def clause(axis):
...     return " or ".join(f'{axis} == "v{i}"' for i in range(18))
>>> wide = MarkerSet.from_marker(" and ".join(f"({clause(a)})" for a in axes))
>>> try:
...     wide.is_empty()
... except IntractableMarkerSet:
...     print("too wide to decide")
too wide to decide
```

`restrict` the variables you already know before deciding, and the axes it
substitutes away stop costing anything.

## What the decisions do not decide

Emptiness enumerates representative points rather than solving, and one
construction reads wrong, deliberately: a `"lit" in var` test on a
version-dispatch variable (`python_version`, `python_full_version`,
`platform_release`, `implementation_version`) is decided as its own free
boolean, independent of the same variable's value, because the versions that
embed a literal cannot be enumerated from that literal. A contradiction
between the two readings is not seen, and the set reads larger than it is.

```pycon
>>> MarkerSet.from_marker(
...     'python_version == "3.9" and "9" not in python_version'
... ).is_empty()
False
```

A string variable has no such limit: its points carry both readings, so the
same contradiction is decided.

```pycon
>>> MarkerSet.from_marker('os_name == "posix" and "posix" not in os_name').is_empty()
True
```

So `is_empty` returning `True` is safe and `False` is the weak answer.

`witness` and `evaluate` do not inherit it: both check a concrete
environment against the set. So a `witness` is always right, and only its
`None` is weaker than "empty".

`variable_names` reads the variables a marker string names without building
a set, so it answers for markers `from_marker` refuses.

```pycon
>>> from nab_markersets.markersets import variable_names
>>> sorted(variable_names('python_full_version === "3.11"'))
['python_full_version']
```

## The supported API

These module paths will not move without a major version bump:

```text
nab_markersets.errors       IntractableMarkerSet, UnserializableMarkerSet
nab_markersets.markersets   DecisionStore, MarkerSet, variable_names
```

The package root binds no names, so importing `nab_markersets` pulls in no
submodules. Anything else is internal and may move in any release.
