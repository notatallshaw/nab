"""Unit suite for the marker algebra: acceptance cases, algebra, and edges.

Correctness against packaging is pinned by ``test_marker_algebra_differential``;
this suite pins the ported acceptance cases and exercises the construction,
decision, restriction, serialisation, witness, and guard surfaces.
"""

from __future__ import annotations

import sys
import traceback

import pytest

from nab_python._vendor.packaging import markersets
from nab_python._vendor.packaging.markers import (
    InvalidMarker,
    Marker,
    UndefinedEnvironmentName,
)
from nab_python._vendor.packaging.markersets import (
    IntractableMarkerSet,
    MarkerSet,
    UnserializableMarkerSet,
    variable_names,
)


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
    with pytest.raises(TypeError):
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
    with pytest.raises(TypeError):
        _ = a & "linux"  # type: ignore[operator]


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


def test_restrict_error_policy() -> None:
    marker = ms('python_version >= "3.9" and sys_platform == "linux"')
    with pytest.raises(ValueError, match="no value for"):
        marker.restrict({"python_version": "3.10"}, on_unknown_variable="error")
    # every referenced variable provided: no error.
    restricted = marker.restrict(
        {"python_full_version": "3.10.0", "sys_platform": "linux"},
        on_unknown_variable="error",
    )
    assert restricted.is_full()


def test_restrict_rejects_bad_policy() -> None:
    with pytest.raises(ValueError, match="on_unknown_variable"):
        ms('sys_platform == "linux"').restrict({}, on_unknown_variable="nonsense")


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


def test_membership_literals() -> None:
    marker = ms('"cpu" in extras and "gpu" not in extras and sys_platform == "linux"')
    assert marker.membership_literals() == frozenset(
        {("extras", "cpu"), ("extras", "gpu")}
    )
    assert ms('sys_platform == "linux"').membership_literals() == frozenset()


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


def test_witness_none_for_opaque_over_approximation() -> None:
    # Opaque contains over-approximates: the set is not is_empty, yet no real
    # environment satisfies "x86_64 and contains x86-but-not-x86".
    marker = ms('platform_machine == "arm64" and "x86" in platform_machine')
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


def test_unserializable_ordered_version_complement() -> None:
    with pytest.raises(UnserializableMarkerSet):
        ms('python_full_version >= "3.9"').complement().to_marker_string()


def test_unserializable_twin_equality_complement() -> None:
    with pytest.raises(UnserializableMarkerSet):
        ms('platform_release == "6.6"').complement().to_marker_string()


def test_repr_summarises_without_leaking_the_tree() -> None:
    # repr renders a marker-string summary, never the private op-tree, and never
    # raises: the constant sets read as words, a plain set as its marker string,
    # and a grammar-inexpressible complement as a placeholder.
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
        for decide in (deep.is_empty, deep.to_marker_string, deep.witness):
            with pytest.raises(IntractableMarkerSet):
                decide()
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


def test_restrict_error_missing_set_variable() -> None:
    marker = ms('"cpu" in extras')
    with pytest.raises(ValueError, match="no value for"):
        marker.restrict({}, on_unknown_variable="error")


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
