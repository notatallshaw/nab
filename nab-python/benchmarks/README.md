# Benchmarks

nab ships two live-index benchmark suites that exercise single-environment and universal resolves against real-world scenarios. It also includes a small, deterministic offline smoke suite for repeatable correctness and performance checks.

## Deterministic offline smoke suite

`deterministic_smoke.py` materializes a content-addressed local Simple index and runs seven semantic cases plus four scaled performance cases. Every successful resolve is checked for exact target pins, PEP 751 lock projection, fixture sources, wheel hashes, and dependency edges. Unsatisfiable cases must return a proof-bearing resolution error without pins or a lock.

Each performance case also pins its exact search counters. `pip-deep-backtracking` measures the volume of backtracking; its conflicts each name the decision one level up, so `deep-backjump` supplies the case where the culprit sits several levels below the conflict and the decision order matters. Between them a change that reaches the right answer along a different path moves a recorded number.

```bash
python nab-python/benchmarks/deterministic_smoke.py --lane semantic
python nab-python/benchmarks/deterministic_smoke.py --lane performance --runs 5
```

The scenarios use Nab's default highest resolution strategy and default cross-target alignment. `strategy-lowest`, `strategy-lowest-direct`, and `universal-independent` declare the only exceptions.

Each resolve gets a fresh coordinator against the same prebuilt offline fixture. Warmups happen before measurement, and each recorded inner interval covers only the resolver call; fixture generation, coordinator lifecycle, semantic validation, and lock emission stay outside it. Timing samples are local diagnostics, not a reusable baseline or comparative result, and should be collected sequentially under controlled conditions.

## Single-environment scenarios

`nab-python/benchmarks/scenarios.py` runs scenarios drawn from
real-world resolver issues across pip, uv, poetry, pex, and
pip-tools.  Scenario TOML files live under
`nab-python/benchmarks/scenarios/`.

```bash
python nab-python/benchmarks/scenarios.py
```

The runner records wall-clock, decision-count, and round-count metrics in one JSON result per scenario under `nab-python/benchmarks/results/<commit>/`.

The standard corpus contains one definition per scenario. A normal run resolves each supported definition once with the default `highest` strategy. An explicit strategy-matrix run expands that same corpus over `highest`, `lowest`, and `lowest-direct`; strategies never have separate TOML copies. Repeat `--toml` to select one or more canonical files:

```bash
python nab-python/benchmarks/scenarios.py --strategy-matrix
python nab-python/benchmarks/scenarios.py \
    --strategy-matrix --toml quick --toml pip
python nab-python/benchmarks/_profile_runner.py \
    pip:cburroughs-v3 --resolution lowest
```

`strategy_sweep.py` is a compatibility alias for `scenarios.py --strategy-matrix`; it does not implement another resolver or result format. Retired selections such as `--toml pip-lowest` fail with the canonical replacement command.

Each run initializes `_standard_manifest.json` as incomplete before resolving anything. It becomes complete only after every selected supported execution has an exact, valid result; every selected unsupported scenario is declared; no result is missing or extra; and the source tree is clean. The manifest records the full source identity at both run boundaries, corpus hash, execution settings, strategies, selected files, and exact available, selected, completed, and unsupported logical and execution key sets. A missing, malformed, dirty, source-changing, or incomplete manifest does not describe a valid run. Reusing a result label resumes only when that manifest describes the same mode, strategy set, corpus, and selection. Use a new label or `--force` when any of those inputs changes. `--force` replaces the standard JSON results for the label while preserving universal results and top-level provenance.

### Static Nab/uv parity plan

`standard_parity_plan.py` compiles every canonical standard scenario and its three strategy mappings into deterministic JSON. The source corpus has 558 logical scenarios and 1,674 mapping identities: 536 scenarios are runnable across 1,608 strategy executions, while 22 are declared unsupported. Every identity remains in the plan. The compiler preserves a normalized Nab contract, omits the default `highest` resolution override, and emits a uv translation only when the input is statically representable. Incomplete target overlays and other semantic gaps remain in the plan with a typed `unsupported` reason and a null uv translation.

