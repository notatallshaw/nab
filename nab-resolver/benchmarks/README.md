# Benchmarks

`range_scenarios.py` resolves three graph shapes through `nab_resolver.Resolver` with every
constructor argument left at its default, so the range type under test is this package's
`Range`. nab-project's benchmark suites all pass packaging's `VersionRange`, which leaves
`Range` unmeasured everywhere else even though it is what a consumer that supplies no
`range_type` gets.

```bash
python nab-resolver/benchmarks/range_scenarios.py
python nab-resolver/benchmarks/range_scenarios.py --scenario conflict-free-fanout
python nab-resolver/benchmarks/range_scenarios.py --repeats 9 --json out.json
```

`wrong-package-backtracking` builds 64 releases of a graph whose roots pin disagreeing
versions of one shared package. It backtracks, so it drives the set predicates and the
differences and intersections that fragment a range.

`conflict-free-fanout` builds one root over 120 leaf packages of 50 releases each. Nothing
conflicts, no range ever gains a second interval, and nearly all the work is membership
tests during prioritization.

`satisfied-ceiling-fanin` builds one root over 1000 packages that all
cap the same hub, one of them below the rest.

That package is decided first, so every later requirement already holds:
all 1005 `relation` calls answer SUBSET against a hub range the yanked
releases leave in six intervals.

The other scenarios answer SUBSET 201 and 3 times; use this one when
subset and disjoint need different paths.

A change to `Range` can win on one scenario and lose on another, so
compare all three.

Each prints its `Range` call counts, `hash_misses` for calls that found
the hash memo empty, `intervals_max` and `intervals_mean` over the
intervals tested, a solution digest, and process CPU.

Everything but the `__hash__` count and timing survives a change of
machine. `__hash__` follows how often CPython's dicts ask a key for its
hash and so differs between versions, which is why `hash_misses` is
reported beside it.

Quote a CPU number only from a serialized run. Two runs whose digests
differ answer different questions and cannot be compared.

The digest identifies the solution, not the workload.
`wrong-package-backtracking` always resolves to the same four pins, so
its search counters and interval census separate runs.

The interval census reflects the scenario's shape, not a sample. On the
two fanned scenarios `intervals_max` is fixed by construction; on
`wrong-package-backtracking` it is the size.

Every membership test comes from `GraphProvider` scanning its own
release lists. A provider that indexed or cached its candidates would
show almost none. These scenarios compare `Range` implementations on a
fixed workload; they do not size `Range`'s share of a real resolve.

Versions here are `int`, so a comparison inside `Range` costs a C-level int compare, while a
consumer holding packaging `Version` objects pays a Python-level `__lt__`. A change that
spends allocations to save comparisons is worth at least what it measures here, and more to
that consumer.
