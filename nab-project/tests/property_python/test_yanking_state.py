"""Replay changing selections and cached facts against an independent preparation model."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider._vendor.packaging.version import Version
from nab_provider.yank_candidates import Candidate, CandidateData
from nab_provider.yanked_resolution import MetadataProxy, PermissionProxy
from nab_resolver.ranges import Range

from .strategies import PROPERTY_SETTINGS
from .yanking_harness import Harness, node_for
from .yanking_model import PIN_SPECS, SPECIFIERS, Artifact, Case, Node, Request, cases

pytestmark = pytest.mark.property


@dataclass(frozen=True)
class Transition:
    """Select an artifact per name, or -1 to remove it, and add one cached metadata entry."""

    choices: tuple[int, int, int]
    cached: int
    with_extras: bool = False


CHAIN = Case(
    (
        Artifact("a", "1", withdrawn=False, dependencies=(Request("b"),)),
        Artifact("b", "1", withdrawn=False, dependencies=(Request("c", "==1"),)),
        Artifact("c", "1", withdrawn=True),
    ),
    (Request("a"), Request("c")),
)


def cache_data(harness: Harness, artifact: Artifact, node: Node) -> Candidate:
    """Build actual cached domain objects from independently declared metadata."""
    dependencies: dict[str, VersionRange] = {}
    pins = []
    for request in artifact.dependencies:
        if not request.active(harness.case.platform, extra=node.extra):
            continue
        for child in request.nodes:
            allowed = SPECIFIERS[request.spec].to_range()
            dependencies[child.text] = (
                dependencies.get(child.text, VersionRange.full()) & allowed
            )
        if request.spec in PIN_SPECS:
            pins.append(Requirement(request.text))
    if node.extra:
        dependencies[node.name] = VersionRange.singleton(Version(artifact.version))
    candidate = Candidate(node.text, Version(artifact.version), artifact.withdrawn)
    harness.search.catalogue.facts[candidate] = CandidateData(
        harness.search.catalogue.provider, dependencies, tuple(pins)
    )
    return candidate


def check_supports(harness: Harness, selected: dict[Node, Artifact]) -> None:
    """Grounding must agree with preparation sequences and keep sufficient proofs."""
    search = harness.search
    files = {node.name: artifact for node, artifact in selected.items()}
    available = frozenset(
        node
        for node, artifact in selected.items()
        if Candidate(node.text, Version(artifact.version), artifact.withdrawn)
        in search.catalogue.facts
    )
    expected = harness.case.reachable(files, frozenset(selected), available)
    assert {
        node_for(search.items[token].package) for token in search.supports
    } == expected
    for node, artifact in selected.items():
        candidate = Candidate(node.text, Version(artifact.version), artifact.withdrawn)
        token = search.identify(candidate)
        assert search.is_ready(MetadataProxy(token)) == (node in expected)

    for proxy, proofs in search.proofs.items():
        target = search.items[proxy.candidate]
        for support in proofs:
            conditions = {}
            for token in (*support, proxy.candidate):
                candidate = search.items[token]
                conditions[node_for(candidate.package)] = harness.artifacts[
                    candidate.base, candidate.label, candidate.withdrawn
                ]
            supported_files = {
                node.name: artifact for node, artifact in conditions.items()
            }
            supported_nodes = frozenset(conditions)
            available_nodes = frozenset(
                node_for(search.items[token].package) for token in support
            )
            permitted = harness.case.reachable(
                supported_files, supported_nodes, available_nodes
            )
            assert node_for(target.package) in permitted, (proxy, support, conditions)


@given(
    case=cases(),
    operations=st.lists(
        st.builds(
            Transition,
            choices=st.tuples(*(st.integers(-1, 3) for _ in range(3))),
            cached=st.integers(0, 100),
            with_extras=st.booleans(),
        ),
        min_size=1,
        max_size=12,
    ),
)
@example(
    case=CHAIN,
    operations=[
        Transition((0, 0, 0), 0),
        Transition((0, 0, 0), 1),
        Transition((0, 0, 0), 2),
        Transition((-1, 0, 0), 2),
    ],
)
@PROPERTY_SETTINGS
def test_grounding_and_proofs_survive_selection_and_cache_changes(
    case: Case, operations: list[Transition]
) -> None:
    harness = Harness(case)
    names = sorted({item.name for item in case.artifacts})
    proof_dependencies = {}
    for transition in operations:
        artifact = case.artifacts[transition.cached % len(case.artifacts)]
        for extra in (False, True):
            cache_data(harness, artifact, Node(artifact.name, extra))
        assignment = []
        for name, choice in zip(names, transition.choices, strict=False):
            options = [item for item in case.artifacts if item.name == name]
            assignment.append(None if choice < 0 else options[choice % len(options)])
        selected = {
            Node(name, extra): item
            for name, item in zip(names, assignment, strict=True)
            if item is not None
            for extra in ((False, True) if transition.with_extras else (False,))
        }
        decisions = {
            node.text: harness.search.identify(
                Candidate(node.text, Version(item.version), item.withdrawn)
            )
            for node, item in selected.items()
        }
        harness.search.receive_decision_scan_hint(
            {node: Range.singleton(token) for node, token in decisions.items()},
            decisions,
        )
        check_supports(harness, selected)
        for proxy, proofs in harness.search.proofs.items():
            assert isinstance(proxy, PermissionProxy)
            for version in proofs.values():
                dependencies = harness.search.get_dependencies(proxy, version)
                key = proxy, version
                assert (
                    key not in proof_dependencies
                    or proof_dependencies[key] == dependencies
                )
                proof_dependencies[key] = dependencies
