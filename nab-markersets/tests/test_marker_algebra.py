"""Unit suite for the marker algebra: acceptance cases, algebra, and edges.

Correctness against packaging is pinned by ``test_marker_algebra_differential``;
this suite pins the ported acceptance cases and exercises the construction,
decision, restriction, serialisation, witness, and guard surfaces, and ends by
pinning the private packaging names the engine imports.
"""

from __future__ import annotations

import gc
import pickle
import sys
import traceback
import weakref

import pytest

from nab_markersets import errors, markersets
from nab_markersets._packaging import (
    InvalidMarker,
    Marker,
    Op,
    ParserSyntaxError,
    UndefinedEnvironmentName,
    Value,
    Variable,
    Version,
    _eval_op,
    parse_marker,
)
from nab_markersets.errors import IntractableMarkerSet, UnserializableMarkerSet
from nab_markersets.markersets import MarkerSet, _markersets, variable_names


def ms(text: str) -> MarkerSet:
    return MarkerSet.from_marker(text)


# --------------------------------------------------------------- acceptance


def test_onnxruntime_extras_disjoint() -> None:
    a = ms('"cpu" in extras and "gpu" not in extras')
    b = ms('"gpu" in extras and "cpu" not in extras')
    assert a.is_disjoint(b)
    assert not a.is_disjoint(a)


def test_pep751_same_variable_partitions() -> None:
    parts = [
        ms('python_full_version < "3.9"'),
        ms('python_full_version >= "3.9" and python_full_version < "3.12"'),
        ms('python_full_version >= "3.12"'),
    ]
    for i in range(len(parts)):
        for j in range(i + 1, len(parts)):
            assert parts[i].is_disjoint(parts[j])
    cpu = ms('"cpu" in extras')
    not_cpu = ms('"cpu" not in extras')
    assert cpu.is_disjoint(not_cpu)
    assert (cpu | not_cpu).is_full()


def test_a1_python_version_contradiction() -> None:
    assert ms('python_version == "3.9" and python_full_version == "3.10.1"').is_empty()
    assert not ms(
        'python_version == "3.10" and python_full_version == "3.10.1"'
    ).is_empty()


def test_prerelease_carve_out_314rc1() -> None:
    naive = ms('python_full_version < "3.14"') | ms('python_full_version >= "3.14"')
    assert not naive.is_full()
    corrected = ms('python_full_version < "3.14"') | ms(
        'python_full_version >= "3.14.dev0"'
    )
    assert corrected.is_full()
    assert ms('python_full_version < "3.14"').is_disjoint(
        ms('python_full_version >= "3.14"')
    )


def test_exclusive_above_prerelease_literal() -> None:
    above = ms('python_full_version > "3.14.0rc1"')
    from_final = ms('python_full_version >= "3.14"')
    assert not above.is_subset(from_final)
    assert not above.equivalent(from_final)
    gap = above & from_final.complement()
    assert not gap.is_empty()
    assert gap.witness() is not None


def test_exclusive_above_post_release_literal() -> None:
    covered = ms('python_full_version <= "3.14.0.post1"') | ms(
        'python_full_version > "3.14"'
    )
    assert not covered.is_full()
    above = ms('python_full_version > "3.14.0rc1.post1"')
    assert not above.is_subset(ms('python_full_version > "3.14.0rc1"'))
    band = ms('python_full_version > "3.14.0.post5"') & ms(
        'python_full_version < "3.14.1"'
    )
    assert not band.is_empty()


def test_exclusive_above_dev_literal() -> None:
    band = ms('python_full_version > "3.14.0.dev1"') & ms(
        'python_full_version < "3.14.1"'
    )
    assert not band.is_empty()
    env = band.witness()
    assert env is not None
    assert Marker(
        'python_full_version > "3.14.0.dev1" and python_full_version < "3.14.1"'
    ).evaluate(env)

    combined = ms('python_full_version > "3.14.0a1.dev2"') & ms(
        'python_full_version < "3.14.0a1"'
    )
    assert not combined.is_empty()
    env2 = combined.witness()
    assert env2 is not None
    assert Marker(
        'python_full_version > "3.14.0a1.dev2" and python_full_version < "3.14.0a1"'
    ).evaluate(env2)


def test_exclusive_below_dev_literal() -> None:
    band = ms('python_version >= "3.14.0.dev2" and python_full_version < "3.14.0.dev2"')
    assert not band.is_empty()
    env = band.witness()
    assert env is not None

    lower = ms('python_full_version < "3.14.0.dev2"') & ms(
        'python_full_version > "3.14.0.dev0"'
    )
    assert not lower.is_empty()


def test_open_band_holds_across_the_version_axes() -> None:
    bands = [
        (
            'python_full_version > "3" and python_full_version < "3.1"',
            {"python_full_version": "3.0.1"},
        ),
        (
            'python_full_version > "3.9" and python_full_version < "3.9.1"',
            {"python_full_version": "3.9.0.1"},
        ),
        (
            'python_full_version > "1!3" and python_full_version < "1!3.1"',
            {"python_full_version": "1!3.0.1"},
        ),
        (
            'implementation_version > "3" and implementation_version < "3.1"',
            {"implementation_version": "3.0.1"},
        ),
    ]
    for text, inside in bands:
        band = ms(text)
        assert not band.is_empty(), text
        assert band.evaluate(inside), text

        env = band.witness()
        assert env is not None, text
        assert Marker(text).evaluate(env), text


def test_dev0_literal_mints_no_below_neighbour() -> None:
    assert not ms('python_full_version == "3.14.0.dev0"').is_empty()


def test_local_literal_admits_its_public_release() -> None:
    # CPython 3.10.0 satisfies both markers: the swapped == builds the public
    # specifier ==3.10.0, which ignores the literal's local label.
    swapped_eq = ms('"3.10+abc" == python_full_version')
    swapped_ge = ms('"3.10.1rc2" >= python_full_version')
    both = swapped_eq & swapped_ge
    shared = {"python_full_version": "3.10.0", "python_version": "3.10"}
    assert both.evaluate(shared)
    assert not swapped_eq.is_disjoint(swapped_ge)
    assert not both.is_empty()

    env = both.witness()
    assert env is not None
    assert Marker(
        '"3.10+abc" == python_full_version and "3.10.1rc2" >= python_full_version'
    ).evaluate(env)


def test_local_literal_padded_twin_refutes_subset() -> None:
    # ==3.10+abc admits the padded 3.10.0+abc, where the invalid-specifier
    # <= clause degrades to string equality and fails.
    eq = ms('implementation_version == "3.10+abc"')
    le = ms('implementation_version <= "3.10+abc"')
    padded = {"implementation_version": "3.10.0+abc"}
    assert eq.evaluate(padded)
    assert not le.evaluate(padded)
    assert not eq.is_subset(le)


def test_membership_reads_padded_twin_as_a_string() -> None:
    # CPython 3.10.0 satisfies both: the swapped == builds the specifier ==3.10.0,
    # which zero-pads the literal, while not in is a raw string test "3.10.0" passes.
    swapped_eq = ms('"3.10" == python_full_version')
    absent = ms('python_full_version not in "3.9 3.10"')
    shared = {"python_full_version": "3.10.0", "python_version": "3.10"}

    assert swapped_eq.evaluate(shared)
    assert absent.evaluate(shared)
    assert not swapped_eq.is_disjoint(absent)

    both = swapped_eq & absent
    assert not both.is_empty()

    env = both.witness()
    assert env is not None
    assert Marker(
        '"3.10" == python_full_version and python_full_version not in "3.9 3.10"'
    ).evaluate(env)


def test_membership_padded_twin_on_a_version_or_string_axis() -> None:
    # platform_release dispatches as a version yet admits arbitrary strings, so its
    # pool needs the padded twin as much as a version-only axis does.
    marker = ms('"5.10" == platform_release and platform_release not in "5.10 5.11"')
    assert not marker.is_empty()

    env = marker.witness()
    assert env is not None
    assert Marker(
        '"5.10" == platform_release and platform_release not in "5.10 5.11"'
    ).evaluate(env)


def test_m1_string_ordering_non_negation() -> None:
    less = ms('sys_platform < "linux"')
    greater_equal = ms('sys_platform >= "linux"')
    assert not less.complement().equivalent(greater_equal)
    assert not (less | greater_equal).is_full()
    assert less.is_empty()  # < is constant-false on a string variable.


