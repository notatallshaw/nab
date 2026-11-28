"""Exercise release lookahead and its handoff to precise file selection."""

from __future__ import annotations

import pytest
from packaging.ranges import VersionRange
from packaging.requirements import Requirement
from packaging.version import Version

from nab_candidates._compat import override
from nab_candidates.preference import IncompletePreferenceError
from nab_candidates.releases import PreparedVersionResolution
from nab_candidates.versions import (
    PreparedVersionCatalogue,
    VersionCandidate,
    VersionCandidateData,
)
from nab_resolver.errors import (
    ResolutionError,
    ResolutionInvariantError,
    ResolutionLimitError,
)


class File(VersionCandidate):
    __slots__ = ("filename",)
    __record_compare_fields__ = ("package", "version", "withdrawn", "label", "filename")

    def __init__(
        self,
        package: str,
        version: str,
        filename: str = "wheel",
        *,
        withdrawn: bool = False,
    ) -> None:
        super().__init__(package, Version(version), withdrawn=withdrawn)
        self.filename = filename

    @property
    @override
    def base(self) -> str:
        return self.package.partition("[")[0]


class Wheels(PreparedVersionCatalogue[File, VersionCandidateData]):
    range_type = VersionRange

    def __init__(self, *files: File) -> None:
        super().__init__(unavailable_reason="wheel metadata is unavailable")
        self.metadata: dict[File, VersionCandidateData | None] = {
            f: VersionCandidateData({}, ()) for f in files
        }
        self.reads: list[File] = []

    @staticmethod
    @override
    def base_name(package: str) -> str:
        return package.partition("[")[0]

    @staticmethod
    @override
    def base_candidate(candidate: File) -> File:
        return File(
            candidate.base,
            candidate.label,
            candidate.filename,
            withdrawn=candidate.withdrawn,
        )

    @staticmethod
    @override
    def pin_project(pin: Requirement) -> str:
        return pin.name

    @override
    def read_choices(self, package: str) -> tuple[File, ...]:
        return tuple(file for file in self.metadata if file.package == package)

    @override
    def read_candidate(self, candidate: File) -> VersionCandidateData | None:
        self.reads.append(candidate)
        return self.metadata[candidate]


def test_one_live_release_needs_no_separate_metadata_decision() -> None:
    wheel = File("lib", "1")
    catalogue = Wheels(wheel)
    session = PreparedVersionResolution.from_catalogue(
        catalogue, {"lib": VersionRange.full()}, (), {}, try_live=True, max_rounds=2
    )

    assert session.solve().candidates == {"lib": wheel}
    assert catalogue.reads == [wheel]


def test_another_wheel_can_supply_metadata_at_the_same_release() -> None:
    broken, usable = File("lib", "1", "broken"), File("lib", "1", "usable")
    catalogue = Wheels(broken, usable)
    catalogue.metadata[broken] = None
    session = PreparedVersionResolution.from_catalogue(
        catalogue, {"lib": VersionRange.full()}, (), {}, try_live=True
    )

    assert session.solve().candidates == {"lib": usable}
    assert catalogue.reads == [broken, usable]


def test_extra_metadata_waits_for_its_exact_base_wheel() -> None:
    extra, base, library = File("app[x]", "1"), File("app", "1"), File("lib", "2")
    catalogue = Wheels(extra, base, library)
    catalogue.metadata[extra] = VersionCandidateData({"lib": VersionRange.full()}, ())
    session = PreparedVersionResolution.from_catalogue(
        catalogue, {"app[x]": VersionRange.full()}, (), {}, try_live=True
    )
    result = session.solve()

    assert result.candidates == {"app[x]": extra, "app": base, "lib": library}
    assert set(result.edges) == {("app[x]", "app"), ("app[x]", "lib")}
    assert catalogue.reads.index(base) < catalogue.reads.index(extra)


def test_incompatible_base_does_not_read_the_extra_wheel() -> None:
    extra, base = File("app[x]", "1"), File("app", "2")
    catalogue = Wheels(extra, base)
    session = PreparedVersionResolution.from_catalogue(
        catalogue,
        {"app[x]": VersionRange.full(), "app": VersionRange.singleton(base.version)},
        (),
        {},
        try_live=True,
    )

    with pytest.raises(ResolutionError):
        session.solve()

    assert extra not in catalogue.reads


