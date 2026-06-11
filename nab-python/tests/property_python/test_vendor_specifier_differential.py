"""Differential Specifier/SpecifierSet semantics: vendored vs upstream.

The vendored ``specifiers.py`` is rewritten on top of the
``VersionRange`` machinery, so parse acceptance, containment,
filtering, intersection, and version comparison must stay identical to
upstream across exotic PEP 440 shapes (epoch, local, dev, post, pre,
wildcards, ``~=``, ``===``, prereleases flag).  Outcomes compare
raised exception class names too, so error behavior cannot drift
silently.  Inputs mix a curated pool of awkward literals with
generated version shapes.
"""

from __future__ import annotations

from types import ModuleType

import packaging.specifiers as up_specifiers
import packaging.version as up_version
import pytest
from hypothesis import given
from hypothesis import strategies as st

import nab_python._vendor.packaging.specifiers as nb_specifiers
import nab_python._vendor.packaging.version as nb_version

from .strategies import DEEP_SETTINGS

pytestmark = pytest.mark.property

OPS = ("<", "<=", "==", "!=", ">=", ">", "~=", "===")

CURATED_VERSIONS = (
    "1.0",
    "1",
    "2.0.1",
    "1!1.0",
    "2!1",
    "1.0+abc",
    "1.0+abc.2",
    "1.0+ubuntu-1",
    "1.0.dev1",
    "1.0.dev0",
    "1.0.post1",
    "1.0.post0.dev1",
    "1.0rc1",
    "1.0a1",
    "1.0b2.post345.dev456",
    "0.dev0",
    "1.2.3.4.5",
    "01.0",
    "v1.0",
    "3.11",
    "3.11.0",
    "1.1.0",
    "1.0.0",
)
CURATED_SPEC_VERSIONS = (
    *CURATED_VERSIONS,
    "3.*",
    "1.0.*",
    "1.*",
    "1.0a1.*",
    "1.0.post1.*",
    "*",
)

prerelease_flags = st.sampled_from([None, True, False])


@st.composite
def generated_version_strings(draw: st.DrawFn) -> str:
    epoch = draw(st.one_of(st.none(), st.integers(0, 2)))
    release = ".".join(
        str(draw(st.integers(0, 20))) for _ in range(draw(st.integers(1, 4)))
    )
    pre = draw(
        st.one_of(st.none(), st.sampled_from(["a0", "b1", "rc2", ".preview3", "-c4"]))
    )
    post = draw(st.one_of(st.none(), st.sampled_from([".post0", "-2", "r3"])))
    dev = draw(st.one_of(st.none(), st.sampled_from([".dev0", "dev1"])))
    local = draw(
        st.one_of(st.none(), st.sampled_from(["+l", "+abc.7", "+0", "+a-b_c.1"]))
    )
    v = release
    if epoch is not None:
        v = f"{epoch}!{v}"
    if pre is not None:
        v += pre
    if post is not None:
        v += post
    if dev is not None:
        v += dev
    if local is not None:
        v += local
    return v


version_strings = st.one_of(
    st.sampled_from(CURATED_VERSIONS), generated_version_strings()
)


@st.composite
def curated_spec_strings(draw: st.DrawFn) -> str:
    op = draw(st.sampled_from(OPS))
    ver = draw(st.sampled_from(CURATED_SPEC_VERSIONS))
    # Upstream 26.2 bug: ~= with a v prefix matches nothing; the vendored copy follows PEP 440.
    if op == "~=" and ver.startswith("v"):
        return f"~={ver[1:]}"
    space = draw(st.sampled_from(["", " "]))
    return f"{op}{space}{ver}"


@st.composite
def generated_spec_strings(draw: st.DrawFn) -> str:
    op = draw(st.sampled_from(OPS))
    v = draw(generated_version_strings())
    if op == "~=":
        v = v.split("+")[0]  # ~= forbids local
        if "!" in v:
            head, _, tail = v.partition("!")
            if tail.count(".") == 0:
                v = f"{head}!{tail}.0"
        elif v.count(".") == 0:
            v += ".0"
    if op in ("==", "!=") and draw(st.booleans()):
        base = v.split("+")[0]
        # wildcard only after a release segment
        if base.replace(".", "").isdigit() or "!" in base:
            v = base + ".*"
    return op + v


spec_strings = st.one_of(curated_spec_strings(), generated_spec_strings())


@st.composite
def spec_set_strings(draw: st.DrawFn) -> str:
    n = draw(st.integers(0, 3))
    return ",".join(draw(spec_strings) for _ in range(n))


