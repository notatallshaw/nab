# Benchmarks

nab ships two live-index benchmark suites that exercise single-environment and universal resolves
against real-world scenarios. It also includes a small, deterministic offline smoke suite for
repeatable correctness and performance checks.

The Test workflow's benchmark job runs the offline smoke and resolver range suites on Ubuntu with
Python 3.15. Download the `offline-benchmarks` artifact for the results, search counters and
timings.

## Deterministic offline smoke suite

`deterministic_smoke.py` materializes a content-addressed local Simple index and runs seven semantic
cases plus four scaled performance cases. Every successful resolve is checked for exact target pins,
PEP 751 lock projection, fixture sources, wheel hashes, and dependency edges. Unsatisfiable cases
must return a proof-bearing resolution error without pins or a lock.

Each performance case pins its exact search counters. `pip-deep-backtracking` measures backtracking
whose conflicts identify the decision one level up. `deep-backjump` supplies the case where the
culprit sits several levels below the conflict, where decision order matters.

Together they detect a change that reaches the right answer by a different path.

```bash
python nab-project/benchmarks/deterministic_smoke.py --lane semantic
python nab-project/benchmarks/deterministic_smoke.py --lane performance --runs 5
```

The scenarios use Nab's default highest resolution strategy and default cross-target alignment.
`strategy-lowest`, `strategy-lowest-direct`, and `universal-independent` declare the only
exceptions.

Each resolve gets a fresh coordinator against the same prebuilt offline fixture. It prefetches only
the listings its requirements and constraints can reach, so a new scenario adds no listing work to
existing ones.

Warmups happen before measurement. Each recorded interval covers only the resolver call; fixture
generation, coordinator lifecycle, semantic validation, and lock emission stay outside it.

Metadata reads started when a listing lands can still run inside the interval, so changing
prefetches changes the recorded time. Timing samples are local diagnostics, not a reusable baseline
or comparative result, and should be collected sequentially under controlled conditions.

## Single-environment scenarios

`nab-project/benchmarks/scenarios.py` runs scenarios drawn from real-world resolver issues across
pip, uv, poetry, pex, and pip-tools. Scenario TOML files live under
`nab-project/benchmarks/scenarios/`.

```bash
python nab-project/benchmarks/scenarios.py
```

The runner records wall-clock, decision-count, and round-count metrics in one JSON result per
scenario under `nab-project/benchmarks/results/<commit>/`.

The standard corpus contains one definition per scenario. A normal run resolves each supported
definition once with the default `highest` strategy. A strategy-matrix run expands that corpus over
`highest`, `lowest`, and `lowest-direct`; strategies never have separate TOML copies.

Repeat `--toml` to select one or more canonical files:

```bash
python nab-project/benchmarks/scenarios.py --strategy-matrix
python nab-project/benchmarks/scenarios.py \
    --strategy-matrix --toml quick --toml pip
python nab-project/benchmarks/_profile_runner.py \
    pip:cburroughs-v3 --resolution lowest
```

`strategy_sweep.py` is a compatibility alias for `scenarios.py --strategy-matrix`; it does not
implement another resolver or result format. Retired selections such as `--toml pip-lowest` fail
with the canonical replacement command.

Each run initializes `_standard_manifest.json` as incomplete before resolving anything. It becomes
complete only when every applicable execution has an exact, valid result, every unsupported or
host-inapplicable scenario is accounted for, no result is missing or extra, and the source tree is
clean.

Successful results retain normalized package pins; failed results have none. The manifest records
source identity at both run boundaries, corpus hash, execution settings, physical host identity,
strategies, selected files, host-specific scenarios, and terminal key sets.

Reusing a result label requires the same mode, corpus, selection, and host. Use a new label or
`--force` when an input changes. `--force` replaces the label's standard JSON results while
preserving universal results and top-level provenance.

### Canary subset

`canary.py` runs the small hard-case subset used by local verification.

`canary.toml` selects canonical scenario definitions and declares each case's strategy. Manual
selections may use an explicit suffix such as `pip:trustllm@lowest`. The runner records the complete
TOML input, effective target and policy, source state, and content hashes. It checks the checkout
again before writing the summary and reports an error if the source changed.

Canary artifacts use contract version 2. An explicit strategy is serialized with the selector and
input definition used by that contract's input hash, keeping existing scoreboards comparable. The
local verifier compares successful results only when their contract version and host-aware execution
hashes match and both source trees are clean.

## Universal-resolution scenarios

