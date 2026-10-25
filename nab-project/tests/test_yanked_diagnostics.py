"""Admission failures retain actionable evidence from the final search proof."""

from __future__ import annotations

from functools import partial

import pytest

from nab_project._testing.yanking import graph_port
from nab_project.diagnostics import ResolutionMessage
from nab_project.resolve import resolve_with_coordinator
from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider.marker_holds import dependency_marker_holds
from nab_provider.policy import DistPolicy
from nab_provider.provider import Provider
from nab_provider.records import SdistFile
from nab_provider.resolver_inputs import build_resolver_inputs
from nab_provider.target import ResolveTarget
from nab_provider.testing import make_sdist
from nab_provider.vcs_admission import VcsConfig
from nab_provider.yanked_resolution import YankProxyProvider
from nab_resolver.errors import ResolutionError
from nab_resolver.types import RootRequirement


@pytest.mark.parametrize("dependency", ["leaf", "leaf[x]"])
@pytest.mark.parametrize("refusal", ["zip", "python", "absent"])
def test_admission_failure_reports_transitive_listing_refusal(
    dependency: str, refusal: str
) -> None:
    port = graph_port(
        [("mid", str(version), False, [dependency]) for version in range(1, 4)]
        + [("timeout", "2", True, []), ("timeout", "2", False, [])]
    )
    suffix = ".zip" if refusal == "zip" else ".tar.gz"
    files = (
        []
        if refusal == "absent"
        else [
            SdistFile(
                f"leaf-1{suffix}",
                f"https://example.test/leaf-1{suffix}",
                "1",
                ">=99" if refusal == "python" else None,
                None,
            )
        ]
    )
    port.index.store_listing("leaf", files)

    result = resolve_with_coordinator(
        port,
        [ResolveTarget.for_host()],
        [Requirement("mid"), Requirement("timeout")],
    )
    error = result.target_results[0].error

    assert error is not None
    assert isinstance(error.args[0], ResolutionMessage)
    compact = str(error)
    verbose = error.verbose_message
    assert verbose is not None
    assert "MetadataProxy" not in compact
    assert "PermissionProxy" not in compact
    assert "grounded yanked-file admission" not in compact
    assert "leaf:" in compact
    if refusal == "zip":
        assert "legacy ZIP sdists require allow-zip-sdists = true" in compact
        assert 'try: set packages."leaf".allow-zip-sdists = true' in compact
        assert "project-wide allow-zip-sdists" in verbose
    elif refusal == "python":
        assert "no file supports Python" in compact
        assert ">=99" in verbose
        assert "allow-zip-sdists" not in compact
    else:
        assert "package not found on any configured index" in compact


def test_failure_uses_the_dependency_range_before_suggesting_zip_permission() -> None:
    port = graph_port(
        [("mid", str(version), False, ["leaf>=2"]) for version in range(1, 4)]
        + [("timeout", "2", True, []), ("timeout", "2", False, [])]
    )
    port.index.store_listing(
        "leaf",
        [SdistFile("leaf-1.zip", "https://example.test/leaf-1.zip", "1", None, None)],
    )

    result = resolve_with_coordinator(
        port, [ResolveTarget.for_host()], [Requirement("mid"), Requirement("timeout")]
    )
    error = result.target_results[0].error

    assert error is not None
    assert "leaf:" in str(error)
    assert "allow-zip-sdists = true" not in str(error)
    assert error.verbose_message is not None
    assert "allow-zip-sdists = true" not in error.verbose_message


def test_failure_ignores_rejections_outside_its_proof() -> None:
    port = graph_port([("mid", "1", False, ["missing"]), ("discarded", "1", False, [])])
    url = "https://example.test/discarded-1-False.whl.metadata"
    port.index.store_metadata("discarded", "1", "invalid metadata", metadata_url=url)
    declarations = [Requirement("mid")]
    inputs = build_resolver_inputs(
        declarations,
        VcsConfig(),
        environment=ResolveTarget.for_host().marker_env,
        marker_holds=dependency_marker_holds,
    )
    factory = partial(Provider, port, defer_yanked=True)
    search = YankProxyProvider(
        factory(), factory, inputs.roots, declarations, {}, preferences={}
    )
    discarded = search.catalogue.choices("discarded")[0]
    assert search.catalogue.prepare(discarded) is None
    assert discarded in search.catalogue.rejections

    with pytest.raises(ResolutionError) as caught:
        search.resolve()

    message = str(caught.value)
    assert "missing:" in message
    assert "discarded" not in message
    assert "Last metadata rejection" not in message