def test_uv_comma_list_non_collapse() -> None:
    assert not ms('python_version in "3.10"').equivalent(ms('python_version == "3.10"'))


def test_deplogic_tautology() -> None:
    assert ms('os_name == "a" or os_name == "b" or os_name != "a"').is_full()


def test_poetry_allows_all_not_in() -> None:
    notin = ms('"x86" not in platform_machine')
    neq = ms('platform_machine != "arm"')
    assert not notin.is_subset(neq)


def test_parenthesisation_round_trip() -> None:
    grouped = ms(
        'sys_platform == "linux" and '
        '(python_version == "3.8" or python_version == "3.9")'
    )
    text = grouped.to_marker_string()
    assert text is not None
    assert "(" in text
    assert grouped.equivalent(ms(text))


# ------------------------------------------------------------- construction


def test_true_false_and_absent_marker() -> None:
    assert MarkerSet.full().is_full()
    assert MarkerSet.empty().is_empty()
    assert ms("").is_full()
    assert ms("   ").is_full()


def test_from_marker_accepts_marker_object() -> None:
    marker = Marker('sys_platform == "linux"')
    assert MarkerSet.from_marker(marker).equivalent(ms('sys_platform == "linux"'))


def test_from_marker_rejects_other_types() -> None:
    with pytest.raises(
        TypeError,
        match=r"expected str or packaging\.markers\.Marker, got builtins\.int",
    ):
        MarkerSet.from_marker(42)  # type: ignore[arg-type]


def test_from_marker_rejects_malformed_string() -> None:
    # A malformed marker raises the public InvalidMarker, exactly as
    # packaging.markers.Marker does, not the tokenizer's internal error.
    with pytest.raises(InvalidMarker):
        MarkerSet.from_marker("sys_platform ==")
    with pytest.raises(InvalidMarker):
        Marker("sys_platform ==")


def test_direct_construction_is_refused() -> None:
    # The op-tree is private; a set is built only through the factories.
    with pytest.raises(TypeError, match="from_marker"):
        MarkerSet()  # type: ignore[call-arg]


def test_pickle_writes_but_load_is_refused() -> None:
    payload = pickle.dumps(MarkerSet.full())
    assert payload

    with pytest.raises(TypeError, match="from_marker"):
        pickle.loads(payload)  # noqa: S301 - exercise the documented load failure


# ---------------------------------------------------------------- interning


_CONSTRUCTION_PATHS = (
    'python_full_version >= "3.10"',  # a plain value atom
    'python_version < "3.12"',  # A1-lowered onto python_full_version
    '"x" in platform_release',  # an opaque contains atom
    'platform_release in "5.10"',  # the exact-substring direction
    'extra == "dev"',  # a set atom
    '"linux" == sys_platform',  # a swapped value atom
)


def _atoms(text: str) -> list[_markersets.Atom]:
    return _markersets.collect_atoms(_markersets.parse(text))


@pytest.mark.parametrize("text", _CONSTRUCTION_PATHS)
def test_every_construction_path_returns_the_interned_atom(text: str) -> None:
    """A second parse of a clause reaches the atom the first parse built.

    Atoms compare and hash by identity, so a construction path that skipped the
    table would leave the algebra carrying one leaf as two.
    """
    (first,) = _atoms(text)
    (second,) = _atoms(text)

    assert second is first


def test_reversed_operands_do_not_collapse_onto_one_atom() -> None:
    """The intern key carries every field, ``swapped`` included.

    Only ``swapped`` separates these two leaves, and sharing one atom between
    them would read the literal as the same operand in both, making two
    disjoint sets equal.
    """
    (below,) = _atoms('python_full_version < "3.10"')
    (above,) = _atoms('"3.10" < python_full_version')

    assert below is not above

    lower = ms('python_full_version < "3.10"')
    upper = ms('"3.10" < python_full_version')

    assert lower.is_disjoint(upper)


def test_a_complement_interns_back_to_the_atom_it_negates() -> None:
    """``replaced`` interns too, so complementing twice returns the original."""
    (atom,) = _atoms('sys_platform == "linux"')

    assert atom.replaced(op="!=").replaced(op="==") is atom

    (contains,) = _atoms('"x" in platform_release')

    assert contains.replaced(positive=False).replaced(positive=True) is contains


def test_an_atom_leaves_the_table_with_the_last_tree_holding_it() -> None:
    """Nothing prunes this table, so a strong one would only ever grow."""
    tree = _markersets.parse('extra == "nab-intern-probe"')
    (atom,) = _markersets.collect_atoms(tree)
    probe = weakref.ref(atom)

    assert atom in _markersets._INTERNED.values()

    del tree, atom
    gc.collect()

    assert probe() is None


# ----------------------------------------------------------- module surface


def test_all_and_dir_pin_the_public_surface() -> None:
    # Spelled out, so adding a name is a decision taken here, in the package
    # docstring's table and in the README, not a leak through __all__.
    assert markersets.__all__ == ["DecisionStore", "MarkerSet", "variable_names"]
    assert errors.__all__ == ["IntractableMarkerSet", "UnserializableMarkerSet"]

    # __dir__ hides the budget constants and the engine submodule, so a
    # completion in a REPL offers the supported surface and nothing else.
    assert dir(markersets) == sorted(markersets.__all__)
    assert dir(errors) == sorted(errors.__all__)

    # Promised at markersets, so that is the module it reports.
    assert markersets.variable_names.__module__ == "nab_markersets.markersets"


# ----------------------------------------------------------------- algebra


def test_and_or_complement() -> None:
    a = ms('sys_platform == "linux"')
    b = ms('os_name == "posix"')
    assert (a & b).is_subset(a)
    assert a.is_subset(a | b)
    assert a.complement().is_disjoint(a)
    assert (a | a.complement()).is_full()


def test_double_complement_and_identity_folding() -> None:
    a = ms('sys_platform == "linux"')
    assert a.complement().complement().equivalent(a)
    assert (a & MarkerSet.full()).equivalent(a)
    assert (a | MarkerSet.empty()).equivalent(a)
    assert (a | MarkerSet.full()).is_full()
    assert (a & MarkerSet.empty()).is_empty()


def test_and_of_identical_atoms_dedupes() -> None:
    a = ms('sys_platform == "linux"')
    assert (a & a).equivalent(a)


def test_named_algebra_matches_operators() -> None:
    a = ms('sys_platform == "linux"')
    b = ms('os_name == "posix"')
    assert a.intersection(b).equivalent(a & b)
    assert a.union(b).equivalent(a | b)
    assert a.complement().equivalent(~a)


def test_is_superset_mirrors_is_subset() -> None:
    inner = ms('sys_platform == "linux"')
    outer = ms('sys_platform == "linux"') | ms('os_name == "posix"')
    assert outer.is_superset(inner)
    assert not inner.is_superset(outer)


def test_operators_reject_foreign_operands() -> None:
    a = ms('sys_platform == "linux"')
    for other in ("linux", 3, None):
        assert a.__and__(other) is NotImplemented
        assert a.__or__(other) is NotImplemented
        assert a.__sub__(other) is NotImplemented
        assert a.__eq__(other) is NotImplemented
        assert a != other
    with pytest.raises(TypeError):
        _ = a & "linux"  # type: ignore[operator]


def test_difference_is_intersection_with_the_complement() -> None:
    a = ms('python_version >= "3.9"')
    b = ms('python_version >= "3.12"')

    assert (a - b).equivalent(a & ~b)
    assert (a - b).is_disjoint(b)
    assert a.difference(a).is_empty()


def test_equality_is_structural_not_semantic() -> None:
    a = ms('sys_platform == "linux"')
    b = ms('os_name == "posix"')

    assert ms('sys_platform == "linux"') == a
    assert hash(ms('sys_platform == "linux"')) == hash(a)
    # A constant hash would satisfy the line above and nothing else.
    assert hash(a) != hash(b)
    assert len({a, ms('sys_platform == "linux"')}) == 1
    assert {a: "kept"}[ms('sys_platform == "linux"')] == "kept"

    assert MarkerSet.full() == MarkerSet.full()
    assert MarkerSet.full() != MarkerSet.empty()

    # Two spellings of one set are unequal, which is what equivalent is for.
    assert (a & b) != (b & a)
    assert (a & b).equivalent(b & a)
    assert (a | ~a) != MarkerSet.full()
    assert (a | ~a).is_full()


