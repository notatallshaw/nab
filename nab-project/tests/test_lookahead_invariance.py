"""The look-ahead picks among valid solutions; it does not decide validity.

``Provider._look_ahead_ok`` rejects a candidate whose dependencies already
contradict a root requirement or a decision the resolve has taken, so the scan
walks on to another version and the resolver can return different pins than it
would have without the check.  What the rejection must not do is change which
graphs resolve, or leave behind a solution that does not hold together.

Each generated graph is therefore resolved twice, once through the real
provider and once through one whose scan accepts every candidate it reaches.
Both runs are checked for a whole solution: everything the resolve asked for
is pinned, and every requirement of every pin is met.  The two are then
compared on solvability.  The solutions themselves are deliberately not
compared, because on some of these graphs the runs pin different versions, or
install different packages, and both answers are valid.

The graphs are layered, so ``pkg0`` may only require ``pkg1`` and later.  That
keeps them acyclic and leaves solvability decided by the version pins alone.
Each graph also asks for a later package under a ceiling, which is what gives
the scan a root requirement to reject against rather than only a decision.

A fixed graph separately exercises decision-aware look-ahead through the real
resolver.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING, NamedTuple

from nab_index.client import WheelFile
from nab_project._testing.coordinator_fake import make_coordinator
from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider._vendor.packaging.specifiers import SpecifierSet
from nab_provider._vendor.packaging.version import Version
from nab_provider.provider import Provider
from nab_provider.target import ResolveTarget
from nab_resolver.errors import ResolutionError
from nab_resolver.resolver import Resolver
from nab_resolver.types import Incompatibility

if TYPE_CHECKING:
    from collections.abc import Sequence

TARGET = ResolveTarget.for_host_python("3.12.0")

# Sized so the scan rejects on a good share of the corpus, which the closing
# assertions pin: on smaller graphs it rarely fires and the two runs are then
# the same resolve twice.
VERSIONS = ("1.0", "1.1", "2.0", "2.1", "3.0", "3.1", "4.0", "4.1")
MAX_PACKAGES = 7
SEEDS = 300


class _Graph(NamedTuple):
    """One graph: what the resolve asks for and what the index holds."""

    root_requirements: dict[str, VersionRange]
    listings: dict[str, list[WheelFile]]
    metadata_by_url: dict[str, str]


def _sidecar_url(package: str, version: Version | str) -> str:
    return f"https://example.com/{package}-{version}.whl.metadata"


def _wheel(package: str, version: str) -> WheelFile:
    return WheelFile(
        filename=f"{package}-{version}-py3-none-any.whl",
        url=f"https://example.com/{package}-{version}.whl",
        version=version,
        requires_python=None,
        has_metadata=True,
        upload_time=None,
    )


def _metadata(package: str, version: str, requires: Sequence[str]) -> str:
    lines = ["Metadata-Version: 2.1", f"Name: {package}", f"Version: {version}"]
    lines += [f"Requires-Dist: {requirement}" for requirement in requires]
    return "\n".join(lines) + "\n"


def _requires(rng: random.Random, candidates: Sequence[str]) -> list[str]:
    """Draw the Requires-Dist lines one version declares on later packages.

    The mix of pins, floors and ceilings is what makes some graphs conflict: an
    ``==`` on a package that another decided version floors above it is the
    shape the look-ahead rejects.
    """
    requires = []
    for package in candidates:
        if rng.random() > 0.55:
            continue
        bound = rng.choice(VERSIONS)
        form = rng.random()
        if form < 0.55:
            requires.append(f"{package}=={bound}")
        elif form < 0.8:
            requires.append(f"{package}>={bound}")
        else:
            requires.append(f"{package}<{bound}")
    return requires


def _root_requirements(
    rng: random.Random, packages: Sequence[str]
) -> dict[str, VersionRange]:
    """Draw what the resolve asks for: the first package, and a later one capped.

    The cap is what the look-ahead's root-requirement check tests against.  A
    candidate that floors the capped package above it is rejected on the root
    rather than on a decision, and that rejection is permanent: it flushes as a
    single-term clause that holds for the rest of the resolve.  It is drawn
    from the upper versions so the capped package keeps some to choose from.
    """
    capped = rng.choice(packages[1:])
    return {
        packages[0]: VersionRange.full(admit_arbitrary=False),
        capped: SpecifierSet(f"<{rng.choice(VERSIONS[3:])}").to_range(),
    }


def _build_graph(seed: int) -> _Graph:
    """Build ``seed``'s graph: pkg0 first, every version with its own METADATA."""
    rng = random.Random(seed)  # noqa: S311
    packages = [f"pkg{i}" for i in range(rng.randint(3, MAX_PACKAGES))]
    listings = {
        package: [_wheel(package, version) for version in reversed(VERSIONS)]
        for package in packages
    }

    metadata_by_url = {}
    for depth, package in enumerate(packages):
        for version in VERSIONS:
            requires = _requires(rng, packages[depth + 1 :])
            metadata_by_url[_sidecar_url(package, version)] = _metadata(
                package, version, requires
            )

    return _Graph(_root_requirements(rng, packages), listings, metadata_by_url)