Schema 1 admits no `exact` or `conditional` rows. The standard source format does not carry structured expected outcomes or frozen prerelease, Requires-Python, build-policy, and artifact evidence, so all current rows fail closed as `unsupported`. A later schema must add and validate that evidence before either status becomes available. The normalized Nab contract uses the product default `trust_unverified_sdist_deps = false` when a scenario omits that setting; it does not preserve the old benchmark runner's permissive fallback.

```bash
python nab-python/benchmarks/standard_parity_plan.py > standard-parity-plan.json
```

The compiler does not invoke either resolver or access the network. Each plan embeds its validated source definitions, binds their digest into the corpus digest, and requires that source digest from a separately trusted caller when validated. The plan is not a result, baseline, or claim of Nab/uv parity. A paired executor and its evidence contract require a separate review boundary.

A paired executor must run one ordinary Nab command and one ordinary uv command for each mapping. It must measure each complete command and neither reject nor rescale a comparison because the tools perform different numbers of internal resolves.

### Canary subset

`canary.py` runs the small hard-case subset used by the local verification gate.
`canary.toml` selects canonical scenario definitions and declares the strategy for each case. Manual selections may use an explicit suffix such as `pip:trustllm@lowest`. The runner records the complete TOML input, effective target and policy, source state, and content hashes. Canary artifacts retain contract version 2: non-default cases reconstruct the equivalent historical selector and input definition at the serialization boundary, so existing scoreboards remain comparable even though strategy-clone files are gone. The local verifier compares success results only when their contract version and host-aware execution hashes match and both source trees are clean.

## Universal-resolution scenarios

`nab-python/benchmarks/universal_scenarios.py` runs
universal-resolution scenarios sourced from public uv/poetry/pex
slowness reports.  The cases stress cross-tuple alignment, marker
divergence, and broad target matrices.

```bash
python nab-python/benchmarks/universal_scenarios.py
python nab-python/benchmarks/universal_scenarios.py --scenario marker-heavy
python nab-python/benchmarks/universal_summary.py
```

`universal_summary.py` walks the latest results directory and prints a markdown table.

Each result retains its per-target solutions and merged target-label pin projection. The runner exits nonzero after an unexpected resolution, timeout, lock-emission failure, or projection failure.

The result schema and source identity invalidate stale caches.

A full run writes `_manifest.json` with the current scenario set; the summary follows that set, ignores removed result files, and accepts only complete runs from a clean source tree. Selected diagnostics are isolated under `universal-selected/` and are never treated as a full-suite baseline.

Benchmark outputs are local run artifacts rather than repository baselines. Generate both sides of a comparison on the same machine, with the same initial cache state and toolchain. The standard manifest makes source, corpus, selection, strategy, timeout, and iteration identity machine-checkable; retained CI or release artifacts still need Python and platform, index context, cache provenance, and toolchain metadata to support a performance claim.

## Scenario shape

Each single-environment scenario is a top-level TOML table keyed by name, with at least `requirements` and a fixed `datetime` (used as the `uploaded-prior-to` cutoff). Optional single-environment knobs include constraints, marker overlays, distribution policy, and build policy.

Universal scenarios require `python`, `platforms`, and `requirements`. They may also set constraints, a cutoff, Python ordering, alignment, resolution strategy, an explanatory reason, and expected-failure handling.

## What the suites cover

* Tight version-cluster cases (e.g. boto3, awscli).
* Conflict graphs that have hit pip's default resolver budget
  (numpy/scipy/scikit-learn matrices).
* Universal-mode fork-explosion cases (xinference, vllm,
  ultralytics, copick).

The suites are opt-in diagnostic harnesses. Wall time is noisy, so prefer decision and round counts when comparing resolver search.