def test_contains_is_evaluate() -> None:
    gpu = ms('extra == "gpu"')

    assert {"extra": frozenset({"gpu"})} in gpu
    assert {"extra": frozenset({"cpu"})} not in gpu


def test_every_pairwise_predicate_reaches_the_store_it_is_given() -> None:
    # One store per call, so a predicate that drops the keyword leaves an empty
    # one rather than riding on a sibling's entries.
    a = ms('python_version >= "3.9" and sys_platform == "linux"')
    b = ms('python_version >= "3.12"')
    calls = (
        lambda store: a.is_disjoint(b, store=store),
        lambda store: a.is_subset(b, store=store),
        lambda store: a.is_superset(b, store=store),
        lambda store: a.equivalent(b, store=store),
        lambda store: a.is_empty(store=store),
        lambda store: a.is_full(store=store),
    )

    for call in calls:
        store = markersets.DecisionStore()
        assert not call(store)
        assert store.decisions


# ------------------------------------------------- leaf-shape edge cases


def test_variable_vs_variable_is_faithful() -> None:
    # packaging turns the RHS variable name into the literal; the two differ.
    assert not ms("os_name == sys_platform").is_full()
    assert ms("os_name == sys_platform").evaluate(
        {"os_name": "sys_platform", "sys_platform": "sys_platform"}
    )
    assert not ms("os_name != sys_platform").evaluate(
        {"os_name": "sys_platform", "sys_platform": "sys_platform"}
    )


def test_const_vs_const_folds() -> None:
    assert ms('"linux" == "linux"').is_full()
    assert ms('"linux" == "win32"').is_empty()


def test_const_vs_known_variable_literal_routes_like_swap() -> None:
    platform = ms('"linux" == "sys_platform"')
    assert not platform.is_empty()
    assert platform.evaluate({"sys_platform": "linux"})
    assert not platform.evaluate({"sys_platform": "win32"})
    version = ms('"3.9" == "python_version"')
    assert version.evaluate({"python_version": "3.9"})
    assert not version.evaluate({"python_version": "3.11"})


def test_swapped_atoms() -> None:
    assert ms('"linux" == sys_platform').equivalent(ms('sys_platform == "linux"'))
    assert ms('"3.9" == python_version').evaluate({"python_full_version": "3.9.4"})


@pytest.mark.parametrize(
    "text",
    [
        'sys_platform ~= "1"',
        'sys_platform === "1"',
        '"1" ~= sys_platform',
        'python_version ~= "3"',
        'python_version ~= "2!0"',
        'python_version ~= "0"',
        '"1.0" ~= python_full_version',
        'python_full_version === "3.9"',
        '"3.9" === python_full_version',
        '"1.0" ~= python_version',
        '"2!0" ~= python_version',
        '"3.14.0rc1" ~= python_version',
        '"3.14.0.dev0" ~= python_version',
    ],
)
def test_undefined_operator_rejected_at_construction(text: str) -> None:
    with pytest.raises(ValueError, match="undefined"):
        ms(text)


@pytest.mark.parametrize(
    "text",
    [
        '"1.0" === python_version',
        'python_version ~= "3.9"',
    ],
)
def test_defined_ordered_operator_decides(text: str) -> None:
    assert isinstance(ms(text).is_empty(), bool)


def test_version_tilde_operator_builds() -> None:
    # ~= parses as a specifier on a version field, so it does not raise.
    assert ms('python_full_version ~= "3.9"').evaluate({"python_full_version": "3.9.4"})


@pytest.mark.parametrize(
    "text",
    [
        'python_full_version ~= "' + "9" * 5000 + '.0"',
        '"' + "9" * 5000 + '.0" ~= python_full_version',
        'implementation_version ~= "' + "9" * 5000 + '.0"',
        'platform_release ~= "' + "9" * 5000 + '.0"',
    ],
)
def test_oversized_tilde_literal_reports_complexity(text: str) -> None:
    # A ~= literal whose numeric component overruns the int-from-string limit
    # crashes packaging's Specifier with a bare ValueError at parse time; the
    # bounded guard fires during construction instead.
    with pytest.raises(IntractableMarkerSet):
        ms(text)


def test_membership_var_in_literal_is_exact() -> None:
    inside = ms('sys_platform in "linuxx"')
    assert inside.evaluate({"sys_platform": "linux"})
    assert inside.evaluate({"sys_platform": "linuxx"})
    assert not inside.evaluate({"sys_platform": "windows"})


def test_python_version_membership_is_opaque_contains() -> None:
    marker = ms('"3" in python_version')
    assert marker.evaluate({"python_version": "3.9"})
    assert not marker.evaluate({"python_version": "2.7"})


def test_set_variable_non_membership_is_false() -> None:
    assert ms('extras == "cpu"').is_empty()
    assert ms('extra < "cpu"').is_empty()


def test_dependency_groups_membership() -> None:
    marker = ms('"dev" in dependency_groups')
    assert marker.evaluate({"dependency_groups": {"dev"}})
    assert not marker.evaluate({"dependency_groups": {"docs"}})


def test_pep685_extra_normalisation() -> None:
    assert ms('extra == "Foo.Bar"').equivalent(ms('extra == "foo-bar"'))


# --------------------------------------------------------------- evaluate


def test_evaluate_extras_as_set_and_string() -> None:
    marker = ms('extra == "cpu"')
    assert marker.evaluate({"extra": {"cpu"}})
    assert not marker.evaluate({"extra": {"gpu"}})
    assert not marker.evaluate({"extra": set()})


def test_evaluate_derive_mm_boundaries() -> None:
    marker = ms('python_version == "3.9"')
    assert marker.evaluate({"python_full_version": "3.9.7"})
    assert not marker.evaluate({"python_full_version": "3.10.0"})
    # single-segment and non-version full versions still evaluate.
    assert not marker.evaluate({"python_full_version": "3"})
    assert not marker.evaluate({"python_full_version": "not-a-version"})


def test_evaluate_python_version_only_env() -> None:
    # An env supplying only python_version (the written variable) evaluates.
    marker = ms('python_version == "3.9"')
    assert marker.evaluate({"python_version": "3.9"})
    assert not marker.evaluate({"python_version": "3.10"})


def test_evaluate_python_version_prefers_full_version() -> None:
    # With both keys present, A1 reads python_full_version, so a disagreeing
    # python_version key is not consulted.
    marker = ms('python_version == "3.9"')
    assert not marker.evaluate(
        {"python_full_version": "3.10.1", "python_version": "3.9"}
    )


def test_evaluate_set_variable_as_string() -> None:
    # A set variable passed as a str is one name, matching restrict().
    marker = ms('extra == "cpu"')
    assert marker.evaluate({"extra": "cpu"})
    assert not marker.evaluate({"extra": "gpu"})


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ('sys_platform == "linux"', "sys_platform"),
        ('"cpu" in extras', "extras"),
        ('"x86" in platform_machine', "platform_machine"),
        ('python_version == "3.9"', "python_version"),
    ],
)
def test_evaluate_missing_variable_raises(text: str, key: str) -> None:
    # A missing referenced variable raises on every axis, matching packaging;
    # UndefinedEnvironmentName subclasses KeyError, so the error is clear and
    # sets are not silently defaulted to empty.
    marker = ms(text)
    with pytest.raises(UndefinedEnvironmentName, match=key):
        marker.evaluate({})
    assert isinstance(UndefinedEnvironmentName(key), KeyError)


def test_platform_version_dispatches_as_string() -> None:
    # platform_version is a plain string in packaging, not a version twin.
    text = 'platform_version == "#1 SMP"'
    marker = ms(text)
    for value in ("#1 SMP", "other"):
        env = {"platform_version": value}
        assert marker.evaluate(env) == Marker(text).evaluate(env)
    assert not marker.is_empty()


