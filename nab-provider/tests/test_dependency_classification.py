"""Stored metadata must select the same dependencies as the live provider."""

from __future__ import annotations

import pytest

from nab_provider._vendor.packaging.markers import (
    Marker,
    UndefinedComparison,
    prepare_environment,
)
from nab_provider._vendor.packaging.version import Version
from nab_provider.environment import UnevaluableMarkerError, classify_dependency
from nab_provider.metadata import parse_metadata
from nab_provider.provider import Provider
from nab_provider.tags import PlatformSpec
from nab_provider.target import Matrix
from nab_provider.testing import make_coordinator, make_metadata, make_wheel


@pytest.mark.parametrize(
    ("condition", "selected", "expected"),
    [
        (None, set(), set()),
        ('python_version >= "3.10"', {"feature"}, set()),
        ('python_version >= "3.10"', set(), set()),
        ('python_version < "3.10"', {"feature"}, None),
        ('extra == "feature"', set(), None),
        ('extra == "feature"', {"feature"}, {"feature"}),
        ('extra != "feature"', {"feature"}, set()),
        ('extra == ""', {"feature"}, set()),
        ('extra == "feature" and extra == "other"', {"feature", "other"}, None),
        (
            'extra == "feature" or extra == "other"',
            {"feature", "other"},
            {"feature", "other"},
        ),
        ('extra == "absent"', {"feature", "other"}, None),
    ],
)
def test_classification_matches_real_provider(
    condition: str | None, selected: set[str], expected: set[str] | None
) -> None:
    target = Matrix(
        python="==3.14", platforms=(PlatformSpec("linux_x86_64"),)
    ).expand()[0]
    declaration = "dep>=2" + (f"; {condition}" if condition is not None else "")
    metadata = (
        make_metadata("demo", "1", declaration)
        + "Provides-Extra: feature\nProvides-Extra: other\n"
    )
    port = make_coordinator(
        listings={"demo": [make_wheel("1", package="demo")]}, metadata_text=metadata
    )
    provider = Provider(port, target)
    provider.get_dependencies("demo", Version("1"))
    declarations = provider.dependency_requirements_for(
        "demo", Version("1"), frozenset(selected)
    )
    marker = Marker(condition) if condition is not None else None
    environment = prepare_environment(target.env_with_membership())
    original = dict(environment)
    result = classify_dependency(marker, environment, selected)
    assert result == expected
    assert environment == original
    if expected is None:
        assert declarations == ()
    else:
        assert len(declarations) == 1
        assert set(declarations[0].required_for_parent_extras) == result


def test_classification_reports_an_unevaluable_marker() -> None:
    marker = Marker('sys_platform ~= "linux"')
    with pytest.raises(UnevaluableMarkerError):
        classify_dependency(marker, prepare_environment(), {"feature"})


@pytest.mark.parametrize(
    ("selected", "expected"),
    [
        pytest.param(set(), [("base-dep", ())], id="no-selected-extras"),
        pytest.param({"absent"}, [("base-dep", ())], id="undeclared-extra"),
        pytest.param(
            {"Fancy_Feature", "absent"},
            [
                ("base-dep", ()),
                ("feature-dep", ("fancy-feature",)),
                ("either-dep", ("fancy-feature",)),
            ],
            id="normalized-selected-intersection",
        ),
        pytest.param(
            {"second"},
            [("base-dep", ()), ("either-dep", ("second",))],
            id="other-declared-extra",
        ),
        pytest.param(
            {"fancy.feature", "second"},
            [
                ("base-dep", ()),
                ("feature-dep", ("fancy-feature",)),
                ("either-dep", ("fancy-feature", "second")),
            ],
            id="multiple-declared-extras",
        ),
    ],
)
def test_declarations_select_declared_extras_and_omit_urls(
    selected: set[str], expected: list[tuple[str, tuple[str, ...]]]
) -> None:
    target = Matrix(
        python="==3.14", platforms=(PlatformSpec("linux_x86_64"),)
    ).expand()[0]
    provider = Provider(make_coordinator(), target)
    provider.metadata_cache[("demo-name", Version("1"))] = parse_metadata(
        make_metadata(
            "Demo_Name",
            "1",
            "base-dep[Child.Extra]>=1",
            'excluded-dep; python_version < "3.10"',
            'feature-dep; extra == "fancy-feature"',
            'either-dep; extra == "fancy-feature" or extra == "second"',
            'unprovided-dep; extra == "absent"',
            "direct-dep @ https://example.com/direct.whl",
            'extra-url @ https://example.com/extra.whl ; extra == "fancy-feature"',
            'unevaluable-url @ https://example.com/invalid.whl ; sys_platform ~= "linux"',
            provides_extra=("Fancy.Feature", "second"),
        )
    )

    declarations = provider.dependency_requirements_for(
        "Demo_Name", Version("1"), frozenset(selected)
    )

    assert [
        (item.dependency_name, item.required_for_parent_extras) for item in declarations
    ] == expected
    assert all(item.parent_name == "demo-name" for item in declarations)
    assert declarations[0].dependency_extras == ("child-extra",)
    assert declarations[0].dependency_specifier == ">=1"


@pytest.mark.parametrize("selected", [frozenset(), frozenset({"feature"})])
def test_declarations_preserve_unevaluable_marker_error(
    selected: frozenset[str],
) -> None:
    target = Matrix(
        python="==3.14", platforms=(PlatformSpec("linux_x86_64"),)
    ).expand()[0]
    provider = Provider(make_coordinator(), target)
    provider.metadata_cache[("demo", Version("1"))] = parse_metadata(
        make_metadata(
            "demo", "1", 'dep; sys_platform ~= "linux"', provides_extra=("feature",)
        )
    )
    environment = dict(provider.prepared_environment)

    with pytest.raises(UnevaluableMarkerError) as caught:
        provider.dependency_requirements_for("demo", Version("1"), selected)

    assert isinstance(caught.value.__cause__, UndefinedComparison)
    assert provider.prepared_environment == environment