def test_original_parent_pin_can_require_a_withdrawn_release() -> None:
    newer, older = File("app", "2"), File("app", "1")
    yanked, live = File("lib", "2", withdrawn=True), File("lib", "1")
    pin = Requirement("lib==2")
    host = Wheels(newer, older, yanked, live)
    host.metadata[newer] = VersionCandidateData(
        {"lib": pin.specifier.to_range()}, (pin,)
    )
    host.metadata[older] = VersionCandidateData(
        {"lib": VersionRange.singleton(live.version)}, ()
    )
    session = PreparedVersionResolution.from_catalogue(
        host, {"app": VersionRange.full()}, (), {}, try_live=True
    )

    model = session.solve()

    assert model.candidates == {"app": newer, "lib": yanked}
    assert model.admission_sources == {"lib": ("app==2 requires lib==2",)}


def test_unavailable_file_with_no_alternative_is_incomplete() -> None:
    missing = File("lib", "1")
    host = Wheels(missing)
    host.unavailable.add(missing)

    with pytest.raises(IncompletePreferenceError):
        PreparedVersionResolution.from_catalogue(
            host, {"lib": VersionRange.full()}, (), {}, try_live=True
        ).solve()


def test_release_and_file_queries_share_the_round_budget() -> None:
    app = File("app", "1")
    lib = File("lib", "1")
    host = Wheels(app, lib)
    host.metadata[app] = VersionCandidateData({"lib": VersionRange.full()}, ())
    session = PreparedVersionResolution.from_catalogue(
        host, {"app": VersionRange.full()}, (), {}, try_live=True, max_rounds=1
    )

    with pytest.raises(ResolutionLimitError):
        session.solve()

    assert session.remaining_rounds == 0


def test_backtracking_can_use_a_different_wheel_at_the_preferred_release() -> None:
    wheel = File("app", "2", "first")
    alternate = File("app", "2", "second")
    older = File("app", "1")
    plugin = File("plugin", "1")
    host = Wheels(wheel, alternate, older, plugin)
    host.metadata[wheel] = VersionCandidateData({"plugin": VersionRange.full()}, ())
    host.metadata[plugin] = VersionCandidateData({"missing": VersionRange.full()}, ())
    session = PreparedVersionResolution.from_catalogue(
        host, {"app": VersionRange.full()}, (), {}, try_live=True
    )

    assert session.solve().candidates == {"app": alternate}


def test_root_pin_can_require_a_withdrawn_file() -> None:
    wheel = File("lib", "1", withdrawn=True)
    pin = Requirement("lib==1")
    result = PreparedVersionResolution.from_catalogue(
        Wheels(wheel), {"lib": pin.specifier.to_range()}, (pin,), {}, try_live=True
    ).solve()

    assert result.candidates == {"lib": wheel}
    assert result.admission_sources["lib"]


def test_unpinned_yank_is_skipped_before_reading_metadata() -> None:
    old = File("lib", "1", withdrawn=True)
    host = Wheels(old)
    with pytest.raises(ResolutionError):
        PreparedVersionResolution.from_catalogue(
            host, {"lib": VersionRange.full()}, (), {}, try_live=True
        ).solve()

    assert not host.reads


def test_empty_requirements_use_one_query() -> None:
    session = PreparedVersionResolution.from_catalogue(
        Wheels(), {}, (), {}, try_live=True, max_rounds=1
    )

    assert session.solve().candidates == {}
    assert session.remaining_rounds == 0


def test_whole_plan_may_refuse_the_first_file() -> None:
    newer, older = File("lib", "2"), File("lib", "1")
    seen: list[File] = []
    result = PreparedVersionResolution.from_catalogue(
        Wheels(newer, older),
        {"lib": VersionRange.full()},
        (),
        {},
        selection_policy=lambda selected: selected["lib"] == older,
        on_candidate_considered=seen.append,
        try_live=True,
    ).solve()

    assert result.candidates == {"lib": older}
    assert list(dict.fromkeys(seen)) == [newer, older]


def test_exact_dependency_uses_the_required_file_before_index_metadata() -> None:
    app = File("app", "1")
    index, source = File("lib", "1", "index"), File("lib", "1", "source")
    host = Wheels(app, index, source)
    host.metadata[app] = VersionCandidateData({"lib": VersionRange.full()}, ())
    result = PreparedVersionResolution.from_catalogue(
        host,
        {"app": VersionRange.full()},
        (),
        {},
        exact_dependencies=lambda choice: (source,) if choice is app else (),
        try_live=True,
    ).solve()

    assert result.candidates == {"app": app, "lib": source}
    assert index not in host.reads


def test_exact_file_requires_a_matching_dependency_declaration() -> None:
    app, child = File("app", "1"), File("lib", "2")
    session = PreparedVersionResolution.from_catalogue(
        Wheels(app, child),
        {"app": VersionRange.full()},
        (),
        {},
        exact_dependencies=lambda choice: (child,) if choice is app else (),
        try_live=True,
    )

    with pytest.raises(ResolutionInvariantError, match="no prepared dependency edge"):
        session.solve()