def test_substring_and_value_on_one_string_variable_decide_together() -> None:
    """A string variable carries both readings of itself on one axis.

    ``literal in point`` is decidable at a point, so a substring test on a
    variable the tree also compares by value is read on that value rather than
    on a boolean of its own.
    """
    assert ms('os_name == "posix" and "posix" not in os_name').is_empty()
    assert ms('sys_platform == "linux" and "win" in sys_platform').is_empty()
    assert ms('os_name == "posix"').is_subset(ms('"posix" in os_name'))


def test_substring_points_reach_past_the_value_literals() -> None:
    """The axis keeps the strings that embed a literal without equalling one.

    ``!= "posix"`` holds at every string but one, and ``"posix" in os_name``
    at every string embedding it, so the two meet. Deciding the substring test
    only on the value literals would miss that and read the set as empty.
    """
    wider = ms('os_name != "posix" and "posix" in os_name')

    assert not wider.is_empty()

    env = wider.witness()
    assert env is not None
    assert wider.evaluate(env)


def test_substring_points_reach_every_realisable_combination() -> None:
    """Two independent substrings can hold at once, and either alone.

    A point is minted for each subset of the axis's substring literals, so a
    combination is ruled out only when one literal embeds another.
    """
    both = ms('"aa" in platform_version and "bb" in platform_version')
    assert not both.is_empty()
    assert not both.is_subset(ms('platform_version == "aabb"'))

    assert ms('"aa" in platform_version and "a" not in platform_version').is_empty()


def test_substring_points_avoid_a_literal_holding_the_first_separator() -> None:
    """The joined point uses a character the axis's own literals do not.

    "ab" carries both substrings and neither excluded one, and only a joined
    point carries two at once. A separator one literal already holds would put
    that literal into every joined point and lose the combination.
    """
    marker = ms(
        '"a" in platform_version and "b" in platform_version'
        ' and "!" not in platform_version'
    )

    assert not marker.is_empty()

    env = marker.witness()
    assert env is not None
    assert marker.evaluate(env)


def test_substring_on_a_version_variable_stays_a_free_boolean() -> None:
    """A version axis keeps the opaque reading rather than folding the two.

    No finite pool covers the values that embed an arbitrary literal, so
    folding would decide empty where a real value satisfies both. The price is
    that a genuine contradiction reads as inhabited.
    """
    # A contradiction: every PEP 440 version equal to 3.9 writes the digit 9.
    assert not ms('python_version == "3.9" and "9" not in python_version').is_empty()

    # Not a contradiction: a local segment satisfies both, and keeping the axis
    # opaque is what lets the algebra see it. No pool of versions built from
    # "6" and "zq" would mint this point.
    both = ms('platform_release == "6" and "zq" in platform_release')
    assert not both.is_empty()
    assert both.evaluate({"platform_release": "6.0+zq"})


def test_implementation_version_is_version_or_string_twin() -> None:
    # implementation_version dispatches as a version yet may hold an arbitrary
    # string (like platform_release), so a non-version literal stays realisable
    # and the decisions must not treat it as empty or the negation as tautology.
    ver = ms('implementation_version == "foo"')
    assert not ver.is_empty()
    assert ver.witness() is not None
    assert not ms('implementation_version != "foo"').is_full()
    assert not ms('"foo" in implementation_version').is_empty()
    for text in (
        'implementation_version == "foo"',
        'implementation_version >= "3.9"',
        'implementation_version != "foo"',
    ):
        marker = ms(text)
        for value in ("foo", "3.9.0", "pypy"):
            env = {"implementation_version": value}
            assert marker.evaluate(env) == Marker(text).evaluate(env), (text, value)


def test_epoch_version_decisions_are_sound() -> None:
    # An epoch full version truncates below its full ordering, so the pool must
    # keep an epoch-bearing representative or the decisions turn unsound.
    high = ms('python_full_version >= "3.14"')
    low_mm = ms('python_version < "3.14"')
    assert not high.is_disjoint(low_mm)
    assert not high.is_subset(ms('python_version >= "3.10"'))
    assert not ms(
        'python_full_version >= "3.14" and python_version == "3.9"'
    ).is_empty()


def test_epoch_literal_pure_axis_decisions_are_sound() -> None:
    # A pure python_full_version axis carrying an epoch literal: the neighbour
    # bumps must sit in the literal's own epoch, or the band above it (1!4.0,
    # 2!0) loses its representative and the decisions turn silently unsound.
    above = ms('python_full_version > "1!3.9"')
    band = ms('python_full_version ~= "1!3.9"')
    at_or_above = ms('python_full_version >= "1!3.9"')
    assert not above.is_subset(band)
    assert not at_or_above.equivalent(band)
    residue = above & band.complement()
    assert not residue.is_empty()
    assert residue.evaluate({"python_full_version": "2!0"})


def test_epoch_gap_between_literals_is_sound() -> None:
    # Mixing python_version (major.minor) with a python_full_version epoch-2
    # literal leaves epoch 1 with no literal of its own; the pool must still mint
    # a representative there or the satisfying epoch-1 band goes invisible.
    text = (
        'python_full_version >= "3.14" and python_full_version < "2!0" '
        'and python_version < "3.10"'
    )
    marker = ms(text)
    env = {"python_full_version": "1!3.9", "python_version": "3.9"}
    assert not marker.is_empty()
    assert marker.evaluate(env)
    assert Marker(text).evaluate(env)
    taut_text = (
        'python_full_version >= "2!0" or python_version != "3.10" '
        'or python_full_version <= "3.14.dev0"'
    )
    assert not ms(taut_text).is_full()


def test_membership_substrings_mint_epoch_twins() -> None:
    # A membership literal's version substrings feed the epoch-elevating pool, so
    # the axis carries a twin for every epoch band and the decisions stay sound.
    marker = ms('python_full_version >= "3.14" and python_version in "3.9 3.10"')
    env = {"python_full_version": "1!3.9", "python_version": "3.9"}
    assert not marker.is_empty()
    assert marker.evaluate(env)
    assert Marker(
        'python_full_version >= "3.14" and python_version in "3.9 3.10"'
    ).evaluate(env)


def test_membership_epoch_twin_witnesses_non_implication() -> None:
    # ~= "1!3.9" holds at 1!3.12, whose major.minor is 3.12, outside the set; the
    # epoch twin of the membership substrings must exist or the implication is
    # wrongly proven.
    compat = ms('python_full_version ~= "1!3.9"')
    member = ms('python_version in "3.9 3.10 3.11"')
    assert not compat.is_subset(member)


# --------------------------------------------------------------- restrict


def test_restrict_residual_and_full() -> None:
    marker = ms('python_version >= "3.9" and sys_platform == "linux"')
    residual = marker.restrict({"sys_platform": "linux"})
    assert residual.equivalent(ms('python_version >= "3.9"'))
    dropped = marker.restrict({"sys_platform": "win32"})
    assert dropped.is_empty()
    full = marker.restrict({"python_full_version": "3.10.0", "sys_platform": "linux"})
    assert full.is_full()


def test_restrict_python_version_key_variants() -> None:
    marker = ms('python_version >= "3.10"')
    assert marker.restrict({"python_full_version": "3.10.1"}).is_full()
    assert marker.restrict({"python_version": "3.9"}).is_empty()
    assert marker.restrict({"sys_platform": "linux"}).equivalent(marker)


def test_restrict_extras_and_contains() -> None:
    extras = ms('"cpu" in extras')
    assert extras.restrict({"extras": {"cpu"}}).is_full()
    assert extras.restrict({"extras": set()}).is_empty()
    assert extras.restrict({"sys_platform": "linux"}).equivalent(extras)
    contains = ms('"x86" in platform_machine')
    assert contains.restrict({"platform_machine": "x86_64"}).is_full()
    assert contains.restrict({"platform_machine": "arm64"}).is_empty()
    assert contains.restrict({"sys_platform": "linux"}).equivalent(contains)


def test_restrict_extra_as_string_value() -> None:
    marker = ms('extra == "cpu"')
    assert marker.restrict({"extra": "cpu"}).is_full()
    assert marker.restrict({"extra": ""}).is_empty()


def test_restrict_normalises_the_set_axis() -> None:
    # A restriction value goes through the same PEP 685 normalisation as an
    # evaluation environment, so "A_B" pins the atom for "a-b".
    assert ms('extra == "a-b"').restrict({"extra": frozenset({"A_B"})}).is_full()

    # A string is one name and not a haystack: "pu" is not the extra "cpu".
    assert ms('extra == "pu"').restrict({"extra": "cpu"}).is_empty()


