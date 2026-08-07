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

The runner records wall-clock, decision-count, and round-count
metrics, and writes a JSON summary under
`nab-python/benchmarks/results/<commit>/`.

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

Benchmark outputs are local run artifacts rather than repository baselines.
Generate both sides of a comparison on the same machine, with the same initial
cache state and toolchain. The current outputs are not self-describing, so any
retained CI or release artifact must be accompanied by the command, source and
scenario revisions, tool versions, Python and platform, index context, cache
provenance, timeouts, and iteration limits needed to interpret it.

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
