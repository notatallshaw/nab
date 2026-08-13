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
all five distributions installed editable, plus the test, lint, docs,
and types environments available via `hatch run`.

[hatch]: https://hatch.pypa.io/

## Running the tests

The default suite is fast (under a minute) and covers every module
under `nab_resolver`, `nab_provider`, `nab_project`, `nab_index`, and
`nab`:

```bash
.venv/bin/python -m pytest                # default selection (no markers)
```

Branch coverage runs through the `coverage` CLI. Each process writes its
own data file, so combine before reporting:

```bash
.venv/bin/python -m coverage erase
.venv/bin/python -m coverage run -m pytest
.venv/bin/python -m coverage combine
.venv/bin/python -m coverage report       # fails below 100 percent
```

CI gates each workspace's coverage on its own tests through nox (see
`noxfile.py`). With nox installed (`pip install nox`), reproduce a single
workspace, or all of them, with:

```bash
nox -s tests                              # every workspace, each gated
nox -s "tests(workspace='project')"       # just one workspace
```

Property-based tests are opt-in via marker:

```bash
.venv/bin/python -m pytest -m property    # Hypothesis-only suites
```

Lint and format with ruff; type-check with pyright:

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format .
.venv/bin/python -m pyright
```

CI checks the same trees with five checkers rather than one, so a change
pyright accepts can still fail the matrix. Reproduce a single cell with
`nox -s "types(checker='mypy')"`; the trees are `TYPED_TREES` in
`noxfile.py`.

## The build backend

nab builds its distributions with `--no-isolation`, so the backend has to be
installed rather than fetched during the build. It is locked from
`[build-system].requires`: `tasks/refresh-locks.sh` writes
`.github/requirements/pylock.build.toml` with `nab lock --build-requirements`,
and every path that builds installs it: the `dists` nox session, the release
workflow and `hatch run release:build`.

One lock serves all five packages, which holds only while they declare the
same `[build-system]`. The refresh script checks that before it writes
anything, and checks afterwards that the locks sharing an environment agree on
the packages they share.

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
3.10 and newer, but locking the toolchain across that range would hold it
to whatever still supports the floor.

## Coverage policy

The `pyproject.toml` `[tool.coverage.report] fail_under = 100`
setting requires 100 percent branch coverage on every workspace
package: `nab_resolver`, `nab_provider`, `nab_project`, `nab_index`,
and `nab`. The full local suite under `coverage run -m pytest` checks
all five together; nox splits them per workspace in CI, with
`nab_index` and `nab_provider` gated in the `project` workspace,
whose tests are the only ones that reach every line of both. The
`provider` workspace runs `nab-provider/tests` without `nab-index`
installed and gates no package of its own. When code is unreachable
from the default suite, prefer:

* `# pragma: no cover` for a platform-specific or defensively
  unreachable line.
* `raise RuntimeError("Bug: unreachable")` style guards: `coverage`
  excludes those automatically via the `raise RuntimeError.*unreachable`
  pattern in `[tool.coverage.report].exclude_also`.

Code under `_build/env.py` and the CLI typically mocks subprocesses,
network calls, and venv creation rather than skipping the gate.
