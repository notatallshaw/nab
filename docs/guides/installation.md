# Installation

`nab` ships as a CLI plus three importable libraries. The
recommended path is to install it as an isolated tool.

## uv tool install

```bash
uv tool install nab
```

Drops `nab` into a uv-managed tool venv and exposes the console
script on `PATH`. uv resolves and installs the four workspace
distributions (`nab`, `nab-resolver`, `nab-python`, `nab-index`)
together. Confirm with:

```bash
nab --version
```

`nab` runs on CPython 3.10 and newer. Other interpreters are not
tested.

## pipx

```bash
pipx install nab
```

pipx creates a per-tool virtual environment. The default backend
is `uv` on recent pipx; the `pip` backend works equivalently.

## Picking an HTTP backend

`nab-index` ships urllib3 by default. `httpx` and `niquests` are
opt-in via extras:

```bash
uv tool install 'nab[httpx]'
uv tool install 'nab[niquests]'
```

Pick one at run-time with `--http-backend httpx` or
`--http-backend niquests`. Selecting a backend that was not
installed surfaces a helpful `ImportError`.

## Throw-away invocations

```bash
uvx nab --help
pipx run nab --help
```

Both fetch the wheel into an ephemeral environment and run the CLI
against it.

## Installing from a local checkout

The four wheels are not on PyPI yet. Build them locally and point
uv at the resulting directory:

```bash
git clone https://github.com/notatallshaw/nab.git
cd nab
mkdir -p /tmp/nab-wheels
uv build --wheel --out-dir /tmp/nab-wheels nab-resolver
uv build --wheel --out-dir /tmp/nab-wheels nab-python
uv build --wheel --out-dir /tmp/nab-wheels nab-index
uv build --wheel --out-dir /tmp/nab-wheels .
uv tool install --find-links /tmp/nab-wheels nab
```

## Development install

Working on nab itself, or pinning to a particular commit, calls for
the workspace check-out. nab uses [hatch] as its environment manager;
clone the repository and let hatch do the editable installs:

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

### Coverage policy

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