def _contains_outcome(
    mod: ModuleType, spec: str, item: str, flag: bool | None
) -> tuple[str, object]:
    try:
        specifier = mod.Specifier(spec)
    except Exception as exc:  # noqa: BLE001
        return ("parse-error", type(exc).__name__)
    try:
        return ("value", specifier.contains(item, prereleases=flag))
    except Exception as exc:  # noqa: BLE001
        return ("contains-error", type(exc).__name__)


def _set_contains_outcome(
    mod: ModuleType, spec: str, item: str, flag: bool | None
) -> tuple[str, object]:
    try:
        spec_set = mod.SpecifierSet(spec)
    except Exception as exc:  # noqa: BLE001
        return ("parse-error", type(exc).__name__)
    try:
        return ("value", spec_set.contains(item, prereleases=flag))
    except Exception as exc:  # noqa: BLE001
        return ("contains-error", type(exc).__name__)


def _filter_outcome(
    mod: ModuleType, spec: str, items: list[str], flag: bool | None
) -> tuple[str, object]:
    try:
        spec_set = mod.SpecifierSet(spec)
    except Exception as exc:  # noqa: BLE001
        return ("parse-error", type(exc).__name__)
    try:
        return (
            "value",
            [str(v) for v in spec_set.filter(items, prereleases=flag)],
        )
    except Exception as exc:  # noqa: BLE001
        return ("filter-error", type(exc).__name__)


def _intersection_outcome(mod: ModuleType, a: str, b: str) -> tuple[str, object]:
    try:
        return ("value", str(mod.SpecifierSet(a) & mod.SpecifierSet(b)))
    except Exception as exc:  # noqa: BLE001
        return ("error", type(exc).__name__)


@DEEP_SETTINGS
@given(spec=spec_strings, item=version_strings, flag=prerelease_flags)
def test_specifier_contains_matches_upstream(
    spec: str, item: str, flag: bool | None
) -> None:
    up = _contains_outcome(up_specifiers, spec, item, flag)
    nb = _contains_outcome(nb_specifiers, spec, item, flag)
    assert up == nb, f"Specifier({spec!r}).contains({item!r}, prereleases={flag})"


@DEEP_SETTINGS
@given(spec=spec_set_strings(), item=version_strings, flag=prerelease_flags)
def test_specifier_set_contains_matches_upstream(
    spec: str, item: str, flag: bool | None
) -> None:
    up = _set_contains_outcome(up_specifiers, spec, item, flag)
    nb = _set_contains_outcome(nb_specifiers, spec, item, flag)
    assert up == nb, f"SpecifierSet({spec!r}).contains({item!r}, prereleases={flag})"


@DEEP_SETTINGS
@given(
    spec=spec_set_strings(),
    items=st.lists(version_strings, min_size=0, max_size=6),
    flag=prerelease_flags,
)
def test_specifier_set_filter_matches_upstream(
    spec: str, items: list[str], flag: bool | None
) -> None:
    up = _filter_outcome(up_specifiers, spec, items, flag)
    nb = _filter_outcome(nb_specifiers, spec, items, flag)
    assert up == nb, f"SpecifierSet({spec!r}).filter({items!r}, prereleases={flag})"


@DEEP_SETTINGS
@given(a=spec_set_strings(), b=spec_set_strings())
def test_specifier_set_intersection_matches_upstream(a: str, b: str) -> None:
    up = _intersection_outcome(up_specifiers, a, b)
    nb = _intersection_outcome(nb_specifiers, a, b)
    assert up == nb, f"({a!r}) & ({b!r})"


@DEEP_SETTINGS
@given(left=version_strings, right=version_strings)
def test_version_compare_matches_upstream(left: str, right: str) -> None:
    try:
        upl, upr = up_version.Version(left), up_version.Version(right)
        up: tuple[str, object] = (
            "value",
            (upl < upr, upl == upr, str(upl), upl.release),
        )
    except Exception as exc:  # noqa: BLE001
        up = ("error", type(exc).__name__)
    try:
        nbl, nbr = nb_version.Version(left), nb_version.Version(right)
        nb: tuple[str, object] = (
            "value",
            (nbl < nbr, nbl == nbr, str(nbl), nbl.release),
        )
    except Exception as exc:  # noqa: BLE001
        nb = ("error", type(exc).__name__)
    assert up == nb, f"Version compare {left!r} vs {right!r}"
