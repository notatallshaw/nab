"""Corpus differential harness for MarkerSet.simplify.

``FIXTURES`` holds 34 per-package markers, each with its lock's declared
``environments`` rows, from eight PEP 751 locks nab wrote while surveying marker
sizes across real projects.

The brute-force oracle below is a second, independent minimiser, used to
cross-check soundness and minimality. It finds the smallest variable subset that
reproduces a marker's environment selection over a finite universe, then
factors. Dropping whole variables is weaker than dropping don't-care atoms per
clause, so it bounds the minimal size from above rather than reaching it.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator

from packaging.markers import Marker

# The tiers the oracle reaches on FIXTURES, in marker-string characters.
TOTAL_CHARS = 9742
CONTEXT_AWARE_CHARS = 1188
CONTEXT_AWARE_PCT = 87.8
CONTEXT_FREE_CHARS = 7332
CONTEXT_FREE_PCT = 24.7

# What the algebra operator reaches on the same corpus, dropping don't-care
# atoms per clause where the oracle can only drop whole variables.
OPERATOR_CONTEXT_AWARE_CHARS = 1010

MARKER_VARS_ORDER = [
    "python_version",
    "sys_platform",
    "platform_machine",
    "platform_python_implementation",
    "implementation_name",
    "platform_system",
    "os_name",
]

STRING_SENTINEL = "__other__"
VERSION_SENTINEL = "3.99"


def env_dict_from_marker(marker_str: str) -> dict[str, str]:
    m = Marker(marker_str)
    env: dict[str, str] = {}

    def walk(markers: object) -> None:
        for node in markers:  # type: ignore[attr-defined]
            if isinstance(node, list):
                walk(node)
            elif isinstance(node, tuple):
                var, op, val = node
                if str(op) == "==":
                    env[str(var)] = str(val)

    walk(m._markers)
    pv = env.get("python_version")
    if pv and "python_full_version" not in env:
        env["python_full_version"] = pv + ".0"
    sysp = env.get("sys_platform")
    if sysp:
        env.setdefault("os_name", {"win32": "nt"}.get(sysp, "posix"))
        env.setdefault(
            "platform_system",
            {"linux": "Linux", "darwin": "Darwin", "win32": "Windows"}.get(sysp, sysp),
        )
    env.setdefault("platform_python_implementation", "CPython")
    env.setdefault("implementation_name", "cpython")
    env.setdefault("extra", "")
    return env


def selects(marker_str: str | None, universe: list[dict[str, str]]) -> frozenset[int]:
    if marker_str is None:
        return frozenset(range(len(universe)))
    m = Marker(marker_str)
    hits: list[int] = []
    for i, env in enumerate(universe):
        try:
            if m.evaluate(env):
                hits.append(i)
        except Exception:  # noqa: BLE001, S110
            pass
    return frozenset(hits)


def _clause_to_str(clause: frozenset[str]) -> str:
    return " and ".join(sorted(clause))


def _render_factored(clauses: list[frozenset[str]]) -> str:
    clauses = [c for c in clauses if c]
    uniq: list[frozenset[str]] = []
    for c in clauses:
        if c not in uniq:
            uniq.append(c)
    if not uniq:
        return ""
    if len(uniq) == 1:
        return _clause_to_str(uniq[0])
    common = frozenset.intersection(*uniq)
    residual = [c - common for c in uniq]
    residual = [c for c in residual if c]
    inner = " or ".join(
        _clause_to_str(c) if len(c) == 1 else f"({_clause_to_str(c)})"
        for c in sorted(residual, key=sorted)
    )
    if not common:
        return inner
    lead = " and ".join(sorted(common))
    if not residual:
        return lead
    return f"{lead} and ({inner})"


def achievable_min_marker(
    selected: frozenset[int],
    universe: list[dict[str, str]],
    marker_vars: list[str],
) -> str:
    if not selected:
        return ""
    if len(selected) == len(universe):
        return ""

    def proj(idx_set: set[int], keys: tuple[str, ...]) -> set[tuple]:
        return {tuple(universe[i].get(k) for k in keys) for i in idx_set}

    sel = set(selected)
    universe_idx = set(range(len(universe)))

    best_keys = marker_vars
    for r in range(1, len(marker_vars) + 1):
        found = None
        for keys in itertools.combinations(marker_vars, r):
            sel_proj = proj(sel, keys)
            recon = {
                i
                for i in universe_idx
                if tuple(universe[i].get(k) for k in keys) in sel_proj
            }
            if recon == sel:
                found = list(keys)
                break
        if found is not None:
            best_keys = found
            break

    tuples = sorted({tuple(universe[i].get(k) for k in best_keys) for i in sel})
    clauses = []
    for t in tuples:
        atoms = frozenset(
            f'{k} == "{v}"' for k, v in zip(best_keys, t, strict=True) if v is not None
        )
        clauses.append(atoms)
    return _render_factored(clauses)


def build_grid_universe(
    marker_vars: list[str], observed: dict[str, set[str]]
) -> list[dict[str, str]]:
    axes = []
    for v in marker_vars:
        vals = sorted(observed.get(v, set()))
        sentinel = VERSION_SENTINEL if "version" in v else STRING_SENTINEL
        if sentinel not in vals:
            vals = [*vals, sentinel]
        axes.append([(v, val) for val in vals])
    grid = []
    for combo in itertools.product(*axes):
        env = dict(combo)
        pv = env.get("python_version")
        if pv and "python_full_version" not in env:
            env["python_full_version"] = pv + ".0"
        env.setdefault("extra", "")
        grid.append(env)
    return grid


def marker_vars_of(marker_strings: list[str]) -> list[str]:
    used: set[str] = set()
    for mk in marker_strings:
        for node in _iter_atoms(Marker(mk)._markers):
            used.add(str(node[0]))
    return [v for v in MARKER_VARS_ORDER if v in used] or list(used)


def observed_values(
    marker_strings: list[str],
    universe: list[dict[str, str]],
    marker_vars: list[str],
) -> dict[str, set[str]]:
    observed: dict[str, set[str]] = {v: set() for v in marker_vars}
    for env in universe:
        for v in marker_vars:
            if v in env:
                observed[v].add(env[v])
    for mk in marker_strings:
        for node in _iter_atoms(Marker(mk)._markers):
            var, op, val = node
            if str(op) == "==" and str(var) in observed:
                observed[str(var)].add(str(val))
    return observed


def _iter_atoms(markers: object) -> Iterator[tuple]:
    for node in markers:  # type: ignore[attr-defined]
        if isinstance(node, list):
            yield from _iter_atoms(node)
        elif isinstance(node, tuple):
            yield node


# Fifteen minors make the whole-matrix complement of a single-platform full span
# overrun the cell budget while the row-restricted oracle stays cheap.
WIDE_PYS = [f"3.{i}" for i in range(15)]
NARROW_PYS = [f"3.{i}" for i in range(9, 15)]
WIDE_PLATS = [
    ("linux", "x86_64"),
    ("linux", "aarch64"),
    ("darwin", "arm64"),
    ("darwin", "x86_64"),
    ("win32", "AMD64"),
]


def _rows(pys: list[str]) -> list[str]:
    return [
        f'python_version == "{py}" and sys_platform == "{sp}" '
        f'and platform_machine == "{mach}"'
        for py in pys
        for sp, mach in WIDE_PLATS
    ]


def wide_universe() -> list[str]:
    """The declared rows: every python minor crossed with every platform."""
    return _rows(WIDE_PYS)


def narrow_universe() -> list[str]:
    """A smaller declared universe for the structural (non-overrun) checks."""
    return _rows(NARROW_PYS)


def _full_span(sp: str, mach: str, pys: list[str]) -> str:
    return " or ".join(
        f'(python_version == "{py}" and sys_platform == "{sp}" '
        f'and platform_machine == "{mach}")'
        for py in pys
    )


def wide_curated() -> list[dict[str, object]]:
    """Single-platform full-span markers whose whole-matrix oracle overruns.

    Each pairs a marker with the declared multi-platform universe; the
    row-restricted oracle collapses each to its platform pin.
    """
    universe = wide_universe()
    return [
        {"marker": _full_span("linux", "x86_64", WIDE_PYS), "environments": universe},
        {"marker": _full_span("darwin", "arm64", WIDE_PYS), "environments": universe},
    ]


FIXTURES = [
    {
        "lock": "marker-heavy",
        "package": "pyobjc-core",
        "marker": 'python_version == "3.11" and sys_platform == "darwin" and platform_machine == "arm64"',
        "environments": [
            'python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython"',
            'python_version == "3.11" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython"',
            'python_version == "3.11" and sys_platform == "win32" and platform_machine == "AMD64" and platform_python_implementation == "CPython"',
        ],
    },
    {
        "lock": "marker-heavy",
        "package": "python-magic",
        "marker": '(python_version == "3.11" and sys_platform == "darwin" and platform_machine == "arm64") or (python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64")',
        "environments": [
            'python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython"',
            'python_version == "3.11" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython"',
            'python_version == "3.11" and sys_platform == "win32" and platform_machine == "AMD64" and platform_python_implementation == "CPython"',
        ],
    },
    {
        "lock": "marker-heavy",
        "package": "python-magic-bin",
        "marker": 'python_version == "3.11" and sys_platform == "win32" and platform_machine == "AMD64"',
        "environments": [
            'python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython"',
            'python_version == "3.11" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython"',
            'python_version == "3.11" and sys_platform == "win32" and platform_machine == "AMD64" and platform_python_implementation == "CPython"',
        ],
    },
    {
        "lock": "marker-heavy",
        "package": "pywin32",
        "marker": 'python_version == "3.11" and sys_platform == "win32" and platform_machine == "AMD64"',
        "environments": [
            'python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython"',
            'python_version == "3.11" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython"',
            'python_version == "3.11" and sys_platform == "win32" and platform_machine == "AMD64" and platform_python_implementation == "CPython"',
        ],
    },
    {
        "lock": "max-25-tuples",
        "package": "colorama",
        "marker": '(python_version == "3.10" and sys_platform == "win32" and platform_machine == "AMD64") or (python_version == "3.11" and sys_platform == "win32" and platform_machine == "AMD64") or (python_version == "3.12" and sys_platform == "win32" and platform_machine == "AMD64") or (python_version == "3.13" and sys_platform == "win32" and platform_machine == "AMD64") or (python_version == "3.9" and sys_platform == "win32" and platform_machine == "AMD64")',
        "environments": [
            'python_version == "3.9" and sys_platform == "linux" and platform_machine == "x86_64" and platform_system == "Linux"',
            'python_version == "3.9" and sys_platform == "linux" and platform_machine == "aarch64" and platform_system == "Linux"',
            'python_version == "3.9" and sys_platform == "darwin" and platform_machine == "arm64" and platform_system == "Darwin"',
            'python_version == "3.9" and sys_platform == "darwin" and platform_machine == "x86_64" and platform_system == "Darwin"',
            'python_version == "3.9" and sys_platform == "win32" and platform_machine == "AMD64" and platform_system == "Windows"',
            'python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64" and platform_system == "Linux"',
            'python_version == "3.10" and sys_platform == "linux" and platform_machine == "aarch64" and platform_system == "Linux"',
            'python_version == "3.10" and sys_platform == "darwin" and platform_machine == "arm64" and platform_system == "Darwin"',
            'python_version == "3.10" and sys_platform == "darwin" and platform_machine == "x86_64" and platform_system == "Darwin"',
            'python_version == "3.10" and sys_platform == "win32" and platform_machine == "AMD64" and platform_system == "Windows"',
            'python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64" and platform_system == "Linux"',
            'python_version == "3.11" and sys_platform == "linux" and platform_machine == "aarch64" and platform_system == "Linux"',
            'python_version == "3.11" and sys_platform == "darwin" and platform_machine == "arm64" and platform_system == "Darwin"',
            'python_version == "3.11" and sys_platform == "darwin" and platform_machine == "x86_64" and platform_system == "Darwin"',
            'python_version == "3.11" and sys_platform == "win32" and platform_machine == "AMD64" and platform_system == "Windows"',
            'python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64" and platform_system == "Linux"',
            'python_version == "3.12" and sys_platform == "linux" and platform_machine == "aarch64" and platform_system == "Linux"',
            'python_version == "3.12" and sys_platform == "darwin" and platform_machine == "arm64" and platform_system == "Darwin"',
            'python_version == "3.12" and sys_platform == "darwin" and platform_machine == "x86_64" and platform_system == "Darwin"',
            'python_version == "3.12" and sys_platform == "win32" and platform_machine == "AMD64" and platform_system == "Windows"',
            'python_version == "3.13" and sys_platform == "linux" and platform_machine == "x86_64" and platform_system == "Linux"',
            'python_version == "3.13" and sys_platform == "linux" and platform_machine == "aarch64" and platform_system == "Linux"',
            'python_version == "3.13" and sys_platform == "darwin" and platform_machine == "arm64" and platform_system == "Darwin"',
            'python_version == "3.13" and sys_platform == "darwin" and platform_machine == "x86_64" and platform_system == "Darwin"',
            'python_version == "3.13" and sys_platform == "win32" and platform_machine == "AMD64" and platform_system == "Windows"',
        ],
    },
    {
        "lock": "max-25-tuples",
        "package": "importlib-metadata",
        "marker": '(python_version == "3.10" and sys_platform == "darwin" and platform_machine == "arm64") or (python_version == "3.10" and sys_platform == "darwin" and platform_machine == "x86_64") or (python_version == "3.10" and sys_platform == "linux" and platform_machine == "aarch64") or (python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64") or (python_version == "3.10" and sys_platform == "win32" and platform_machine == "AMD64") or (python_version == "3.11" and sys_platform == "darwin" and platform_machine == "arm64") or (python_version == "3.11" and sys_platform == "darwin" and platform_machine == "x86_64") or (python_version == "3.11" and sys_platform == "linux" and platform_machine == "aarch64") or (python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64") or (python_version == "3.11" and sys_platform == "win32" and platform_machine == "AMD64") or (python_version == "3.9" and sys_platform == "darwin" and platform_machine == "arm64") or (python_version == "3.9" and sys_platform == "darwin" and platform_machine == "x86_64") or (python_version == "3.9" and sys_platform == "linux" and platform_machine == "aarch64") or (python_version == "3.9" and sys_platform == "linux" and platform_machine == "x86_64") or (python_version == "3.9" and sys_platform == "win32" and platform_machine == "AMD64")',
        "environments": [
            'python_version == "3.9" and sys_platform == "linux" and platform_machine == "x86_64" and platform_system == "Linux"',
            'python_version == "3.9" and sys_platform == "linux" and platform_machine == "aarch64" and platform_system == "Linux"',
            'python_version == "3.9" and sys_platform == "darwin" and platform_machine == "arm64" and platform_system == "Darwin"',
            'python_version == "3.9" and sys_platform == "darwin" and platform_machine == "x86_64" and platform_system == "Darwin"',
            'python_version == "3.9" and sys_platform == "win32" and platform_machine == "AMD64" and platform_system == "Windows"',
            'python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64" and platform_system == "Linux"',
            'python_version == "3.10" and sys_platform == "linux" and platform_machine == "aarch64" and platform_system == "Linux"',
            'python_version == "3.10" and sys_platform == "darwin" and platform_machine == "arm64" and platform_system == "Darwin"',
            'python_version == "3.10" and sys_platform == "darwin" and platform_machine == "x86_64" and platform_system == "Darwin"',
            'python_version == "3.10" and sys_platform == "win32" and platform_machine == "AMD64" and platform_system == "Windows"',
            'python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64" and platform_system == "Linux"',
            'python_version == "3.11" and sys_platform == "linux" and platform_machine == "aarch64" and platform_system == "Linux"',
            'python_version == "3.11" and sys_platform == "darwin" and platform_machine == "arm64" and platform_system == "Darwin"',
            'python_version == "3.11" and sys_platform == "darwin" and platform_machine == "x86_64" and platform_system == "Darwin"',
            'python_version == "3.11" and sys_platform == "win32" and platform_machine == "AMD64" and platform_system == "Windows"',
            'python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64" and platform_system == "Linux"',
            'python_version == "3.12" and sys_platform == "linux" and platform_machine == "aarch64" and platform_system == "Linux"',
            'python_version == "3.12" and sys_platform == "darwin" and platform_machine == "arm64" and platform_system == "Darwin"',
            'python_version == "3.12" and sys_platform == "darwin" and platform_machine == "x86_64" and platform_system == "Darwin"',
            'python_version == "3.12" and sys_platform == "win32" and platform_machine == "AMD64" and platform_system == "Windows"',
            'python_version == "3.13" and sys_platform == "linux" and platform_machine == "x86_64" and platform_system == "Linux"',
            'python_version == "3.13" and sys_platform == "linux" and platform_machine == "aarch64" and platform_system == "Linux"',
            'python_version == "3.13" and sys_platform == "darwin" and platform_machine == "arm64" and platform_system == "Darwin"',
            'python_version == "3.13" and sys_platform == "darwin" and platform_machine == "x86_64" and platform_system == "Darwin"',
            'python_version == "3.13" and sys_platform == "win32" and platform_machine == "AMD64" and platform_system == "Windows"',
        ],
    },
    {
        "lock": "max-25-tuples",
        "package": "zipp",
        "marker": '(python_version == "3.10" and sys_platform == "darwin" and platform_machine == "arm64") or (python_version == "3.10" and sys_platform == "darwin" and platform_machine == "x86_64") or (python_version == "3.10" and sys_platform == "linux" and platform_machine == "aarch64") or (python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64") or (python_version == "3.10" and sys_platform == "win32" and platform_machine == "AMD64") or (python_version == "3.11" and sys_platform == "darwin" and platform_machine == "arm64") or (python_version == "3.11" and sys_platform == "darwin" and platform_machine == "x86_64") or (python_version == "3.11" and sys_platform == "linux" and platform_machine == "aarch64") or (python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64") or (python_version == "3.11" and sys_platform == "win32" and platform_machine == "AMD64") or (python_version == "3.9" and sys_platform == "darwin" and platform_machine == "arm64") or (python_version == "3.9" and sys_platform == "darwin" and platform_machine == "x86_64") or (python_version == "3.9" and sys_platform == "linux" and platform_machine == "aarch64") or (python_version == "3.9" and sys_platform == "linux" and platform_machine == "x86_64") or (python_version == "3.9" and sys_platform == "win32" and platform_machine == "AMD64")',
        "environments": [
            'python_version == "3.9" and sys_platform == "linux" and platform_machine == "x86_64" and platform_system == "Linux"',
            'python_version == "3.9" and sys_platform == "linux" and platform_machine == "aarch64" and platform_system == "Linux"',
            'python_version == "3.9" and sys_platform == "darwin" and platform_machine == "arm64" and platform_system == "Darwin"',
            'python_version == "3.9" and sys_platform == "darwin" and platform_machine == "x86_64" and platform_system == "Darwin"',
            'python_version == "3.9" and sys_platform == "win32" and platform_machine == "AMD64" and platform_system == "Windows"',
            'python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64" and platform_system == "Linux"',
            'python_version == "3.10" and sys_platform == "linux" and platform_machine == "aarch64" and platform_system == "Linux"',
            'python_version == "3.10" and sys_platform == "darwin" and platform_machine == "arm64" and platform_system == "Darwin"',
            'python_version == "3.10" and sys_platform == "darwin" and platform_machine == "x86_64" and platform_system == "Darwin"',
            'python_version == "3.10" and sys_platform == "win32" and platform_machine == "AMD64" and platform_system == "Windows"',
            'python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64" and platform_system == "Linux"',
            'python_version == "3.11" and sys_platform == "linux" and platform_machine == "aarch64" and platform_system == "Linux"',
            'python_version == "3.11" and sys_platform == "darwin" and platform_machine == "arm64" and platform_system == "Darwin"',
            'python_version == "3.11" and sys_platform == "darwin" and platform_machine == "x86_64" and platform_system == "Darwin"',
            'python_version == "3.11" and sys_platform == "win32" and platform_machine == "AMD64" and platform_system == "Windows"',
            'python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64" and platform_system == "Linux"',
            'python_version == "3.12" and sys_platform == "linux" and platform_machine == "aarch64" and platform_system == "Linux"',
            'python_version == "3.12" and sys_platform == "darwin" and platform_machine == "arm64" and platform_system == "Darwin"',
            'python_version == "3.12" and sys_platform == "darwin" and platform_machine == "x86_64" and platform_system == "Darwin"',
            'python_version == "3.12" and sys_platform == "win32" and platform_machine == "AMD64" and platform_system == "Windows"',
            'python_version == "3.13" and sys_platform == "linux" and platform_machine == "x86_64" and platform_system == "Linux"',
            'python_version == "3.13" and sys_platform == "linux" and platform_machine == "aarch64" and platform_system == "Linux"',
            'python_version == "3.13" and sys_platform == "darwin" and platform_machine == "arm64" and platform_system == "Darwin"',
            'python_version == "3.13" and sys_platform == "darwin" and platform_machine == "x86_64" and platform_system == "Darwin"',
            'python_version == "3.13" and sys_platform == "win32" and platform_machine == "AMD64" and platform_system == "Windows"',
        ],
    },
    {
        "lock": "poetry-numpy-deveps",
        "package": "exceptiongroup",
        "marker": 'python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64"',
        "environments": [
            'python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64" and implementation_name == "cpython" and platform_system == "Linux"',
            'python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64" and implementation_name == "cpython" and platform_system == "Linux"',
            'python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64" and implementation_name == "cpython" and platform_system == "Linux"',
        ],
    },
    {
        "lock": "poetry-numpy-deveps",
        "package": "tomli",
        "marker": 'python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64"',
        "environments": [
            'python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64" and implementation_name == "cpython" and platform_system == "Linux"',
            'python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64" and implementation_name == "cpython" and platform_system == "Linux"',
            'python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64" and implementation_name == "cpython" and platform_system == "Linux"',
        ],
    },
    {
        "lock": "poetry-numpy-deveps",
        "package": "typing-extensions",
        "marker": 'python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64"',
        "environments": [
            'python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64" and implementation_name == "cpython" and platform_system == "Linux"',
            'python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64" and implementation_name == "cpython" and platform_system == "Linux"',
            'python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64" and implementation_name == "cpython" and platform_system == "Linux"',
        ],
    },
    {
        "lock": "scientific-python-multi",
        "package": "importlib-resources",
        "marker": '(python_version == "3.9" and sys_platform == "darwin" and platform_machine == "arm64") or (python_version == "3.9" and sys_platform == "linux" and platform_machine == "x86_64") or (python_version == "3.9" and sys_platform == "win32" and platform_machine == "AMD64")',
        "environments": [
            'python_version == "3.9" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython"',
            'python_version == "3.9" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython"',
            'python_version == "3.9" and sys_platform == "win32" and platform_machine == "AMD64" and platform_python_implementation == "CPython"',
            'python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython"',
            'python_version == "3.10" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython"',
            'python_version == "3.10" and sys_platform == "win32" and platform_machine == "AMD64" and platform_python_implementation == "CPython"',
            'python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython"',
            'python_version == "3.11" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython"',
            'python_version == "3.11" and sys_platform == "win32" and platform_machine == "AMD64" and platform_python_implementation == "CPython"',
            'python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython"',
            'python_version == "3.12" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython"',
            'python_version == "3.12" and sys_platform == "win32" and platform_machine == "AMD64" and platform_python_implementation == "CPython"',
        ],
    },
    {
        "lock": "scientific-python-multi",
        "package": "zipp",
        "marker": '(python_version == "3.9" and sys_platform == "darwin" and platform_machine == "arm64") or (python_version == "3.9" and sys_platform == "linux" and platform_machine == "x86_64") or (python_version == "3.9" and sys_platform == "win32" and platform_machine == "AMD64")',
        "environments": [
            'python_version == "3.9" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython"',
            'python_version == "3.9" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython"',
            'python_version == "3.9" and sys_platform == "win32" and platform_machine == "AMD64" and platform_python_implementation == "CPython"',
            'python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython"',
            'python_version == "3.10" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython"',
            'python_version == "3.10" and sys_platform == "win32" and platform_machine == "AMD64" and platform_python_implementation == "CPython"',
            'python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython"',
            'python_version == "3.11" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython"',
            'python_version == "3.11" and sys_platform == "win32" and platform_machine == "AMD64" and platform_python_implementation == "CPython"',
            'python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython"',
            'python_version == "3.12" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython"',
            'python_version == "3.12" and sys_platform == "win32" and platform_machine == "AMD64" and platform_python_implementation == "CPython"',
        ],
    },
    {
        "lock": "starlette-fastapi-universal",
        "package": "exceptiongroup",
        "marker": '(python_version == "3.10" and sys_platform == "darwin" and platform_machine == "arm64") or (python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64") or (python_version == "3.10" and sys_platform == "win32" and platform_machine == "AMD64")',
        "environments": [
            'python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.10" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
            'python_version == "3.10" and sys_platform == "win32" and platform_machine == "AMD64" and platform_python_implementation == "CPython" and platform_system == "Windows"',
            'python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.11" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
            'python_version == "3.11" and sys_platform == "win32" and platform_machine == "AMD64" and platform_python_implementation == "CPython" and platform_system == "Windows"',
            'python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.12" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
            'python_version == "3.12" and sys_platform == "win32" and platform_machine == "AMD64" and platform_python_implementation == "CPython" and platform_system == "Windows"',
        ],
    },
    {
        "lock": "top88-parallel",
        "package": "colorama",
        "marker": 'python_version == "3.11" and sys_platform == "win32" and platform_machine == "AMD64"',
        "environments": [
            'python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.11" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
            'python_version == "3.11" and sys_platform == "win32" and platform_machine == "AMD64" and platform_python_implementation == "CPython" and platform_system == "Windows"',
        ],
    },
    {
        "lock": "top88-parallel",
        "package": "greenlet",
        "marker": '(python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64") or (python_version == "3.11" and sys_platform == "win32" and platform_machine == "AMD64")',
        "environments": [
            'python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.11" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
            'python_version == "3.11" and sys_platform == "win32" and platform_machine == "AMD64" and platform_python_implementation == "CPython" and platform_system == "Windows"',
        ],
    },
    {
        "lock": "top88-pypi-stress",
        "package": "colorama",
        "marker": 'python_version == "3.11" and sys_platform == "win32" and platform_machine == "AMD64"',
        "environments": [
            'python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.11" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
            'python_version == "3.11" and sys_platform == "win32" and platform_machine == "AMD64" and platform_python_implementation == "CPython" and platform_system == "Windows"',
        ],
    },
    {
        "lock": "top88-pypi-stress",
        "package": "greenlet",
        "marker": '(python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64") or (python_version == "3.11" and sys_platform == "win32" and platform_machine == "AMD64")',
        "environments": [
            'python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.11" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
            'python_version == "3.11" and sys_platform == "win32" and platform_machine == "AMD64" and platform_python_implementation == "CPython" and platform_system == "Windows"',
        ],
    },
    {
        "lock": "transformers-cpu-multi-python",
        "package": "async-timeout",
        "marker": '(python_version == "3.10" and sys_platform == "darwin" and platform_machine == "arm64") or (python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64")',
        "environments": [
            'python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.10" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
            'python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.11" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
            'python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.12" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
        ],
    },
    {
        "lock": "transformers-cpu-multi-python",
        "package": "nvidia-cublas-cu12",
        "marker": '(python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64") or (python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64") or (python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64")',
        "environments": [
            'python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.10" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
            'python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.11" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
            'python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.12" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
        ],
    },
    {
        "lock": "transformers-cpu-multi-python",
        "package": "nvidia-cuda-cupti-cu12",
        "marker": '(python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64") or (python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64") or (python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64")',
        "environments": [
            'python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.10" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
            'python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.11" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
            'python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.12" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
        ],
    },
    {
        "lock": "transformers-cpu-multi-python",
        "package": "nvidia-cuda-nvrtc-cu12",
        "marker": '(python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64") or (python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64") or (python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64")',
        "environments": [
            'python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.10" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
            'python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.11" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
            'python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.12" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
        ],
    },
    {
        "lock": "transformers-cpu-multi-python",
        "package": "nvidia-cuda-runtime-cu12",
        "marker": '(python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64") or (python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64") or (python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64")',
        "environments": [
            'python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.10" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
            'python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.11" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
            'python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.12" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
        ],
    },
    {
        "lock": "transformers-cpu-multi-python",
        "package": "nvidia-cudnn-cu12",
        "marker": '(python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64") or (python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64") or (python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64")',
        "environments": [
            'python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.10" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
            'python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.11" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
            'python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.12" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
        ],
    },
    {
        "lock": "transformers-cpu-multi-python",
        "package": "nvidia-cufft-cu12",
        "marker": '(python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64") or (python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64") or (python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64")',
        "environments": [
            'python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.10" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
            'python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.11" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
            'python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.12" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
        ],
    },
    {
        "lock": "transformers-cpu-multi-python",
        "package": "nvidia-cufile-cu12",
        "marker": '(python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64") or (python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64") or (python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64")',
        "environments": [
            'python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.10" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
            'python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.11" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
            'python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.12" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
        ],
    },
    {
        "lock": "transformers-cpu-multi-python",
        "package": "nvidia-curand-cu12",
        "marker": '(python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64") or (python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64") or (python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64")',
        "environments": [
            'python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.10" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
            'python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.11" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
            'python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.12" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
        ],
    },
    {
        "lock": "transformers-cpu-multi-python",
        "package": "nvidia-cusolver-cu12",
        "marker": '(python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64") or (python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64") or (python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64")',
        "environments": [
            'python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.10" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
            'python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.11" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
            'python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.12" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
        ],
    },
    {
        "lock": "transformers-cpu-multi-python",
        "package": "nvidia-cusparse-cu12",
        "marker": '(python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64") or (python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64") or (python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64")',
        "environments": [
            'python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.10" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
            'python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.11" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
            'python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.12" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
        ],
    },
    {
        "lock": "transformers-cpu-multi-python",
        "package": "nvidia-cusparselt-cu12",
        "marker": '(python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64") or (python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64") or (python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64")',
        "environments": [
            'python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.10" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
            'python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.11" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
            'python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.12" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
        ],
    },
    {
        "lock": "transformers-cpu-multi-python",
        "package": "nvidia-nccl-cu12",
        "marker": '(python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64") or (python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64") or (python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64")',
        "environments": [
            'python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.10" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
            'python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.11" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
            'python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.12" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
        ],
    },
    {
        "lock": "transformers-cpu-multi-python",
        "package": "nvidia-nvjitlink-cu12",
        "marker": '(python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64") or (python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64") or (python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64")',
        "environments": [
            'python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.10" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
            'python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.11" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
            'python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.12" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
        ],
    },
    {
        "lock": "transformers-cpu-multi-python",
        "package": "nvidia-nvtx-cu12",
        "marker": '(python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64") or (python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64") or (python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64")',
        "environments": [
            'python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.10" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
            'python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.11" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
            'python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.12" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
        ],
    },
    {
        "lock": "transformers-cpu-multi-python",
        "package": "setuptools",
        "marker": '(python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64") or (python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64") or (python_version == "3.12" and sys_platform == "darwin" and platform_machine == "arm64") or (python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64")',
        "environments": [
            'python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.10" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
            'python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.11" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
            'python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.12" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
        ],
    },
    {
        "lock": "transformers-cpu-multi-python",
        "package": "triton",
        "marker": '(python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64") or (python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64") or (python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64")',
        "environments": [
            'python_version == "3.10" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.10" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
            'python_version == "3.11" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.11" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
            'python_version == "3.12" and sys_platform == "linux" and platform_machine == "x86_64" and platform_python_implementation == "CPython" and platform_system == "Linux"',
            'python_version == "3.12" and sys_platform == "darwin" and platform_machine == "arm64" and platform_python_implementation == "CPython" and platform_system == "Darwin"',
        ],
    },
]
