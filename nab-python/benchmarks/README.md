# Benchmarks

nab ships two benchmark suites that exercise single-environment and
universal resolves against real-world scenarios.

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