`nab-project/benchmarks/universal_scenarios.py` runs universal-resolution scenarios sourced from
public uv/poetry/pex slowness reports. The cases stress cross-tuple alignment, marker divergence,
and broad target matrices.

```bash
python nab-project/benchmarks/universal_scenarios.py
python nab-project/benchmarks/universal_scenarios.py --scenario marker-heavy
python nab-project/benchmarks/universal_summary.py
```

`universal_summary.py` walks the latest results directory and prints a markdown table. To name a
run, pass a portable filename such as `--commit topic-refresh` to the runner, then pass
`topic-refresh` to the summary.

Each result retains its per-target solutions and merged target-label pin projection. The runner
exits nonzero after an unexpected resolution, timeout, lock-emission failure, or projection failure.

The report records the Python versions selected from the scenario's range before resolution. A
completed result must cover every requested Python version and platform. Rerun older scenarios to
write this target list, or read their reports with the summary script from the original checkout.

A full run writes `_manifest.json` with the current scenario set. The summary follows that set,
ignores removed result files, and accepts only complete runs that started and ended on the same
clean revision. If the checkout changes during a run, rerun it to get a complete report.

If a release's wheels and source distribution have the same dependencies on every target, add
`assume_universal_deps = { package = "==1.2" }` to the scenario. Nab can then read those
dependencies from a wheel. The report records the package and version selector.

For older source releases that lack static metadata, record the release's declared requirements with
`dependency_metadata."name==1.2" = ["dep>=3"]`. Use an empty list for a release with no runtime
dependencies. The benchmark records these declarations alongside its other inputs.

Selected diagnostics are isolated under `universal-selected/` and are never treated as a full-suite
baseline.

Benchmark outputs are local run artifacts rather than repository baselines. Generate both sides of a
comparison on the same machine, with the same initial cache state and toolchain.

The standard manifest makes source, corpus, selection, strategy, timeout, and iteration identity
machine-checkable. Retained CI or release artifacts still need Python and platform, index context,
cache provenance, and toolchain metadata to support a performance claim.

## Yanked candidates

Standard and canary runs retain the original requirement text to recognize exact pins. When yanked
candidates need consideration, they invoke `YankProxyProvider`, matching project resolution. A
singleton version range alone does not count as an exact pin.

The existing PubGrub counters include every fallback resolve, non-yanked alternative check and
known-dependency check. `yanked_proxy_rounds` counts the rounds spent on those checks and fallback
resolves. Those rounds are already included in the total. The counter is zero when fallback is
unused. The comparison tool shows the difference when both runs recorded it.

## Scenario shape

A runnable single-environment scenario declares its `python_version` and `requirements` in a named
TOML table. Set `datetime` to fix the upload cutoff. Optional settings include `constraints`,
`marker_environment`, `indexes` and `build_packages`.

To skip a scenario, set `unsupported_reason` to a string explaining why it cannot run. Omit the
field to run the scenario.

Use an explicit UTC cutoff when comparing a scenario with pip. For example, the Nab benchmark reads
`datetime = "2025-06-06 12:35:16"` as UTC. Run pip with `--uploaded-prior-to 2025-06-06T12:35:16Z`
to use the same instant. Without an offset, pip uses local time and can see a different set of
releases.

`marker_environment` and the `platform_system` shorthand define the resolver target without
restricting the host. For example, `platform_system = "Windows"` also sets `sys_platform` to `win32`
and `os_name` to `nt`. Set `requires_matching_host = true` when the physical host must match those
values so its wheel tags remain faithful.

`platform_system` alone admits any machine in that OS family; machine and implementation markers
narrow the match. A nonmatching host records the scenario as inapplicable.

Universal scenarios require `python`, `platforms`, and `requirements`. They may also set
constraints, a cutoff, Python ordering, alignment, resolution strategy, an explanatory reason, and
expected-failure handling.

With `skip_on_fail = true`, a failed resolution counts as expected only if the solver proved a
conflict on every failed target. A work limit or solver error fails the benchmark. Rerun older
expected-failure reports to record the proof status.

Use `expect_conflict = true` when the requirements must conflict on every target. Any target that
resolves or hits a work limit makes the benchmark fail.

## What the suites cover

- Tight version-cluster cases (e.g. boto3, awscli).
- Conflict graphs that have hit pip's default resolver budget (numpy/scipy/scikit-learn matrices).
- Universal-mode fork-explosion cases (xinference, vllm, ultralytics, copick).

The suites are opt-in diagnostic harnesses. Wall time is noisy, so prefer decision and round counts
when comparing resolver search.
