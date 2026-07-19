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
under `nab_resolver`, `nab_python`, `nab_index`, and `nab`:

```bash
.venv/bin/python -m pytest                # default selection (no markers)
.venv/bin/python -m pytest --cov          # with branch coverage
```

CI gates each workspace's coverage on its own tests through nox (see
`noxfile.py`). With nox installed (`pip install nox`), reproduce a single
workspace, or all of them, with:

```bash
nox -s tests                              # every workspace, each gated
nox -s "tests(workspace='python')"        # just one workspace
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

The `docs` dependency-group in `pyproject.toml` is the one place the doc
tooling is declared. nab locks it into
`.github/requirements/pylock.docs.toml` (`tasks/refresh-locks.sh`), and both
CI and Read the Docs install from that lock, so a published build resolves
nothing. After changing the group, re-run the refresh script and commit the
lock.

Unlike the other groups, that lock is a single resolution for Python 3.13,
the one version Read the Docs and the CI docs job build with. nab runs on
3.10 and newer, but the docs build is only supported on 3.13: locking the
toolchain across the whole range would hold it to whatever still supports
the floor.

## Coverage policy

The `pyproject.toml` `[tool.coverage.report] fail_under = 100`
setting requires 100 percent branch coverage on every workspace
package: `nab_resolver`, `nab_python`, `nab_index`, and `nab`. The
full local suite (`pytest --cov`) checks all four together; nox splits
them per workspace in CI so each workspace's tests cover only its own
package, with `nab_index` gated alongside `nab_python`, whose tests
exercise it. When code is genuinely unreachable from the default
suite, prefer:

* `# pragma: no cover` for a platform-specific or defensively
  unreachable line.
* `raise RuntimeError("Bug: unreachable")` style guards: `coverage`
  excludes those automatically via the `raise RuntimeError.*unreachable`
  pattern in `[tool.coverage.report].exclude_also`.

Code under `_build/env.py` and the CLI typically mocks subprocesses,
network calls, and venv creation rather than skipping the gate.