def test_failure_reports_each_relevant_metadata_rejection() -> None:
    port = graph_port(
        [("mid", str(version), False, ["broken"]) for version in range(1, 4)]
        + [("timeout", "2", True, []), ("timeout", "2", False, [])]
        + [("broken", version, False, []) for version in ("1", "2")]
    )
    for version in ("1", "2"):
        port.index.store_metadata(
            "broken",
            version,
            "invalid metadata",
            metadata_url=f"https://example.test/broken-{version}-False.whl.metadata",
        )

    result = resolve_with_coordinator(
        port, [ResolveTarget.for_host()], [Requirement("mid"), Requirement("timeout")]
    )
    error = result.target_results[0].error

    assert error is not None
    assert isinstance(error.args[0], ResolutionMessage)
    assert "broken:" in str(error)
    assert str(error).count("broken:") == 1
    assert error.verbose_message is not None
    assert "broken==1" in error.verbose_message
    assert "broken==2" in error.verbose_message
    assert "Last metadata rejection" not in str(error)


@pytest.mark.parametrize("name", ["leaf", "leaf[x]"])
def test_root_failure_with_a_nonempty_listing_reports_the_original_domain(
    name: str,
) -> None:
    port = graph_port([("leaf", "1", False, [])], {("leaf", "1", False): ("x",)})
    declarations = [Requirement(name + ">=2")]
    inputs = build_resolver_inputs(
        declarations,
        VcsConfig(),
        environment=ResolveTarget.for_host().marker_env,
        marker_holds=dependency_marker_holds,
    )
    factory = partial(Provider, port, defer_yanked=True)
    search = YankProxyProvider(
        factory(), factory, inputs.roots, declarations, {}, preferences={}
    )

    with pytest.raises(ResolutionError) as caught:
        search.resolve()

    assert "leaf:" in str(caught.value)
    assert "no version matches" in str(caught.value)
    assert "MetadataProxy" not in str(caught.value)


def test_an_earlier_broad_failure_does_not_supply_a_zip_remedy_for_a_narrow_root() -> (
    None
):
    port = graph_port([])
    port.index.store_listing(
        "leaf",
        [SdistFile("leaf-1.zip", "https://example.test/leaf-1.zip", "1", None, None)],
    )
    declarations = [Requirement("leaf>=2")]
    inputs = build_resolver_inputs(
        declarations,
        VcsConfig(),
        environment=ResolveTarget.for_host().marker_env,
        marker_holds=dependency_marker_holds,
    )
    factory = partial(Provider, port, defer_yanked=True)
    provider = factory()
    assert (
        provider.choose_version("leaf", Requirement("leaf").specifier.to_range())
        is None
    )
    search = YankProxyProvider(
        provider, factory, inputs.roots, declarations, {}, preferences={}
    )

    with pytest.raises(ResolutionError) as caught:
        search.resolve()

    assert "leaf:" in str(caught.value)
    assert "allow-zip-sdists = true" not in str(caught.value)
    assert caught.value.verbose_message is not None
    assert "allow-zip-sdists = true" not in caught.value.verbose_message


def test_install_artifact_identity_failure_does_not_invent_a_listing_refusal() -> None:
    port = graph_port([("leaf", "1", False, [])])
    files = port.index.get_listing("leaf")
    assert files is not None
    port.index.store_listing("leaf", [*files, make_sdist("1.0", package="leaf")])
    requirements = [Requirement("leaf===1")]
    allowed = requirements[0].specifier.to_range()
    factory = partial(
        Provider, port, defer_yanked=True, dist_policy=DistPolicy.SDIST_INSTALL
    )
    search = YankProxyProvider(
        factory(),
        factory,
        [RootRequirement("leaf", allowed)],
        requirements,
        {},
        preferences={},
    )

    with pytest.raises(ResolutionError) as caught:
        search.resolve()

    # The wheel matches the identity pin, but the installable source has another label.
    assert any(
        version in allowed
        for version, _ in search.catalogue.provider.versions_cache["leaf"]
    )
    assert [candidate.label for candidate in search.catalogue.choices("leaf")] == [
        "1.0"
    ]
    assert not search.catalogue.facts
    assert port.calls_to("request_built_metadata") == []

    message = caught.value.args[0]
    assert isinstance(message, ResolutionMessage)
    assert message.entries == ()
    assert (
        str(caught.value)
        == "no solution satisfies the requirements and yanked-file policy"
    )
