"""Behavioral contracts for the shared in-memory test setup tools."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from nab_provider.metadata import parse_metadata
from nab_provider.policy import BuildPolicy, LocalSource, SourceRequest
from nab_provider.records import DEFAULT_INDEX_NAME, release_wheel_payload
from nab_provider.testing import make_coordinator, make_metadata, make_sdist, make_wheel

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        pytest.param(
            None, "https://example.com/pkg-1.0-py3-none-any.whl.metadata", id="derived"
        ),
        pytest.param("", None, id="released"),
        pytest.param(
            "https://files.example/wheel.whl#sha256=ab",
            "https://files.example/wheel.whl.metadata",
            id="fragment",
        ),
    ],
)
def test_wheel_sidecar_uses_the_configured_url(
    url: str | None, expected: str | None
) -> None:
    assert make_wheel(url=url).metadata_url == expected


@pytest.mark.parametrize("package", ["My-Lib", "my.Lib", "my___lib"])
def test_artifact_names_use_the_distribution_filename_form(package: str) -> None:
    wheel = make_wheel(package=package)
    source = make_sdist(package=package)
    assert wheel.filename == "my_lib-1.0-py3-none-any.whl"
    assert source.filename == "my_lib-1.0.tar.gz"
    assert wheel.url.endswith("/" + wheel.filename)
    assert source.url.endswith("/" + source.filename)


def test_record_fixtures_do_not_share_releasable_state() -> None:
    first = make_wheel()
    second = make_wheel()
    assert first == second

    release_wheel_payload(first)

    assert first.metadata_url is None
    assert (
        second.metadata_url == "https://example.com/pkg-1.0-py3-none-any.whl.metadata"
    )


def test_generated_metadata_preserves_dependency_and_extra_semantics() -> None:
    text = make_metadata(
        "alpha",
        "2.0",
        "base>=1",
        'optional; extra == "feature"',
        requires_python=">=3.11",
        provides_extra=("feature",),
        metadata_version="2.4",
    )
    metadata = parse_metadata(text)

    assert metadata.name == "alpha"
    assert metadata.version == Version("2.0")
    assert metadata.metadata_version == "2.4"
    assert metadata.requires_python == SpecifierSet(">=3.11")
    assert metadata.requires_dist == [
        Requirement("base>=1"),
        Requirement('optional; extra == "feature"'),
    ]
    assert metadata.provides_extra == ["feature"]


def test_pending_listing_arrives_only_when_waited_on() -> None:
    ready = make_wheel(package="ready")
    wheel = make_wheel(package="later")
    sdist = make_sdist(package="later")
    port = make_coordinator(
        listings={"ready": [ready]}, pending_listings={"later": [wheel, sdist]}
    )

    event = port.request_listing("later", speculative=True)

    assert port.index.get_listing("ready") == [ready]
    assert port.index.get_listing("later") is None
    assert not event.is_set()
    assert port.calls_to("request_listing") == [("later", True)]

    assert event.wait(0)

    assert event.is_set()
    assert port.index.get_listing("later") == [wheel, sdist]
    assert port.index.get_listing_index("later") == DEFAULT_INDEX_NAME


def test_ports_over_the_same_fixture_graph_have_independent_state() -> None:
    graph = {"pkg": [make_wheel()]}
    first = make_coordinator(pending_listings=graph)
    second = make_coordinator(pending_listings=graph)

    first.request_listing("pkg").wait()

    assert first.index.get_listing("pkg") == graph["pkg"]
    assert second.index.get_listing("pkg") is None
    assert second.calls_to("request_listing") == []


def test_source_materialization_needs_a_handler(tmp_path: Path) -> None:
    port = make_coordinator()
    request = SourceRequest(
        "demo",
        LocalSource("demo", str(tmp_path)),
        BuildPolicy.NEVER,
        vcs_cache_dir=None,
        archive_cache_dir=None,
        require_pin=True,
    )

    with pytest.raises(NotImplementedError, match="materialize"):
        port.materialize_source(request)

    assert port.calls_to("materialize_source") == [(request,)]


def test_source_build_needs_a_handler() -> None:
    port = make_coordinator()
    url = "https://example.org/demo-1.tar.gz"

    with pytest.raises(NotImplementedError, match="build_sdist"):
        port.build_sdist_metadata("demo", "1", url, ())

    assert port.calls_to("build_sdist_metadata") == [("demo", "1", url, ())]