def test_restrict_leaves_an_unprovided_variable_alone() -> None:
    marker = ms('python_version >= "3.9" and sys_platform == "linux"')

    assert marker.restrict({"python_version": "3.10"}).equivalent(
        ms('sys_platform == "linux"')
    )
    assert marker.restrict(
        {"python_full_version": "3.10.0", "sys_platform": "linux"}
    ).is_full()


def test_restrict_constant_set() -> None:
    assert MarkerSet.full().restrict({"sys_platform": "linux"}).is_full()


def test_restrict_through_complement() -> None:
    marker = ms('sys_platform == "linux"').complement()
    assert marker.restrict({"sys_platform": "win32"}).is_full()
    assert marker.restrict({"sys_platform": "linux"}).is_empty()


# ------------------------------------------------------ variables / literals


def test_variable_names_accepts_a_marker_instance() -> None:
    assert variable_names(Marker('sys_platform == "linux"')) == frozenset(
        {"sys_platform"}
    )


def test_variable_names_rejects_a_non_marker_argument() -> None:
    with pytest.raises(TypeError):
        variable_names(42)  # type: ignore[arg-type]


def test_variable_names_of_empty_input_is_empty() -> None:
    assert variable_names("") == frozenset()
    assert variable_names("   ") == frozenset()


def test_variable_names_of_an_invalid_marker_raises() -> None:
    with pytest.raises(InvalidMarker):
        variable_names("this is not a marker")


def test_variable_names_walks_nested_and_or_groups() -> None:
    names = variable_names(
        'python_full_version === "3.13.5"'
        ' and (sys_platform == "linux" or os_name == "posix")'
    )
    assert names == frozenset({"python_full_version", "sys_platform", "os_name"})


def test_variable_names_collects_both_sides_of_a_variable_comparison() -> None:
    assert variable_names("python_version == python_full_version") == frozenset(
        {"python_version", "python_full_version"}
    )


def test_variable_names_of_a_literal_only_comparison_is_empty() -> None:
    assert variable_names('"linux" == "linux"') == frozenset()


def test_variable_names_collects_a_literal_that_names_a_variable() -> None:
    assert variable_names('"3.9" == "python_version"') == frozenset({"python_version"})


def test_set_memberships() -> None:
    marker = ms('"cpu" in extras and "gpu" not in extras and sys_platform == "linux"')
    assert marker.set_memberships() == frozenset({("extras", "cpu"), ("extras", "gpu")})
    assert ms('sys_platform == "linux"').set_memberships() == frozenset()


# ---------------------------------------------------------------- witness


def test_witness_of_empty_is_none() -> None:
    assert MarkerSet.empty().witness() is None
    assert ms('python_full_version < "0"').witness() is None


def test_witness_of_value_set_satisfies() -> None:
    marker = ms('python_full_version >= "3.9" and sys_platform == "linux"')
    env = marker.witness()
    assert env is not None
    assert marker.evaluate(env)


def test_witness_of_tautology() -> None:
    env = MarkerSet.full().witness()
    assert env == {}


def test_witness_of_extras() -> None:
    marker = ms('"cpu" in extras and "gpu" not in extras')
    env = marker.witness()
    assert env is not None
    assert marker.evaluate(env)


def test_witness_of_contains() -> None:
    marker = ms('"x86" in platform_machine')
    env = marker.witness()
    assert env is not None
    assert marker.evaluate(env)


def test_string_axis_decides_a_substring_contradiction() -> None:
    # A string variable's value axis carries both readings, so the pair is
    # decided rather than over-approximated.
    assert ms('platform_machine == "arm64" and "x86" in platform_machine').is_empty()


def test_witness_none_for_opaque_over_approximation() -> None:
    # A twin keeps the opaque reading of a substring test, so the set is not
    # is_empty although no string equal to "pypy" carries "x86".
    marker = ms('implementation_version == "pypy" and "x86" in implementation_version')
    assert not marker.is_empty()
    assert marker.witness() is None


def test_witness_none_for_python_version_alias_conflict() -> None:
    # A python_version substring and a python_version value constraint share the
    # python_full_version axis under A1, so the materialised major.minor cannot
    # also carry the substring; witness reports no realisable environment rather
    # than an inconsistent python_version / python_full_version pair.
    conflict = ms('"9" in python_version and python_version == "3.10"')
    assert not conflict.is_empty()
    assert conflict.witness() is None
    # Against a full-version bound the realisable representatives lie at 3.9 or
    # 3.19, off the cell boundary the decomposition mints, so witness stays
    # incomplete here too.
    spanning = ms('"9" in python_version and python_full_version >= "3.0"')
    assert spanning.witness() is None


def test_witness_of_python_version_contains() -> None:
    marker = ms('"9" in python_version')
    env = marker.witness()
    assert env is not None
    assert marker.evaluate(env)


def test_witness_of_python_version_value_satisfies_packaging() -> None:
    marker = ms('python_version == "3.9"')
    env = marker.witness()
    assert env is not None
    assert marker.evaluate(env)
    assert Marker('python_version == "3.9"').evaluate(env)


# ------------------------------------------------------------ serialisation


def test_to_marker_string_none_for_tautology() -> None:
    assert MarkerSet.full().to_marker_string() is None
    assert ms('os_name == "a" or os_name != "a"').to_marker_string() is None


def test_to_marker_string_raises_for_empty() -> None:
    with pytest.raises(UnserializableMarkerSet):
        MarkerSet.empty().to_marker_string()
    with pytest.raises(UnserializableMarkerSet):
        ms('python_full_version < "0"').to_marker_string()


@pytest.mark.parametrize(
    "text",
    [
        'sys_platform == "linux"',
        'sys_platform != "linux"',
        'python_full_version == "3.9"',
        'python_full_version != "3.9"',
        '"cpu" in extras',
        '"cpu" not in extras',
        'extra == "cpu"',
        'extra != "cpu"',
        '"x86" in platform_machine',
        '"x86" not in platform_machine',
        'sys_platform in "linuxx"',
        '"3.9" == python_full_version',
        'sys_platform == "linux" and python_full_version >= "3.9"',
        'sys_platform == "linux" or os_name == "posix"',
    ],
)
def test_round_trip_is_equivalent(text: str) -> None:
    marker = ms(text)
    result = marker.to_marker_string()
    assert result is not None
    assert marker.equivalent(ms(result))


@pytest.mark.parametrize(
    "text",
    [
        "'a\"b' == sys_platform",
        "'a\"b' in sys_platform",
        '"a\'b" == sys_platform',
    ],
)
def test_embedded_quote_literal_round_trips(text: str) -> None:
    # A literal carrying a quote must be spelled with the other quote style, not
    # emit a malformed string that leaks InvalidMarker from the re-parse guard.
    marker = ms(text)
    result = marker.to_marker_string()
    assert result is not None
    assert marker.equivalent(ms(result))


def test_complement_round_trips_where_spellable() -> None:
    marker = ms('sys_platform == "linux"').complement()
    text = marker.to_marker_string()
    assert text is not None
    assert marker.equivalent(ms(text))


def test_complement_of_and_or_serialises() -> None:
    conj = (ms('sys_platform == "linux"') & ms('os_name == "posix"')).complement()
    disj = (ms('sys_platform == "linux"') | ms('os_name == "posix"')).complement()
    for marker in (conj, disj):
        text = marker.to_marker_string()
        assert text is not None
        assert marker.equivalent(ms(text))


def test_double_negation_serialises() -> None:
    inner = ms('os_name == "posix"').complement()
    marker = (ms('sys_platform == "linux"') & inner).complement()
    text = marker.to_marker_string()
    assert text is not None
    assert marker.equivalent(ms(text))


def test_open_band_between_adjacent_literals_is_sound() -> None:
    # An exclusive comparison excludes its own bound's post, pre, dev and local
    # variants, so a band open at both ends admits only a version whose release
    # differs from both. The pool seeds one; before it did not and the band
    # decided empty.
    for text, value in (
        ('platform_release > "6" and platform_release < "6.1"', "6.0.1"),
        ('python_full_version > "3" and python_full_version < "3.1"', "3.0.1"),
        ('implementation_version > "9" and implementation_version < "9.1"', "9.0.1"),
    ):
        band = ms(text)
        assert not band.is_empty(), text
        assert band.evaluate(_env_for(text, value)), text

        env = band.witness()
        assert env is not None, text
        assert Marker(text).evaluate(env), text


