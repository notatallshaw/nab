"""Extras listing failures retain the version bounds of their declarations."""

from __future__ import annotations

import pytest

from nab_project._resolve.engine import _augment_resolution_error
from nab_project._testing.coordinator_fake import make_coordinator
from nab_project.inputs import ResolveInputs
from nab_project.resolve import resolve_with_coordinator
from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider._vendor.packaging.version import Version
from nab_provider.provider import Provider
from nab_provider.records import SdistFile
from nab_provider.target import ResolveTarget
from nab_provider.testing import make_metadata, make_wheel
from nab_resolver.errors import ResolutionError
from nab_resolver.types import Incompatibility, IncompatibilityCause, Term


@pytest.mark.parametrize("transitive", [False, True])
@pytest.mark.parametrize("minimum", ["1", "2"])
def test_empty_extra_listing_scopes_zip_advice_to_the_requirement(
    minimum: str, *, transitive: bool
) -> None:
    dependency = f"leaf[x]>={minimum}"
    for allow_zip in (False, True):
        port = make_coordinator(
            listings={
                "mid": [make_wheel("1", package="mid")],
                "leaf": [
                    SdistFile(
                        "leaf-1.zip", "https://example.test/leaf-1.zip", "1", None, None
                    )
                ],
            },
            metadata_text=make_metadata("mid", "1", dependency),
            sdist_pkg_info=make_metadata(
                "leaf", "1", provides_extra=("x",), metadata_version="2.2"
            ),
        )
        result = resolve_with_coordinator(
            port,
            [ResolveTarget.for_host()],
            [Requirement("mid" if transitive else dependency)],
            inputs=ResolveInputs(allow_zip_sdists=allow_zip),
        ).target_results[0]

        if allow_zip and minimum == "1":
            assert result.success
            assert result.pins["leaf"] == Version("1")
            continue

        assert not result.success
        error = result.error
        assert error is not None
        assert error.verbose_message is not None

        for report in (str(error), error.verbose_message):
            if minimum == "1":
                assert 'try: set packages."leaf".allow-zip-sdists = true' in report
            else:
                assert "allow-zip-sdists" not in report
                if not allow_zip:
                    assert "leaf[x] >=2:" in report
                    assert "no version matches the requirement" in report


def test_extra_failure_combines_bounds_from_every_parent_release_in_the_proof() -> None:
    port = make_coordinator(
        listings={
            "mid": [make_wheel(version, package="mid") for version in ("1", "2")],
            "leaf": [
                SdistFile(
                    "leaf-1.zip", "https://example.test/leaf-1.zip", "1", None, None
                )
            ],
        },
        metadata_by_version={
            version: make_metadata("mid", version, f"leaf[x]>={version}")
            for version in ("1", "2")
        },
    )

    result = resolve_with_coordinator(
        port, [ResolveTarget.for_host()], [Requirement("mid")]
    ).target_results[0]

    assert not result.success
    error = result.error
    assert error is not None
    assert error.verbose_message is not None

    for report in (str(error), error.verbose_message):
        assert 'try: set packages."leaf".allow-zip-sdists = true' in report


def test_unbounded_extra_failure_keeps_its_label_and_zip_advice() -> None:
    port = make_coordinator(
        listings={
            "leaf": [
                SdistFile(
                    "leaf-1.zip", "https://example.test/leaf-1.zip", "1", None, None
                )
            ]
        },
    )

    result = resolve_with_coordinator(
        port, [ResolveTarget.for_host()], [Requirement("leaf[x]")]
    ).target_results[0]

    assert not result.success
    error = result.error
    assert error is not None
    assert error.verbose_message is not None

    for report in (str(error), error.verbose_message):
        assert "leaf[x]:" in report
        assert 'try: set packages."leaf".allow-zip-sdists = true' in report


@pytest.mark.parametrize("minimum", ["1", "2"])
def test_extra_failure_without_declaration_uses_the_failed_clause(minimum: str) -> None:
    provider = Provider(
        make_coordinator(
            listings={
                "leaf": [
                    SdistFile(
                        "leaf-1.zip", "https://example.test/leaf-1.zip", "1", None, None
                    )
                ]
            },
        )
    )
    assert provider.choose_version("leaf[x]", VersionRange.full()) is None

    # A standalone failure clause has no root or parent declaration to recover.
    clause = Incompatibility(
        [Term("leaf[x]", Requirement(f"leaf>={minimum}").specifier.to_range())],
        cause=IncompatibilityCause.NO_VERSIONS,
    )
    error = ResolutionError("resolution failed", incompatibility=clause)

    _augment_resolution_error(error, provider)

    assert error.verbose_message is not None

    for report in (str(error), error.verbose_message):
        assert "leaf[x]:" in report
        if minimum == "1":
            assert 'try: set packages."leaf".allow-zip-sdists = true' in report
        else:
            assert "no version matches the requirement" in report
            assert "allow-zip-sdists" not in report
