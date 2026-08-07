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
divergence, and pre-release handling.

```bash
python nab-python/benchmarks/universal_scenarios.py
python nab-python/benchmarks/universal_summary.py
```

`universal_summary.py` walks the latest results directory and
prints a markdown table.

Benchmark outputs are local run artifacts rather than repository baselines.
Generate both sides of a comparison on the same machine, with the same initial
cache state and toolchain. The current outputs are not self-describing, so any
retained CI or release artifact must be accompanied by the command, source and
scenario revisions, tool versions, Python and platform, index context, cache
provenance, timeouts, and iteration limits needed to interpret it.

## Scenario shape

Each scenario is a top-level TOML table keyed by name, with at least
`requirements` and a fixed `datetime` (used as the
`uploaded-prior-to` cutoff).  Optional knobs include constraints,
marker overlay, dist policy, and build policy.

## What the suites cover

* Tight version-cluster cases (e.g. boto3, awscli).
* Conflict graphs that have hit pip's default resolver budget
  (numpy/scipy/scikit-learn matrices).
* Universal-mode fork-explosion cases (xinference, vllm,
  ultralytics, copick).

The suites are diagnostic harnesses that flag regressions on pull
requests.  Walltime is noisy; decision count and round count are
the load-bearing numbers.