def _env_for(text: str, value: str) -> dict[str, str]:
    """The environment pinning whichever variable ``text`` names to ``value``."""
    name = next(iter(variable_names(text)))
    env = {name: value}
    if name == "python_full_version":
        env["python_version"] = ".".join(value.split(".")[:2])
    return env


def test_open_band_predicates_and_simplify_stay_sound() -> None:
    # Every predicate reduces to is_empty, so each one inherits the band fix.
    for text in (
        'platform_release > "6" and platform_release < "6.1"',
        'python_full_version > "3" and python_full_version < "3.1"',
    ):
        band = ms(text)

        assert not band.is_disjoint(MarkerSet.full()), text
        assert not band.is_subset(ms('sys_platform == "win32"')), text
        assert not band.equivalent(MarkerSet.empty()), text
        assert not band.simplify(within=MarkerSet.full()).is_empty(), text


def test_string_axis_decides_a_substring_against_its_own_value() -> None:
    # On a string variable the substring test and the value comparison share one
    # axis, so the pair is decided rather than read as inhabited.
    assert ms('os_name == "posix" and "posix" not in os_name').is_empty()
    assert ms('sys_platform == "linux" and "win" in sys_platform').is_empty()
    assert ms('os_name == "posix"').is_subset(ms('"posix" in os_name'))

    # And a pair that is genuinely inhabited still reads as inhabited.
    both = ms('"lin" in sys_platform and "ux" in sys_platform')
    assert not both.is_empty()
    assert both.evaluate({"sys_platform": "linux"})


def test_many_substring_literals_on_one_axis_refuse_loudly() -> None:
    # Folding mints a point per subset, so an axis carrying many substring tests
    # refuses rather than answering. Refusing is the contract; the
    # over-approximation it replaces was an answer.
    text = " and ".join(f'"{chr(ord("a") + i) * 2}" in os_name' for i in range(17))

    with pytest.raises(IntractableMarkerSet, match="substring subsets over 17"):
        ms(text).is_empty()


def test_unserializable_ordered_version_complement() -> None:
    # The message quotes the clause as written: python_version lowers onto the
    # python_full_version axis and must not surface as it.
    with pytest.raises(
        UnserializableMarkerSet,
        match=r'no marker string spells the complement of python_version >= "3\.9"',
    ):
        ms('python_version >= "3.9"').complement().to_marker_string()

    with pytest.raises(UnserializableMarkerSet):
        ms('python_full_version >= "3.9"').complement().to_marker_string()


def test_release_between_reads_both_epochs_and_widths() -> None:
    # Two branches the marker-level tests do not separate: the padding width is
    # vhigh's only inside one epoch, and the prefix is vlow's epoch, not vhigh's.
    between = _markersets._release_between

    assert between(Version("1!3"), Version("1!3.1")) == "1!3.0.1"
    assert between(Version("3"), Version("3.1.1.1")) == "3.0.0.0.1"

    # Across an epoch boundary vhigh's release does not bound the candidate, so
    # padding to it would mint a needlessly deep point.
    assert between(Version("3"), Version("1!3.1.1.1")) == "3.1"


def test_unserializable_swapped_version_complement() -> None:
    # A swapped atom builds its specifier from the environment value, so it
    # compares as a version wherever that value parses as one and through the
    # string table on the rest. No single flipped atom is its complement, and
    # `<` and `>` are not constant there either.
    for text in (
        '"pypy" == implementation_version',
        '"pypy" != implementation_version',
        '"3.9+local" < python_full_version',
        '"3.9" >= platform_release',
    ):
        with pytest.raises(
            UnserializableMarkerSet, match="no marker string spells the complement"
        ):
            ms(text).complement().to_marker_string()

    # A swapped atom on a string variable still complements: that variable never
    # dispatches as a version, so the table's reading is the whole story.
    assert ms('"linux" == sys_platform').complement().to_marker_string() == (
        '"linux" != sys_platform'
    )


def test_unserializable_twin_equality_complement() -> None:
    with pytest.raises(
        UnserializableMarkerSet,
        match=r'no marker string spells the complement of platform_release == "6\.6"',
    ):
        ms('platform_release == "6.6"').complement().to_marker_string()


def test_unserializable_degraded_equality_complement() -> None:
    # A literal the ordered specifier rejects sends >= and <= down the string
    # operator table, where they mean exact equality. Spelling that complement
    # as != would re-dispatch as a version, so it has no marker string.
    for text in (
        'python_full_version >= "3.9+local"',
        'platform_release <= "6.6+x"',
    ):
        with pytest.raises(
            UnserializableMarkerSet,
            match="no marker string spells the complement of",
        ):
            ms(text).complement().to_marker_string()

    # A wildcard literal has no realisable value, so its set reads empty and
    # to_marker_string answers None for the complement before serialising it.
    assert repr(~ms('python_full_version <= "3.*"')) == "<MarkerSet 'unrepresentable'>"


def test_string_table_equality_complement_serialises() -> None:
    # On a string variable the same degradation is spellable: >= means equality
    # and != is its complement on the same table.
    marker = ms('os_name >= "posix"').complement()
    text = marker.to_marker_string()
    assert text == 'os_name != "posix"'
    assert marker.equivalent(ms(text))


def test_repr_of_a_constant_complement_is_total() -> None:
    # < and > are constant-false on the string operator table, which a
    # version-dispatch variable falls onto when its literal builds no specifier.
    # The complement is then the universe, which has no atom to render.
    assert repr(~ms('sys_platform < "linux"')) == "<MarkerSet 'universe'>"
    assert repr(~ms('python_full_version < "3.*"')) == "<MarkerSet 'universe'>"
    assert repr(~ms('implementation_version > "pypy"')) == "<MarkerSet 'universe'>"


def test_repr_summarises_without_leaking_the_tree() -> None:
    # repr renders a marker-string summary and never the private op-tree: the
    # constant sets read as words, a plain set as its marker string, and a
    # grammar-inexpressible complement as a placeholder.
    assert (
        repr(ms('sys_platform == "linux"')) == "<MarkerSet 'sys_platform == \"linux\"'>"
    )
    assert repr(MarkerSet.full()) == "<MarkerSet 'universe'>"
    assert repr(MarkerSet.empty()) == "<MarkerSet 'empty'>"
    assert repr(~ms('python_full_version >= "3.10"')) == "<MarkerSet 'unrepresentable'>"
    for rendered in (
        repr(ms('sys_platform == "linux"')),
        repr(~ms('python_full_version >= "3.10"')),
    ):
        assert "AtomLeaf" not in rendered
        assert "NotNode" not in rendered


# ------------------------------------------------------------------ guards


def test_guard_set_powerset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(markersets, "_MAX_CELLS", 1000)
    marker = ms(" and ".join(f'extra == "pkg{i}"' for i in range(20)))
    with pytest.raises(IntractableMarkerSet):
        marker.is_empty()


def test_guard_substring_enumeration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(markersets, "_MAX_CELLS", 3)
    marker = ms('sys_platform in "abcdefghij"')
    with pytest.raises(IntractableMarkerSet):
        marker.is_empty()


def test_guard_substring_low_entropy(monkeypatch: pytest.MonkeyPatch) -> None:
    # A repeated-character literal has few distinct substrings but a quadratic
    # index loop; the guard bounds the loop work, so it fires here.
    monkeypatch.setattr(markersets, "_MAX_CELLS", 100)
    marker = ms('sys_platform in "' + "a" * 50 + '"')
    with pytest.raises(IntractableMarkerSet):
        marker.is_empty()


def test_guard_version_pool_epoch_elevation(monkeypatch: pytest.MonkeyPatch) -> None:
    # Mixing python_version with many distinct-epoch python_full_version atoms
    # triggers epoch elevation, whose product is bounded as it is generated.
    monkeypatch.setattr(markersets, "_MAX_CELLS", 1000)
    epochs = " and ".join(f'python_full_version == "{e}!2.0"' for e in range(1, 16))
    marker = ms(f'python_version == "3.9" and {epochs}')
    with pytest.raises(IntractableMarkerSet):
        marker.is_empty()


