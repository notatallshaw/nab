"""Hold the package to the API its own docstring advertises.

``nab_resolver`` re-exports nothing, so the module paths listed in its
docstring are the whole surface an embedder has to import from.  A name that
moves out from under one of them breaks the promise rather than refactoring
behind it, and the README carries a second copy of the list that can drift.

The docstring being true is not the same as it being current, so a new public
name in a promised module has to be either promised or written off below.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path
from types import ModuleType

import nab_resolver

README = Path(__file__).resolve().parents[1] / "README.md"

_ROW = re.compile(r"(?P<indent> *)(?P<module>nab_resolver\.\w+) {2,}(?P<names>\S.*)")
_NAME_LIST = re.compile(r"\w+(?:, *\w+)*,?")

ROW_SHAPE = "nab_resolver.<module><spaces><Name>[, <Name>...]"


def _split_names(text: str) -> list[str]:
    return [name for name in re.split(r"[,\s]+", text.strip()) if name]


def _wraps(line: str, row_indent: int) -> bool:
    """Whether ``line`` is a row's name list spilling past the module column."""
    return _NAME_LIST.fullmatch(line.strip()) is not None and (
        len(line) - len(line.lstrip()) > row_indent
    )


def _api_table(text: str) -> dict[str, set[str]]:
    """Module path to names, read off the aligned table somewhere in ``text``.

    Rows are found by their own shape rather than by the prose around them, so
    the wording of either the docstring or the README can be rewritten without
    touching this.
    """
    table: dict[str, set[str]] = {}
    names: set[str] | None = None
    row_indent = 0

    for line in text.splitlines():
        row = _ROW.fullmatch(line)
        if row is not None:
            row_indent = len(row["indent"])
            names = table.setdefault(row["module"], set())
            names.update(_split_names(row["names"]))
        elif names is not None and _wraps(line, row_indent):
            names.update(_split_names(line))
        else:
            names = None

    return table


DOCSTRING_API = _api_table(nab_resolver.__doc__ or "")

# Declared public by a promised module but left out of the promise. A name
# promised at the module that defines it is absent here even where a second
# promised module re-exports it.
UNPROMISED = {
    "nab_resolver.ranges": {
        "NEGATIVE_INFINITY",
        "POSITIVE_INFINITY",
        "Bound",
        "Interval",
    },
    "nab_resolver.resolver": {"IncompatibilityState", "ResolverStats", "SetRelation"},
    "nab_resolver.types": {
        "IncompatibilityState",
        "PackageType",
        "RangeRelation",
        "RelationProtocol",
        "SetRelation",
        "VersionType",
    },
}


def test_docstring_carries_an_api_table() -> None:
    """Guard the other checks, which pass on an empty table without this."""
    assert DOCSTRING_API, (
        "nab_resolver.__doc__ lists no API module paths. Expected one or more"
        f" indented rows shaped {ROW_SHAPE!r}, got: {nab_resolver.__doc__!r}"
    )


def test_every_documented_name_is_importable() -> None:
    missing: list[str] = []
    for module, names in sorted(DOCSTRING_API.items()):
        try:
            imported = importlib.import_module(module)
        except ModuleNotFoundError:
            missing.extend(f"{module}.{name}" for name in sorted(names))
            continue
        missing.extend(
            f"{module}.{name}" for name in sorted(names) if not hasattr(imported, name)
        )

    assert missing == [], f"documented but not importable: {missing}"


def test_no_public_name_in_a_promised_module_goes_undecided() -> None:
    """Force a choice when a promised module gains or loses a public name.

    Importability alone cannot see a name the docstring never mentioned, which
    is how a class as embedder-facing as ``BaseProvider`` reached ``__all__``
    with nothing to notice it.
    """
    promised = set().union(*DOCSTRING_API.values())

    for module in sorted(DOCSTRING_API):
        declared = getattr(importlib.import_module(module), "__all__", None)
        assert declared is not None, f"{module} promises names without an __all__"

        assert set(declared) - promised == UNPROMISED.get(module, set()), (
            f"{module}.__all__ no longer matches the promise. Add each new name"
            f" to the docstring table or to UNPROMISED, and drop any UNPROMISED"
            f" entry the module has stopped declaring. __all__ is {sorted(declared)}"
        )


def test_readme_lists_the_same_paths() -> None:
    assert _api_table(README.read_text(encoding="utf-8")) == DOCSTRING_API


def test_package_root_exports_nothing() -> None:
    """Submodules bind themselves onto the package, so only those are allowed."""
    bound = {
        name
        for name, value in vars(nab_resolver).items()
        if not name.startswith("_") and not isinstance(value, ModuleType)
    }

    assert bound == set(), f"the package root must re-export nothing, found: {bound}"
