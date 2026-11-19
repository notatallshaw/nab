# Benchmarks

`range_scenarios.py` resolves three graph shapes through `nab_resolver.Resolver` with every
constructor argument left at its default, so the range type under test is this package's `Range`.
nab-project's benchmark suites all pass packaging's `VersionRange`, which leaves `Range` unmeasured
everywhere else even though it is what a consumer that supplies no `range_type` gets.

```bash
python nab-resolver/benchmarks/range_scenarios.py
python nab-resolver/benchmarks/range_scenarios.py --scenario conflict-free-fanout
python nab-resolver/benchmarks/range_scenarios.py --repeats 9 --json out.json
```

`wrong-package-backtracking` builds 64 releases of a graph whose roots pin disagreeing versions of
one shared package. It backtracks, so it drives the set predicates and the differences and
intersections that fragment a range.

`conflict-free-fanout` builds one root over 120 leaf packages of 50 releases each. Nothing
conflicts, no range ever gains a second interval, and nearly all the work is membership tests during
prioritization.

`satisfied-ceiling-fanin` builds one root over 1000 packages that all cap the same hub, one of them
below the rest.

That package is decided first, so every later requirement already holds: all 1005 `relation` calls
answer SUBSET against a hub range the yanked releases leave in six intervals.

The other scenarios answer SUBSET 201 and 3 times; use this one when subset and disjoint need
different paths.

A change to `Range` can win on one scenario and lose on another, so compare all three.

Each prints its `Range` call counts, `hash_misses` for calls that found the hash memo empty,
`intervals_max` and `intervals_mean` over the intervals tested, a solution digest, and process CPU.

The number of times a dictionary calls `__hash__` can differ between Python versions. `hash_misses`
counts the calls that actually computed a range's hash.

Each scenario starts with a warm-up. The next solve records the selected versions, search counters
and range calls together. The timed solves run with the original range methods. The benchmark
rejects a run if any solve differs in its pins, rounds, decisions or conflicts. To compare CPU
times, run one benchmark process at a time.

The digest identifies the solution, not the workload. `wrong-package-backtracking` always resolves
to the same four pins, so its search counters and interval census separate runs.

The interval census reflects the scenario's shape, not a sample. On the two fanned scenarios
`intervals_max` is fixed by construction; on `wrong-package-backtracking` it is the size.

Every membership test comes from `GraphProvider` scanning its own release lists. A provider that
indexed or cached its candidates would show almost none. These scenarios compare `Range`
implementations on a fixed workload; they do not size `Range`'s share of a real resolve.

These scenarios use `int` versions. Consumers using packaging `Version` objects have different
comparison costs, so measure those workloads separately.
