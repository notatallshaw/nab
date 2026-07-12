# Contributing

Working on nab itself, rather than installing it as a tool, starts
from the workspace check-out.

## Development install

nab uses [hatch] as its environment manager; clone the repository and
let hatch do the editable installs:

```bash
git clone https://github.com/notatallshaw/nab.git
cd nab
hatch shell
nab --version
```

`hatch shell` enters a virtual environment (`.venv` by default) with
all four workspace members installed editable, plus the test, lint,
docs, and types groups available via `hatch run`.

[hatch]: https://hatch.pypa.io/

## Running the tests

The default suite is fast (under a minute) and covers every module
under `nab_resolver`, `nab_python`, and `nab`:

```bash
.venv/bin/python -m pytest                # default selection (no markers)
.venv/bin/python -m pytest --cov          # with branch coverage
```

Property-based tests are opt-in via marker:

```bash
.venv/bin/python -m pytest -m property    # Hypothesis-only suites
```

Lint and format with ruff; type-check with pyright:

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format .
.venv/bin/python -m pyright nab-resolver/src/
```

## Building the docs

```bash
hatch run docs:build     # sphinx-build -W, warnings are errors
hatch run docs:serve     # live-reloading preview
```

## Coverage policy

The `pyproject.toml` `[tool.coverage.report] fail_under = 100`
setting requires `pytest --cov` to report 100 percent branch
coverage on `nab_resolver`, `nab_python`, and `nab`. When code is
genuinely unreachable from the default suite, prefer:

* `# pragma: no cover` for a platform-specific or defensively
  unreachable line.
* `raise RuntimeError("Bug: unreachable")` style guards: `coverage`
  excludes those automatically via the `raise RuntimeError.*unreachable`
  pattern in `[tool.coverage.report].exclude_also`.

Code under `_build/env.py` and the CLI typically mocks subprocesses,
network calls, and venv creation rather than skipping the gate.
