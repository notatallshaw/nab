# Benchmarks

`range_scenarios.py` resolves two graph shapes through `nab_resolver.Resolver` with every
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

A change to `Range` can win on one of those and lose on the other, so compare both. Each
scenario prints its Range call counts, `hash_misses` for the calls that found the hash memo
empty, `intervals_max` and `intervals_mean` over the intervals per range that membership was
tested against, a digest of the solution it reached, and process CPU. Everything but the
`__hash__` count and the timing survives a change of machine. `__hash__` follows how often
CPython's dicts ask a key for its hash and so differs between versions, which is why
`hash_misses` is reported beside it. Quote a CPU number only from a serialized run, and read
two runs whose digests differ as two different questions rather than as a comparison.

Three things those numbers do not say. The digest identifies the solution, not the workload,
and `wrong-package-backtracking` always resolves to the same four pins, so on that scenario
the search counters and the interval census are what separate two runs. The interval census
is a function of that scenario's size rather than a sample of anything: `intervals_max` is
always the size. And every membership test in either scenario comes from `GraphProvider`
scanning its own release lists, so a provider that indexed or cached its candidates would
show almost none. All of it compares `Range` implementations against each other on a fixed
workload; none of it sizes `Range`'s share of a real resolve.

Versions here are `int`, so a comparison inside `Range` costs a C-level int compare, while a
consumer holding packaging `Version` objects pays a Python-level `__lt__`. A change that
spends allocations to save comparisons is worth at least what it measures here, and more to
that consumer.