def test_extra_can_bind_its_selected_base_file() -> None:
    base, extra = File("app", "1"), File("app[x]", "1")
    host = Wheels(base, extra)
    host.metadata[extra] = VersionCandidateData({"app": VersionRange.full()}, ())
    session = PreparedVersionResolution.from_catalogue(
        host,
        {"app[x]": VersionRange.full()},
        (),
        {},
        exact_dependencies=lambda choice: (base,) if choice == extra else (),
        try_live=True,
    )

    assert session.solve().candidates == {"app[x]": extra, "app": base}
    assert host.reads == [base, extra]


def test_root_bound_skips_incompatible_release_files() -> None:
    newer, older = File("app", "2"), File("app", "1")
    rejected = File("app", "2", "rejected")
    renderer = File("renderer", "1")
    host = Wheels(newer, rejected, older, renderer)
    host.facts[rejected] = None
    host.metadata[newer] = VersionCandidateData(
        {"renderer": VersionRange.singleton(Version("2"))}, ()
    )
    session = PreparedVersionResolution.from_catalogue(
        host,
        {
            "app": VersionRange.full(),
            "renderer": VersionRange.singleton(renderer.version),
        },
        (),
        {},
        try_live=True,
    )

    assert session.solve().candidates == {"app": older, "renderer": renderer}
    assert rejected not in host.reads


def test_repeated_dependency_conflict_reconsiders_the_selected_release() -> None:
    preferred, compatible = File("runtime", "2"), File("runtime", "1")
    plugins = [File("plugin", str(version)) for version in range(1, 13)]
    catalogue = Wheels(preferred, compatible, *plugins)
    for plugin in plugins:
        catalogue.metadata[plugin] = VersionCandidateData(
            {"runtime": VersionRange.singleton(compatible.version)}, ()
        )
    session = PreparedVersionResolution.from_catalogue(
        catalogue,
        {"runtime": VersionRange.full(), "plugin": VersionRange.full()},
        (),
        {},
        try_live=True,
    )

    assert session.solve().candidates == {"runtime": compatible, "plugin": plugins[-1]}
    assert set(plugins).intersection(catalogue.reads) == set(plugins[-4:])


def test_extra_requires_its_own_file_when_base_versions_are_equal() -> None:
    first, matching = File("app", "1", "first"), File("app", "1", "matching")
    extra = File("app[x]", "1", "matching")
    host = Wheels(extra, first, matching)
    session = PreparedVersionResolution.from_catalogue(
        host, {"app[x]": VersionRange.full()}, (), {}, try_live=True
    )

    assert session.solve().candidates == {"app": matching, "app[x]": extra}


def test_conflicting_wheel_does_not_discard_its_same_release_sibling() -> None:
    first, second = File("app", "1", "first"), File("app", "1", "second")
    library = File("lib", "1")
    host = Wheels(first, second, library)
    host.metadata[first] = VersionCandidateData(
        {"lib": VersionRange.singleton(Version("2"))}, ()
    )
    session = PreparedVersionResolution.from_catalogue(
        host,
        {"app": VersionRange.full(), "lib": VersionRange.singleton(library.version)},
        (),
        {},
        try_live=True,
    )

    assert session.solve().candidates == {"app": second, "lib": library}


def test_incompatible_plugin_releases_leave_the_runtime_conflict_explainable() -> None:
    runtime = File("runtime", "1")
    first, second = File("plugin", "1"), File("plugin", "2")
    host = Wheels(runtime, first, second)
    for plugin in (first, second):
        host.metadata[plugin] = VersionCandidateData(
            {"runtime": VersionRange.singleton(Version("2"))}, ()
        )
    session = PreparedVersionResolution.from_catalogue(
        host,
        {
            "plugin": VersionRange.full(),
            "runtime": VersionRange.singleton(runtime.version),
        },
        (),
        {},
        try_live=True,
    )

    with pytest.raises(ResolutionError) as failure:
        session.solve()

    assert failure.value.incompatibility is not None
    assert {first, second}.issubset(host.reads)


def test_constraint_can_hide_every_live_release() -> None:
    wheel = File("lib", "1")
    host = Wheels(wheel)
    session = PreparedVersionResolution.from_catalogue(
        host,
        {"lib": VersionRange.full()},
        (),
        {"lib": VersionRange.singleton(Version("2"))},
        try_live=True,
    )

    with pytest.raises(ResolutionError) as failure:
        session.solve()

    assert failure.value.incompatibility is not None
    assert not host.reads
