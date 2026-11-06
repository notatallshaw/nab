"""Metadata source selection independently of install artifact policy."""

from __future__ import annotations

from functools import partial

import pytest

from nab_provider._vendor.packaging.version import Version
from nab_provider.overrides import IndexOverride
from nab_provider.policy import DistPolicy, MetadataPolicy
from nab_provider.provider import Provider
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget
from nab_provider.testing import (
    make_coordinator,
    make_metadata,
    make_sdist,
    make_wheel,
    pkg_override,
)
from nab_provider.yank_candidates import YankCandidates
from nab_provider.yanking import mark_yanked

_TARGET = ResolveTarget.for_declared(
    python_version="3.12", spec=PlatformSpec("linux_x86_64")
)


@pytest.mark.parametrize(
    ("dist_policy", "metadata_policy", "dependency"),
    [
        (DistPolicy.SDIST_ONLY, MetadataPolicy.MATCH_ARTIFACT, "source-dep"),
        (DistPolicy.SDIST_ONLY, MetadataPolicy.PREFER_WHEEL, "wheel-dep"),
        (DistPolicy.WHEEL_OR_SDIST, MetadataPolicy.MATCH_ARTIFACT, "wheel-dep"),
        (DistPolicy.WHEEL_OR_SDIST, MetadataPolicy.PREFER_WHEEL, "wheel-dep"),
    ],
)
def test_metadata_comes_from_the_selected_policy(
    dist_policy: DistPolicy, metadata_policy: MetadataPolicy, dependency: str
) -> None:
    port = make_coordinator(
        listings={"demo": [make_sdist(package="demo"), make_wheel(package="demo")]},
        metadata_text=make_metadata("demo", "1.0", "wheel-dep>=1"),
        sdist_pkg_info=make_metadata(
            "demo", "1.0", "source-dep>=1", metadata_version="2.2"
        ),
    )
    provider = Provider(
        port, target=_TARGET, dist_policy=dist_policy, metadata_policy=metadata_policy
    )
    assert set(provider.get_dependencies("demo", Version("1.0"))) == {dependency}


@pytest.mark.parametrize("source_yanked", [False, True])
@pytest.mark.parametrize(
    "metadata_policy", [MetadataPolicy.MATCH_ARTIFACT, MetadataPolicy.PREFER_WHEEL]
)
def test_yank_preparation_preserves_metadata_policy(
    source_yanked: bool, metadata_policy: MetadataPolicy
) -> None:
    wheel = make_wheel(package="demo")
    source = make_sdist(package="demo")
    files = (
        [wheel, mark_yanked(source, reason="withdrawn")]
        if source_yanked
        else [mark_yanked(wheel, reason="withdrawn"), source]
    )

    port = make_coordinator(
        listings={"demo": files},
        metadata_text=make_metadata("demo", "1.0", "wheel-dep"),
        sdist_pkg_info=make_metadata(
            "demo", "1.0", "source-dep", metadata_version="2.2"
        ),
    )

    factory = partial(
        Provider,
        port,
        target=_TARGET,
        defer_yanked=True,
        dist_policy=DistPolicy.SDIST_ONLY,
        metadata_policy=metadata_policy,
    )
    facts = YankCandidates(factory(), factory, preferences={})
    (candidate,) = facts.choices("demo")
    data = facts.prepare(candidate)

    assert data is not None
    expected = (
        "wheel-dep"
        if source_yanked and metadata_policy is MetadataPolicy.PREFER_WHEEL
        else "source-dep"
    )
    assert set(data.dependencies) == {expected}


@pytest.mark.parametrize("scope", ["package", "rule", "index"])
def test_scoped_metadata_policy_can_restore_the_default(scope: str) -> None:
    port = make_coordinator()
    override = pkg_override(
        "demo<2" if scope == "rule" else "demo",
        metadata_policy=MetadataPolicy.MATCH_ARTIFACT,
    )
    provider = Provider(
        port,
        target=_TARGET,
        metadata_policy=MetadataPolicy.PREFER_WHEEL,
        package_overrides=() if scope == "index" else (override,),
        index_overrides=(
            {"private": IndexOverride(metadata_policy=MetadataPolicy.MATCH_ARTIFACT)}
            if scope == "index"
            else {}
        ),
    )
    assert (
        provider.effective_metadata_policy("demo", Version("1.0"), "private")
        is MetadataPolicy.MATCH_ARTIFACT
    )
    assert (
        provider.effective_metadata_policy("other", Version("1.0"), "pypi")
        is MetadataPolicy.PREFER_WHEEL
    )
    if scope == "rule":
        assert (
            provider.effective_metadata_policy("demo", Version("2.0"), "private")
            is MetadataPolicy.PREFER_WHEEL
        )