def test_guard_repeated_clause_tree_walk() -> None:
    # Repeating one clause inflates the op-tree walked per cell without adding
    # distinct atoms or cells, so the leaf-occurrence guard fires where a
    # distinct-atom count would not.
    axes = " and ".join(
        ['"a0" in python_version', '"a0" not in python_version']
        + [f'"a{i}" in python_version' for i in range(1, 12)]
    )
    marker = ms(" or ".join(f"({axes})" for _ in range(8)))
    with pytest.raises(IntractableMarkerSet):
        marker.is_empty()


def test_guard_substring_subsets() -> None:
    # A string axis mints one point per subset of its substring literals, so
    # the subset count is guarded before the points are built.
    text = " and ".join(f'"lit{i}" in sys_platform' for i in range(17))
    with pytest.raises(IntractableMarkerSet, match="substring subsets over 17"):
        ms(text).is_empty()


def test_guard_value_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(markersets, "_MAX_CELLS", 1)
    marker = ms('python_full_version == "3.9"')
    with pytest.raises(IntractableMarkerSet):
        marker.is_empty()


def test_guard_cell_product(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(markersets, "_MAX_CELLS", 2)
    marker = ms(
        'sys_platform == "linux" and os_name == "posix" '
        'and platform_machine == "x86_64"'
    )
    with pytest.raises(IntractableMarkerSet):
        marker.is_empty()


def test_guard_axis_work(monkeypatch: pytest.MonkeyPatch) -> None:
    # Many distinct atoms on one axis: the point count stays under the cap but
    # points x atoms does not, so the guard fires instead of doing O(N^2) work.
    monkeypatch.setattr(markersets, "_MAX_CELLS", 100)
    marker = ms(" or ".join(f'sys_platform == "p{i}"' for i in range(60)))
    with pytest.raises(IntractableMarkerSet):
        marker.is_empty()


def test_guard_set_axis_work(monkeypatch: pytest.MonkeyPatch) -> None:
    # A set axis clears the powerset cap (two subsets) yet its subsets x atoms
    # product does not, so the per-axis reduce guard fires.
    monkeypatch.setattr(markersets, "_MAX_CELLS", 3)
    marker = ms('extra == "a" and extra != "a"')
    with pytest.raises(IntractableMarkerSet):
        marker.is_empty()


def test_guard_version_axis_literal_count(monkeypatch: pytest.MonkeyPatch) -> None:
    # Many distinct version literals already exceed the cap; the axis is
    # rejected up front, before the neighbour pool is materialised.
    monkeypatch.setattr(markersets, "_MAX_CELLS", 100)
    marker = ms(" or ".join(f'python_full_version == "{i}.0"' for i in range(200)))
    with pytest.raises(IntractableMarkerSet):
        marker.is_empty()


def test_guard_does_not_fire_under_default() -> None:
    marker = ms(" and ".join(f'extra == "pkg{i}"' for i in range(10)))
    assert not marker.is_empty()


@pytest.mark.parametrize(
    "text",
    [
        'python_full_version >= "1.' + "9" * 5000 + '"',
        'implementation_version >= "1.' + "9" * 5000 + '"',
        'python_full_version == "' + "9" * 5000 + '!1.0"',
        'python_full_version in "' + "9" * 5000 + '"',
    ],
)
def test_guard_oversized_numeric_literal(text: str) -> None:
    # A version literal whose numeric component overruns the interpreter's
    # int-from-string limit crashes packaging's Version with a bare ValueError;
    # the decision procedures report the algebra's bounded failure instead.
    marker = ms(text)
    with pytest.raises(IntractableMarkerSet):
        marker.is_empty()


@pytest.mark.parametrize("op", ["<", ">=", "=="])
def test_evaluate_rejects_oversized_literal(op: str) -> None:
    marker = ms(f'python_full_version {op} "' + "9" * 5000 + '"')
    with pytest.raises(IntractableMarkerSet):
        marker.evaluate({"python_full_version": "3.9"})


@pytest.mark.parametrize("op", ["<", ">=", "=="])
def test_restrict_rejects_oversized_literal(op: str) -> None:
    marker = ms(f'python_full_version {op} "' + "9" * 5000 + '"')
    with pytest.raises(IntractableMarkerSet):
        marker.restrict({"python_full_version": "3.9"})


def test_restrict_keeps_oversized_literal_when_unprovided() -> None:
    marker = ms(
        'python_full_version < "' + "9" * 5000 + '" and sys_platform == "linux"'
    )
    residual = marker.restrict({"sys_platform": "linux"})
    assert isinstance(residual, MarkerSet)


def test_string_axis_ignores_oversized_numeric_literal() -> None:
    # A string variable never parses its literal as a version, so a long numeric
    # literal is an ordinary string and the guard must not fire.
    assert not ms('sys_platform == "' + "9" * 5000 + '"').is_empty()


@pytest.mark.parametrize("op", ["<", ">=", "=="])
def test_evaluate_rejects_oversized_value(op: str) -> None:
    marker = ms(f'python_full_version {op} "3.9"')
    with pytest.raises(IntractableMarkerSet):
        marker.evaluate({"python_full_version": "9" * 5000})


@pytest.mark.parametrize("op", ["<", ">=", "=="])
def test_restrict_rejects_oversized_value(op: str) -> None:
    marker = ms(f'python_full_version {op} "3.9"')
    with pytest.raises(IntractableMarkerSet):
        marker.restrict({"python_full_version": "9" * 5000})


def test_string_axis_ignores_oversized_numeric_value() -> None:
    # A string variable never parses its env value as a version, so an oversized
    # numeric value is an ordinary string and the guard must not fire.
    assert ms('sys_platform == "linux"').evaluate({"sys_platform": "9" * 5000}) is False


def test_oversized_literal_allowed_when_int_limit_disabled() -> None:
    # A zero int-string limit disables the interpreter's overflow check, so the
    # literal parses and the guard stands down.
    lit = "1." + "9" * 5000
    original = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(0)
    try:
        assert isinstance(ms(f'python_full_version >= "{lit}"').is_empty(), bool)
    finally:
        sys.set_int_max_str_digits(original)


def test_apply_memo_reuses_a_repeated_comparison() -> None:
    assert _markersets._apply("3.11.4", ">=", "3.9", key="python_full_version") is True

    hits = _markersets._apply_memoised.cache_info().hits
    assert _markersets._apply("3.11.4", ">=", "3.9", key="python_full_version") is True
    assert _markersets._apply_memoised.cache_info().hits == hits + 1


def test_apply_memo_misses_when_int_limit_changes() -> None:
    # A version comparison parses both operands, so a result memoised under one
    # int-string limit must not be replayed under a limit that forbids the parse.
    lit = "1." + "9" * 5000
    original = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(0)
    try:
        assert _markersets._apply(lit, ">=", "3.9", key="python_full_version") is False
    finally:
        sys.set_int_max_str_digits(original)

    with pytest.raises(ValueError, match="limit"):
        _markersets._apply(lit, ">=", "3.9", key="python_full_version")


def test_mint_overflow_at_parse_limit_reports_complexity() -> None:
    # A literal whose numeric run is exactly the parse-limit width leaves no
    # room for neighbour minting to increment a component, so decomposition
    # reports the algebra's bounded failure rather than a bare ValueError.
    original = sys.get_int_max_str_digits()
    limit = 640
    sys.set_int_max_str_digits(limit)
    try:
        marker = ms('python_full_version > "' + "9" * limit + '"')
        with pytest.raises(IntractableMarkerSet):
            marker.is_empty()
        with pytest.raises(IntractableMarkerSet):
            marker.witness()
    finally:
        sys.set_int_max_str_digits(original)


def test_warm_version_pool_does_not_bypass_the_oversized_literal_guard() -> None:
    # A store carries its parsed versions across decisions, so a parse taken under
    # a disabled int-string limit outlives that limit. A decision under a limit the
    # literal overruns has to reach the guard rather than the store's copy.
    literal = "1." + "9" * 700
    store = markersets.DecisionStore()
    original = sys.get_int_max_str_digits()

    sys.set_int_max_str_digits(0)
    try:
        assert ms(f'python_full_version >= "{literal}"').is_empty(store=store) is False
    finally:
        sys.set_int_max_str_digits(original)

    assert literal in store.versions

    sys.set_int_max_str_digits(640)
    try:
        # Both parse-limit guards reject this literal, so the match is on the
        # oversize guard's own message to pin which one fired.
        with pytest.raises(
            IntractableMarkerSet, match="exceeds the 640-digit parse limit"
        ):
            ms(f'python_full_version < "{literal}"').is_empty(store=store)
    finally:
        sys.set_int_max_str_digits(original)


def _nested_alternating(depth: int) -> MarkerSet:
    # Algebra assembles the op-tree without recursing, so a tree far deeper than
    # the stack is built at any recursion limit; only the walk that decides it
    # recurses.
    left = ms('sys_platform == "linux"')
    right = ms('os_name == "posix"')
    deep = ms('python_version == "3.9"')
    for i in range(depth):
        deep = (left & deep) if i % 2 == 0 else (right | deep)
    return deep


def test_deep_nesting_decision_reports_complexity() -> None:
    deep = _nested_alternating(1200)
    original = sys.getrecursionlimit()
    try:
        # Bound the walk to a fixed headroom over the current stack, so the guard
        # fires below the interpreter limit whatever the ambient limit is.
        sys.setrecursionlimit(len(traceback.extract_stack()) + 300)
        shallow = ms('os_name == "posix"')
        env = {"sys_platform": "linux", "os_name": "posix", "extra": frozenset()}
        decisions = (
            deep.is_empty,
            deep.is_full,
            deep.to_marker_string,
            deep.witness,
            lambda: deep.is_disjoint(shallow),
            lambda: deep.is_subset(shallow),
            lambda: deep.is_superset(shallow),
            lambda: deep.equivalent(shallow),
            lambda: deep.equivalent_within(shallow, MarkerSet.full()),
            lambda: deep.restrict(env),
            lambda: deep.evaluate(env),
            deep.set_memberships,
            lambda: deep.simplify(within=MarkerSet.full()),
        )
        for decide in decisions:
            with pytest.raises(IntractableMarkerSet):
                decide()

        # The two dunders stay undecorated, so they report the depth the way
        # CPython does for any deep structure.
        for dunder in (lambda: deep == shallow, lambda: hash(deep)):
            with pytest.raises(RecursionError):
                dunder()
    finally:
        sys.setrecursionlimit(original)


def test_repr_past_the_stack_is_total() -> None:
    # repr answers where every decision procedure above reports the depth.
    deep = _nested_alternating(1200)
    original = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(len(traceback.extract_stack()) + 300)
        assert repr(deep) == "<MarkerSet 'too deeply nested'>"
    finally:
        sys.setrecursionlimit(original)


def test_deep_nesting_construction_reports_complexity() -> None:
    marker = 'python_version == "3.9"'
    for i in range(1200):
        atom = 'sys_platform == "linux"' if i % 2 == 0 else 'os_name == "posix"'
        op = "and" if i % 2 == 0 else "or"
        marker = f"{atom} {op} ({marker})"
    original = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(len(traceback.extract_stack()) + 300)
        with pytest.raises(IntractableMarkerSet):
            MarkerSet.from_marker(marker)
    finally:
        sys.setrecursionlimit(original)


# -------------------------------------------------- branch-completeness edges


def test_other_cell_survives_literal_collision() -> None:
    # The OTHER-cell representative is distinct from every literal on the axis,
    # so a string field keeps its two cells whatever the literal spells.
    for literal in ("zzz-no-literal-equals-this", "z" * 40, ""):
        marker = ms(f'sys_platform == "{literal}"')
        assert not marker.is_full()
        assert not marker.evaluate({"sys_platform": "linux"})
        assert marker.evaluate({"sys_platform": literal})


def test_non_version_literal_on_version_axis() -> None:
    # A non-version literal produces no version neighbours and is filtered from
    # the pure-version candidate pool.
    assert ms('python_full_version == "abc"').is_empty()


def test_version_membership_substrings_filtered() -> None:
    # Substrings of the haystack that are not versions are dropped on a pure
    # version axis.
    marker = ms('python_full_version in "3.10"')
    assert marker.evaluate({"python_full_version": "3"})
    assert not marker.evaluate({"python_full_version": "4"})


def test_version_axis_with_two_literals() -> None:
    marker = ms('python_full_version >= "3.9" and python_full_version < "3.10"')
    assert marker.evaluate({"python_full_version": "3.9.5"})
    assert not marker.evaluate({"python_full_version": "3.10.0"})
    assert not marker.is_empty()


def test_restrict_leaves_an_unprovided_set_variable_alone() -> None:
    marker = ms('"cpu" in extras')

    assert marker.restrict({}).equivalent(marker)


def test_restrict_plain_value_absent_is_residual() -> None:
    marker = ms('sys_platform == "linux"')
    assert marker.restrict({"os_name": "posix"}).equivalent(marker)


def test_restrict_or_tree() -> None:
    marker = ms('sys_platform == "linux" or os_name == "posix"')
    assert marker.restrict({"sys_platform": "win32"}).equivalent(
        ms('os_name == "posix"')
    )


def test_evaluate_through_complement() -> None:
    marker = ms('sys_platform == "linux"').complement()
    assert not marker.evaluate({"sys_platform": "linux"})
    assert marker.evaluate({"sys_platform": "win32"})


def test_complement_serialises_set_and_contains() -> None:
    for text in ('extra == "cpu"', '"x86" in platform_machine'):
        marker = ms(text).complement()
        result = marker.to_marker_string()
        assert result is not None
        assert marker.equivalent(ms(result))


def test_complement_serialises_pure_version_equality() -> None:
    marker = ms('python_full_version == "3.9"').complement()
    text = marker.to_marker_string()
    assert text is not None
    assert marker.equivalent(ms(text))
    assert marker.equivalent(ms('python_full_version != "3.9"'))


def test_complement_serialises_string_inequality() -> None:
    marker = ms('sys_platform != "linux"').complement()
    text = marker.to_marker_string()
    assert text is not None
    assert marker.equivalent(ms('sys_platform == "linux"'))


def test_complement_serialises_non_version_twin_literal() -> None:
    # platform_release is version-typed, but a non-version literal falls to the
    # string complement path.
    marker = ms('platform_release == "NT"').complement()
    text = marker.to_marker_string()
    assert text is not None
    assert marker.equivalent(ms(text))


def test_complement_serialises_substring_membership() -> None:
    for text in ('sys_platform in "linuxx"', 'sys_platform not in "linuxx"'):
        marker = ms(text).complement()
        result = marker.to_marker_string()
        assert result is not None
        assert marker.equivalent(ms(result))


def test_version_axis_equal_distinct_literals() -> None:
    # "3.9" and "3.9.0" are distinct strings that parse to the same version;
    # the pool must not double-count them.
    marker = ms('python_full_version == "3.9" or python_full_version == "3.9.0"')
    assert marker.evaluate({"python_full_version": "3.9.0"})
    assert not marker.is_empty()


def test_serialise_absorbs_constant_false_atom() -> None:
    # Complementing a disjunction with a constant-false string-ordering atom
    # exercises the "< / > complements to all" path.
    marker = (ms('sys_platform < "linux"') | ms('os_name == "posix"')).complement()
    text = marker.to_marker_string()
    assert text is not None
    assert marker.equivalent(ms(text))
    assert marker.equivalent(ms('os_name != "posix"'))


# ------------------------------------------------ the private packaging reach


def test_the_private_packaging_names_the_engine_reaches() -> None:
    # None of the six the manifest lists has a public replacement, so packaging
    # may rename one or reshape the parse tree in any release. That breaks every
    # import of nab_markersets, and this is where it shows.
    parsed = parse_marker('sys_platform == "linux"')
    lhs, op, rhs = parsed[0]
    assert isinstance(lhs, Variable)
    assert isinstance(rhs, Value)
    assert (lhs.value, op.value, rhs.value) == ("sys_platform", "==", "linux")

    with pytest.raises(ParserSyntaxError):
        parse_marker("sys_platform ==")

    # Called exactly as the engine calls it, keyword and all.
    assert _eval_op("linux", Op("=="), "linux", key="sys_platform") is True
    assert _eval_op("linux", Op("=="), "win32", key="sys_platform") is False