class _CountingProvider(Provider):
    """Count root rejections and clauses supported by the resolver's decisions.

    Root-rejection tables empty on flush, so they are counted before that call.
    Decision-backed clauses are counted where the resolver consumes them.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.root_rejections = 0
        self.decision_backed_clauses = 0

    def _flush_pending_blocks(self) -> None:
        self.root_rejections += sum(
            len(versions) for versions in self.pending_root_blocks.values()
        )
        super()._flush_pending_blocks()

    def consume_pending_clauses(self) -> list[Incompatibility[str, Version]]:
        """Drain clauses and count those supported by a current decision."""
        clauses = super().consume_pending_clauses()
        for clause in clauses:
            if len(clause.terms) != 2:
                continue
            if any(
                term.package in self.solution_decisions
                and self.solution_decisions[term.package] in term.constraint
                for term in clause.terms
            ):
                self.decision_backed_clauses += 1
        return clauses


class _NoLookAheadProvider(_CountingProvider):
    """Counting provider whose decision scan accepts every candidate it reaches."""

    def _look_ahead_ok(
        self, package: str, version: Version, *, check_decisions: bool = True
    ) -> bool:
        return True


class _Run(NamedTuple):
    """What one resolve returned, with the rejections its scan queued.

    ``pins`` is None when the graph is unsolvable.  ``rejections`` counts every
    candidate the scan skipped, ``root_rejections`` only those it skipped on a
    root requirement, and ``decision_backed_clauses`` those whose terms contain
    a current decision.
    """

    pins: dict[str, Version] | None
    rejections: int
    root_rejections: int
    decision_backed_clauses: int


def _resolve(graph: _Graph, provider_type: type[_CountingProvider]) -> _Run:
    """Resolve ``graph``'s requirements through a fresh provider and coordinator."""
    coordinator = make_coordinator(
        listings=graph.listings, metadata_by_url=graph.metadata_by_url
    )
    provider = provider_type(
        coordinator, target=TARGET, root_requirements=graph.root_requirements
    )
    resolver = Resolver(provider, range_type=VersionRange, root_version="0")

    try:
        pins = resolver.resolve(dict(graph.root_requirements))
    except ResolutionError:
        pins = None
    return _Run(
        pins,
        provider.stats.look_ahead_rejections,
        provider.root_rejections,
        provider.decision_backed_clauses,
    )


def _decision_rejection_graph() -> _Graph:
    """Build a graph that decides leaf 3.0 before scanning app."""
    versions_by_package = {
        "app": ("2.0", "1.0"),
        "bridge": ("1.0",),
        "leaf": ("3.0", "2.0"),
    }
    requirements_by_candidate = {
        ("app", "2.0"): ["leaf==2.0"],
        ("bridge", "1.0"): ["leaf==3.0"],
    }

    return _Graph(
        root_requirements={
            "app": VersionRange.full(admit_arbitrary=False),
            "bridge": SpecifierSet("==1.0").to_range(),
        },
        listings={
            package: [_wheel(package, version) for version in package_versions]
            for package, package_versions in versions_by_package.items()
        },
        metadata_by_url={
            _sidecar_url(package, version): _metadata(
                package,
                version,
                requirements_by_candidate.get((package, version), []),
            )
            for package, package_versions in versions_by_package.items()
            for version in package_versions
        },
    )


def test_real_resolve_consumes_decision_backed_look_ahead_clause() -> None:
    run = _resolve(_decision_rejection_graph(), _CountingProvider)

    assert run.pins == {
        "app": Version("1.0"),
        "bridge": Version("1.0"),
        "leaf": Version("3.0"),
    }
    assert run.decision_backed_clauses > 0


def _declared_requirements(
    graph: _Graph, package: str, version: Version
) -> list[Requirement]:
    """Read the requirements back out of the METADATA the graph published."""
    text = graph.metadata_by_url[_sidecar_url(package, version)]
    return [
        Requirement(line.removeprefix("Requires-Dist: "))
        for line in text.splitlines()
        if line.startswith("Requires-Dist: ")
    ]


def _closure_errors(graph: _Graph, pins: dict[str, Version]) -> list[str]:
    """Report every way ``pins`` fails to be a solution for ``graph``.

    A solution holds a version of everything the resolve asked for and of
    everything those versions require, each inside the range that asked for it.
    """
    errors = []
    for package, asked in graph.root_requirements.items():
        chosen = pins.get(package)
        if chosen is None:
            errors.append(f"asked for {package}, absent")
        elif chosen not in asked:
            errors.append(f"asked for {package} in {asked}, got {chosen}")

    for package, version in pins.items():
        for requirement in _declared_requirements(graph, package, version):
            pinned = pins.get(requirement.name)
            if pinned is None:
                errors.append(f"{package} {version} requires {requirement}, absent")
            elif not requirement.specifier.contains(pinned):
                errors.append(
                    f"{package} {version} requires {requirement}, got {pinned}"
                )
    return errors


def test_look_ahead_preserves_validity_and_solvability() -> None:
    solvable = 0
    rejected_on = 0
    root_rejections = 0
    for seed in range(SEEDS):
        graph = _build_graph(seed)
        with_look_ahead = _resolve(graph, _CountingProvider)
        without_look_ahead = _resolve(graph, _NoLookAheadProvider)

        assert (with_look_ahead.pins is None) == (without_look_ahead.pins is None), (
            f"seed {seed}: look-ahead changed solvability, "
            f"with={with_look_ahead.pins} without={without_look_ahead.pins}"
        )
        for label, run in (("with", with_look_ahead), ("without", without_look_ahead)):
            if run.pins is None:
                continue
            errors = _closure_errors(graph, run.pins)
            assert not errors, f"seed {seed}, {label} look-ahead: {errors}"

        if with_look_ahead.pins is not None:
            solvable += 1
        if with_look_ahead.rejections:
            rejected_on += 1
        root_rejections += with_look_ahead.root_rejections
        assert without_look_ahead.rejections == 0, f"seed {seed}: scan still rejected"

    # A graph the scan never rejects on is the same resolve twice, so the corpus
    # has to reach the rejection path to be checking anything.  Root-requirement
    # rejections are counted apart because they are the permanent ones.
    assert rejected_on > SEEDS // 10, rejected_on
    assert root_rejections > 0

    # Both verdicts have to appear, or one of them is going untested.
    assert 0 < solvable < SEEDS, solvable
