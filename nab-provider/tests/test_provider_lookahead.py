"""Provider look-ahead, blocker recording and decision priorities."""

from __future__ import annotations

import threading

import pytest
from packaging.ranges import VersionRange
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from nab_provider._provider.lookahead import DepRangeUnion
from nab_provider.provider import Provider
from nab_provider.records import WheelFile
from nab_provider.testing import make_coordinator, make_metadata, make_sdist, make_wheel
from nab_resolver.errors import ResolutionError
from nab_resolver.resolver import Resolver
from nab_resolver.types import Incompatibility, IncompatibilityCause, Term

from .provider_fixtures import PYTHON_312 as _PY312
from .provider_fixtures import short_reason

V = Version


def _done_event() -> threading.Event:
    """Return an already-set Event."""
    ev = threading.Event()
    ev.set()
    return ev


class TestLookAhead:
    def test_skips_version_conflicting_with_root(self) -> None:
        """choose_version skips candidates whose deps conflict with root reqs."""
        wheels = [make_wheel("2.0"), make_wheel("1.0")]
        meta_v2 = (
            "Metadata-Version: 2.1\nName: foo\nVersion: 2.0\nRequires-Dist: bar>=5.0\n"
        )
        meta_v1 = (
            "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\nRequires-Dist: bar>=1.0\n"
        )
        coordinator = make_coordinator(
            wheels,
            metadata_by_version={"2.0": meta_v2, "1.0": meta_v1},
            package="foo",
        )

        root_reqs = {"bar": SpecifierSet("<2.0").to_range()}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        spec = SpecifierSet("")
        assert provider.choose_version("foo", spec.to_range()) == V("1.0")

    def test_no_root_requirements_skips_nothing(self) -> None:
        """Without root_requirements, look-ahead is inactive."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(coordinator)
        spec = SpecifierSet("")
        assert provider.choose_version("foo", spec.to_range()) == V("1.0")

    def test_unrelated_deps_pass_look_ahead(self) -> None:
        """Deps on packages not in root requirements pass look-ahead."""
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=(
                "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n"
                "Requires-Dist: unknown-pkg>=5.0\n"
            ),
            package="foo",
        )
        root_reqs = {"bar": SpecifierSet(">=1.0").to_range()}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        spec = SpecifierSet("")
        assert provider.choose_version("foo", spec.to_range()) == V("1.0")

    def test_all_versions_filtered_returns_none(self) -> None:
        """Returns None when every candidate conflicts with root reqs."""
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=(
                "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n"
                "Requires-Dist: bar>=5.0\n"
            ),
            package="foo",
        )
        root_reqs = {"bar": SpecifierSet("<2.0").to_range()}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        spec = SpecifierSet("")
        assert provider.choose_version("foo", spec.to_range()) is None

    def test_root_rejection_queues_its_own_clause(self) -> None:
        """A root-requirement rejection bans just the versions it rejected.

        Look-ahead rejects the candidate before it is decided, so the
        dependency clause that would explain the rejection is never added.
        """
        coordinator = make_coordinator(
            [make_wheel("2.0"), make_wheel("1.0")],
            metadata_by_version={
                "2.0": (
                    "Metadata-Version: 2.1\nName: foo\nVersion: 2.0\n"
                    "Requires-Dist: bar>=5.0\n"
                ),
                "1.0": "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n",
            },
            package="foo",
        )
        root_range = SpecifierSet("<2.0").to_range()
        provider = Provider(
            coordinator, target=_PY312, root_requirements={"bar": root_range}
        )
        assert provider.choose_version("foo", SpecifierSet("").to_range()) == V("1.0")

        (clause,) = provider.consume_pending_clauses()
        assert clause.cause is IncompatibilityCause.NO_VERSIONS
        (term,) = clause.terms
        assert term.package == "foo"
        assert term.is_positive()
        assert V("2.0") in term.constraint
        assert V("1.0") not in term.constraint

    def test_metadata_rejection_queues_its_own_clause(self) -> None:
        """A version whose metadata will not read is banned the same way."""
        coordinator = make_coordinator(
            [make_wheel("2.0"), make_wheel("1.0")],
            metadata_by_version={
                "2.0": "Metadata-Version: 2.1\nName: foo\nVersion: nope\n",
                "1.0": "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n",
            },
            package="foo",
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            root_requirements={"foo": VersionRange.full(admit_arbitrary=False)},
        )
        assert provider.choose_version("foo", SpecifierSet("").to_range()) == V("1.0")

        (clause,) = provider.consume_pending_clauses()
        assert clause.cause is IncompatibilityCause.NO_VERSIONS
        (term,) = clause.terms
        assert term.package == "foo"
        assert V("2.0") in term.constraint
        assert V("1.0") not in term.constraint

    def test_cached_deps_used_for_look_ahead(self) -> None:
        """Look-ahead uses cached deps without re-fetching."""
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=(
                "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n"
                "Requires-Dist: bar>=5.0\n"
            ),
            package="foo",
        )
        root_reqs = {"bar": SpecifierSet("<2.0").to_range()}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        provider.get_dependencies("foo", V("1.0"))
        spec = SpecifierSet("")
        assert provider.choose_version("foo", spec.to_range()) is None

    def test_batch_with_no_metadata_url(self) -> None:
        """Versions without metadata URLs are skipped by look-ahead.

        Look-ahead catches :class:`MetadataError` so the resolver can
        record "no versions" and backjump.
        """
        wheels = [
            make_wheel("2.0"),
            make_wheel("1.0", has_metadata=False),
        ]
        coordinator = make_coordinator(
            wheels,
            metadata_by_version={
                "2.0": "Metadata-Version: 2.1\nName: foo\nVersion: 2.0\nRequires-Dist: bar>=5.0\n",
            },
            package="foo",
        )
        root_reqs = {"bar": SpecifierSet("<2.0").to_range()}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        spec = SpecifierSet("")
        # v2.0 conflicts with root, v1.0 has no metadata_url and is
        # skipped; choose_version returns ``None`` instead of raising.
        assert provider.choose_version("foo", spec.to_range()) is None

    def test_batch_uses_cached_deps(self) -> None:
        """Batch prefetch skips already-cached versions."""
        wheels = [make_wheel("2.0"), make_wheel("1.0")]
        coordinator = make_coordinator(
            wheels,
            metadata_text=(
                "Metadata-Version: 2.1\nName: foo\nVersion: 2.0\nRequires-Dist: bar>=5.0\n"
            ),
            package="foo",
        )
        root_reqs = {"bar": SpecifierSet("<2.0").to_range()}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        provider.deps_cache[("foo", V("1.0"))] = {}
        spec = SpecifierSet("")
        assert provider.choose_version("foo", spec.to_range()) == V("1.0")

    def test_batch_skips_version_cached_during_fetch(self) -> None:
        """If a version gets cached between submit and result, skip it."""
        wheels = [make_wheel("3.0"), make_wheel("2.0"), make_wheel("1.0")]

        root_reqs = {"bar": SpecifierSet("<2.0").to_range()}

        coordinator = make_coordinator(wheels, package="foo")
        index = coordinator.index

        call_count = [0]

        def _request_metadata(
            pkg: str, ver: str, url: str, metadata_hash: tuple[str, str] | None = None
        ) -> threading.Event:
            call_count[0] += 1
            text = (
                f"Metadata-Version: 2.1\nName: foo\nVersion: {ver}\n"
                "Requires-Dist: bar>=5.0\n"
            )
            if ver == "1.0":
                text = "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n"
            index.store_metadata(pkg, ver, text)
            return _done_event()

        coordinator.override("request_metadata", _request_metadata)

        def _request_metadata_batch(
            items: list[tuple[str, str, str, tuple[str, str] | None]],
        ) -> list[tuple[str, str, threading.Event]]:
            results: list[tuple[str, str, threading.Event]] = []
            for pkg, ver, url, _hash in items:
                call_count[0] += 1
                # On second batch call, inject cache for v1.0
                if call_count[0] == 2:
                    provider.deps_cache[("foo", V("1.0"))] = {}
                text = (
                    f"Metadata-Version: 2.1\nName: foo\nVersion: {ver}\n"
                    "Requires-Dist: bar>=5.0\n"
                )
                if ver == "1.0":
                    text = "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n"
                index.store_metadata(pkg, ver, text)
                results.append((pkg, ver, _done_event()))
            return results

        coordinator.override("request_metadata_batch", _request_metadata_batch)

        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)

        spec = SpecifierSet("")
        assert provider.choose_version("foo", spec.to_range()) == V("1.0")

    def test_batch_skips_multiple_bad_versions(self) -> None:
        """Batch processes multiple chunks to find a compatible version."""
        wheels = [make_wheel(f"{v}.0") for v in range(15, 0, -1)]
        metadata_by_version = {}
        for v in range(15, 0, -1):
            ver = f"{v}.0"
            bar_req = "bar>=0.1" if ver == "1.0" else f"bar>={ver}"
            metadata_by_version[ver] = (
                f"Metadata-Version: 2.1\nName: foo\nVersion: {ver}\n"
                f"Requires-Dist: {bar_req}\n"
            )
        coordinator = make_coordinator(
            wheels,
            metadata_by_version=metadata_by_version,
            package="foo",
        )

        root_reqs = {"bar": SpecifierSet("<2.0").to_range()}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        spec = SpecifierSet("")
        assert provider.choose_version("foo", spec.to_range()) == V("1.0")


class TestDecisionLookAhead:
    """First-candidate decision check with grouped binary clauses."""

    def test_hint_stores_decisions_subset(self) -> None:
        """receive_partial_solution_hint records the decisions snapshot."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(coordinator)
        ranges = {"bar": SpecifierSet(">=1.0").to_range()}
        decisions = {"bar": V("1.0")}
        provider.receive_partial_solution_hint(ranges, decisions)
        assert provider.solution_decisions == decisions
        assert provider.solution_ranges == ranges

    def test_hint_stores_snapshots_without_copying(self) -> None:
        """The caller hands over owned snapshots, so they are stored as-is."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(coordinator)
        ranges = {"bar": SpecifierSet(">=1.0").to_range()}
        decisions = {"bar": V("1.0")}
        provider.receive_partial_solution_hint(ranges, decisions)
        assert provider.solution_ranges is ranges
        assert provider.solution_decisions is decisions

    def test_first_candidate_blocked_by_decision_records_clause(self) -> None:
        """When the newest candidate's deps disagree with a decision, a binary
        clause is queued for the resolver to absorb via consume_pending_clauses."""
        wheels = [make_wheel("2.0"), make_wheel("1.0")]
        meta_v2 = (
            "Metadata-Version: 2.1\nName: foo\nVersion: 2.0\nRequires-Dist: bar>=5.0\n"
        )
        meta_v1 = (
            "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\nRequires-Dist: bar>=1.0\n"
        )
        coordinator = make_coordinator(
            wheels,
            metadata_by_version={"2.0": meta_v2, "1.0": meta_v1},
            package="foo",
        )
        # Root requirements are unused for the rejection itself, but they
        # have to be non-empty so that look-ahead is enabled.
        root_reqs = {"baz": SpecifierSet(">=1.0").to_range()}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        provider.receive_partial_solution_hint({}, {"bar": V("3.0")})
        spec = SpecifierSet("")
        # foo==2.0 conflicts with bar==3.0 (needs >=5.0); foo==1.0 is compatible.
        assert provider.choose_version("foo", spec.to_range()) == V("1.0")
        clauses = provider.consume_pending_clauses()
        assert len(clauses) == 1
        # Each rejected (candidate_pkg, blocker_pkg, blocker_version) group
        # produces one clause: positive(candidate range), positive(blocker==w).
        terms = clauses[0].terms
        assert len(terms) == 2
        packages = {t.package for t in terms}
        assert packages == {"foo", "bar"}

    def test_consume_pending_clauses_drains(self) -> None:
        """consume_pending_clauses returns and clears queued clauses."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(coordinator)
        # Manually populate the queue, then drain.
        clause: Incompatibility[str, Version] = Incompatibility(
            [
                Term("foo", VersionRange.singleton(V("1.0")), positive=True),
                Term("bar", VersionRange.singleton(V("2.0")), positive=True),
            ],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        provider.pending_clauses.append(clause)
        drained = provider.consume_pending_clauses()
        assert drained == [clause]
        assert provider.consume_pending_clauses() == []

    def test_decision_check_disabled_skips_clause(self) -> None:
        """check_decisions=False suppresses the decision-driven rejection path."""
        wheels = [make_wheel("1.0")]
        meta = (
            "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\nRequires-Dist: bar>=5.0\n"
        )
        coordinator = make_coordinator(wheels, metadata_text=meta, package="foo")
        root_reqs = {"baz": SpecifierSet(">=1.0").to_range()}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        provider.receive_partial_solution_hint({}, {"bar": V("3.0")})
        # check_decisions=False ignores the bar==3.0 conflict.
        assert provider._look_ahead_ok("foo", V("1.0"), check_decisions=False)
        assert provider.consume_pending_clauses() == []

    def test_all_first_candidates_blocked_returns_none(self) -> None:
        """When the only candidate is blocked by a decision, returns None
        and queues the binary clause for the resolver."""
        wheels = [make_wheel("1.0")]
        meta = (
            "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\nRequires-Dist: bar>=5.0\n"
        )
        coordinator = make_coordinator(wheels, metadata_text=meta, package="foo")
        root_reqs = {"baz": SpecifierSet(">=1.0").to_range()}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        provider.receive_partial_solution_hint({}, {"bar": V("3.0")})
        spec = SpecifierSet("")
        assert provider.choose_version("foo", spec.to_range()) is None
        clauses = provider.consume_pending_clauses()
        assert len(clauses) == 1

    def test_undecided_positive_range_records_range_block(self) -> None:
        """When the dep has a positive range but no decision, look-ahead
        records a sound range-block clause and rejects the candidate.
        """
        wheels = [make_wheel("1.0")]
        meta = (
            "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\nRequires-Dist: bar>=5.0\n"
        )
        coordinator = make_coordinator(wheels, metadata_text=meta, package="foo")
        root_reqs = {"baz": SpecifierSet(">=1.0").to_range()}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        # bar has a positive range that doesn't intersect bar>=5.0 (the dep)
        # but is not decided yet.
        provider.receive_partial_solution_hint(
            {"bar": SpecifierSet("<2.0").to_range()}, {}
        )
        assert provider._look_ahead_ok("foo", V("1.0")) is False
        # _flush_pending_blocks turns the range block into a clause.
        provider._flush_pending_blocks()
        clauses = provider.consume_pending_clauses()
        assert len(clauses) == 1
        terms = clauses[0].terms
        assert {t.package for t in terms} == {"foo", "bar"}

    def test_self_dependency_rejection_merges_into_one_term(self) -> None:
        """A candidate excluded by its own requirement gets one merged term.

        ``foo`` 1.0 requires ``foo==5.0`` while the solution holds ``foo<2.0``.
        Both terms would name ``foo``, and a clause holds at most one term per
        package, so they merge to the versions the requirement rules out and
        the clause carries the declared range.
        """
        wheels = [make_wheel("1.0")]
        meta = make_metadata("foo", "1.0", "foo==5.0")
        coordinator = make_coordinator(wheels, metadata_text=meta, package="foo")
        provider = Provider(coordinator, target=_PY312)
        provider.receive_partial_solution_hint(
            {"foo": SpecifierSet("<2.0").to_range()}, {}
        )

        assert provider._look_ahead_ok("foo", V("1.0")) is False
        provider._flush_pending_blocks()

        (clause,) = provider.consume_pending_clauses()
        (term,) = clause.terms
        assert term.package == "foo"
        assert term.is_positive()

        assert V("1.0") in term.constraint
        assert V("5.0") not in term.constraint
        assert clause.dependency_range == SpecifierSet("==5.0").to_range()

    def test_self_requirement_selects_the_release_it_admits(self) -> None:
        versions = ("4.0", "3.0", "2.0", "1.0")
        coordinator = make_coordinator(
            [make_wheel(v) for v in versions],
            metadata_by_version={
                v: make_metadata("foo", v, "foo==1.0") for v in versions
            },
            package="foo",
        )
        roots = {"foo": SpecifierSet(">=1").to_range()}
        provider = Provider(coordinator, target=_PY312, root_requirements=roots)
        resolver = Resolver(provider, range_type=VersionRange, root_version="0")

        pins = resolver.resolve(roots)

        assert pins == {"foo": V("1.0")}
        assert resolver.stats.conflicts == 0

    def test_self_rejections_leave_a_filtered_release_out_of_the_clause(self) -> None:
        versions = ("4.0", "3.0", "2.0", "1.0")
        coordinator = make_coordinator(
            [
                make_wheel(v, requires_python=">=3.13" if v == "3.0" else None)
                for v in versions
            ],
            metadata_by_version={
                v: make_metadata("foo", v, "foo==1.0") for v in versions if v != "3.0"
            },
            package="foo",
        )
        requirement = SpecifierSet(">=1").to_range()
        provider = Provider(
            coordinator, target=_PY312, root_requirements={"foo": requirement}
        )

        chosen = provider.choose_version("foo", requirement)
        (clause,) = provider.consume_pending_clauses()
        (term,) = clause.terms

        assert chosen == V("1.0")
        assert term.package == "foo"
        assert {v for v in versions if V(v) in term.constraint} == {"4.0", "2.0"}
        assert clause.dependency_range == SpecifierSet("==1.0").to_range()

    def test_self_dependency_report_names_the_edge(self) -> None:
        """The report names both versions in the self-dependency.

        ``proj`` 3.0 requires ``proj==2.0`` while the solution holds ``>2.0``
        from ``app``, so look-ahead rejects it.  Two positive terms for
        ``proj`` would print as ``proj`` being incompatible with itself.
        """
        coordinator = make_coordinator(
            listings={
                "app": [make_wheel("1.0")],
                "proj": [make_wheel("3.0"), make_wheel("2.0")],
            },
            metadata_by_version={
                "1.0": make_metadata("app", "1.0", "proj>2.0"),
                "3.0": make_metadata("proj", "3.0", "proj==2.0"),
                "2.0": make_metadata("proj", "2.0", "proj==2.0"),
            },
        )
        root_reqs = {"app": VersionRange.full(admit_arbitrary=False)}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        resolver = Resolver(
            provider,
            range_type=VersionRange,
            root_version="0",
            format_range=provider.format_range,
        )
        with pytest.raises(ResolutionError) as exc_info:
            resolver.resolve(dict(root_reqs))

        lines = str(exc_info.value).splitlines()
        assert "because proj >=3.0 depends on proj ==2.0" in lines
        assert not any(
            "incompatible with" in line and line.count("proj") > 1 for line in lines
        )

        assert short_reason(provider, "proj") == (
            "every version in range needs proj in ==2.0, but the resolve holds proj in >2.0"
        )

    def test_flush_widens_terms_onto_listed_gaps(self) -> None:
        """Consecutive rejected candidates coalesce into one segment and the
        blocker covers every version the scanned candidates exclude."""
        foo_wheels = [make_wheel(v) for v in ("3.0", "2.0", "1.0", "0.5")]
        bar_wheels = [make_wheel(v) for v in ("5.0", "3.0", "1.0")]
        meta_template = "Metadata-Version: 2.1\nName: foo\nVersion: {ver}\nRequires-Dist: bar>=5.0\n"
        coordinator = make_coordinator(
            listings={"foo": foo_wheels, "bar": bar_wheels},
            metadata_by_version={
                v: meta_template.format(ver=v) for v in ("3.0", "2.0", "1.0", "0.5")
            },
        )
        provider = Provider(coordinator, target=_PY312)
        provider.fetch_versions("bar")
        provider.receive_partial_solution_hint({}, {"bar": V("3.0")})
        assert provider.choose_version("foo", SpecifierSet(">=1.0").to_range()) is None
        clauses = provider.consume_pending_clauses()
        assert len(clauses) == 1
        candidate, blocker = clauses[0].terms
        assert candidate.package == "foo"
        assert isinstance(candidate.constraint, VersionRange)
        assert len(candidate.constraint._bounds) == 1
        assert V("1.0") in candidate.constraint
        assert V("2.0") in candidate.constraint
        assert V("3.0") in candidate.constraint
        assert V("0.5") not in candidate.constraint
        assert blocker.package == "bar"
        assert V("3.0") in blocker.constraint
        assert V("4.0") in blocker.constraint
        # Every scanned candidate requires bar>=5.0, so bar 1.0 blocks them
        # all just as bar 3.0 does; bar 5.0 satisfies them and stays out.
        assert V("1.0") in blocker.constraint
        assert V("5.0") not in blocker.constraint

    def test_flush_widening_keeps_a_live_hole_version_out(self) -> None:
        """A still-selectable version inside a hole of the asked range stays
        out of the widened union, so the candidates keep separate segments."""
        foo_wheels = [make_wheel(v) for v in ("3.0", "2.0", "1.0", "0.5")]
        bar_wheels = [make_wheel(v) for v in ("5.0", "3.0", "1.0")]
        meta_template = "Metadata-Version: 2.1\nName: foo\nVersion: {ver}\nRequires-Dist: bar>=5.0\n"
        coordinator = make_coordinator(
            listings={"foo": foo_wheels, "bar": bar_wheels},
            metadata_by_version={
                v: meta_template.format(ver=v) for v in ("3.0", "2.0", "1.0", "0.5")
            },
        )
        provider = Provider(coordinator, target=_PY312)
        provider.fetch_versions("bar")
        provider.receive_partial_solution_hint({}, {"bar": V("3.0")})
        asked = SpecifierSet(">=1.0,!=2.0").to_range()
        assert provider.choose_version("foo", asked) is None
        clauses = provider.consume_pending_clauses()
        assert len(clauses) == 1
        candidate = clauses[0].terms[0]
        assert candidate.package == "foo"
        assert isinstance(candidate.constraint, VersionRange)
        assert len(candidate.constraint._bounds) == 2
        assert V("1.0") in candidate.constraint
        assert V("3.0") in candidate.constraint
        assert V("2.0") not in candidate.constraint
        assert V("0.5") not in candidate.constraint

    def test_flush_widens_range_block_candidates_only(self) -> None:
        """Range-keyed flush widens the candidate union; the blocker term
        keeps the captured positive-range object."""
        wheels = [make_wheel(v) for v in ("3.0", "2.0", "1.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator, target=_PY312)
        provider.fetch_versions("foo")
        blocker_range = SpecifierSet("<2.0").to_range()
        provider.pending_range_blocks[("foo", "bar", blocker_range)].extend(
            [V("2.0"), V("3.0")]
        )
        provider._flush_pending_blocks()
        clauses = provider.consume_pending_clauses()
        assert len(clauses) == 1
        candidate, blocker = clauses[0].terms
        assert isinstance(candidate.constraint, VersionRange)
        assert len(candidate.constraint._bounds) == 1
        assert V("2.0") in candidate.constraint
        assert V("3.0") in candidate.constraint
        assert V("1.0") not in candidate.constraint
        assert blocker.constraint is blocker_range

    def test_flush_keeps_singletons_when_widening_unavailable(self) -> None:
        """No cached listing: the flushed terms are exact singletons."""
        coordinator = make_coordinator([], package="foo")
        provider = Provider(coordinator)
        provider.pending_blocks[("foo", "bar", V("2.0"))].extend([V("1.0"), V("3.0")])
        provider._flush_pending_blocks()
        clauses = provider.consume_pending_clauses()
        assert len(clauses) == 1
        candidate, blocker = clauses[0].terms
        assert candidate.constraint == (
            VersionRange.singleton(V("1.0")) | VersionRange.singleton(V("3.0"))
        )
        assert blocker.constraint == VersionRange.singleton(V("2.0"))

    def test_blocker_widens_to_the_recorded_dep_range_complement(self) -> None:
        """The blocker term becomes the complement of the scanned candidates'
        dependency ranges, which reaches past the decided version's gap."""
        coordinator = make_coordinator(
            listings={
                "foo": [make_wheel(v) for v in ("11.0", "10.0")],
                "bar": [make_wheel(v) for v in ("9.0", "4.0", "2.0", "0.1")],
            },
            metadata_by_version={
                "10.0": make_metadata("foo", "10.0", "bar>=4.0"),
                "11.0": make_metadata("foo", "11.0", "bar>=9.0"),
            },
        )
        provider = Provider(coordinator, target=_PY312)
        provider.fetch_versions("bar")
        provider.receive_partial_solution_hint({}, {"bar": V("2.0")})
        assert provider.choose_version("foo", SpecifierSet(">=1.0").to_range()) is None
        clauses = provider.consume_pending_clauses()
        assert len(clauses) == 1
        blocker = clauses[0].terms[1]
        assert blocker.package == "bar"
        # The decided version, by construction outside every recorded range.
        assert V("2.0") in blocker.constraint
        # A listed version no candidate would have accepted either, and one
        # the decided version's neighbor gap leaves out.
        assert V("0.1") in blocker.constraint
        gap = provider.widen_decision("bar", V("2.0"))
        assert gap is not None
        assert V("0.1") not in gap
        # Versions some candidate accepts stay out.
        assert V("4.0") not in blocker.constraint
        assert V("9.0") not in blocker.constraint

    def test_blocker_membership_keeps_out_versions_a_candidate_accepts(self) -> None:
        """Candidates rejecting the blocker from opposite sides leave a bounded
        blocker term: everything between their ranges, and nothing inside."""
        coordinator = make_coordinator(
            listings={
                "foo": [make_wheel(v) for v in ("11.0", "10.0")],
                "bar": [make_wheel(v) for v in ("9.0", "4.0", "2.0", "0.1")],
            },
            metadata_by_version={
                "10.0": make_metadata("foo", "10.0", "bar<1.0"),
                "11.0": make_metadata("foo", "11.0", "bar>=9.0"),
            },
        )
        provider = Provider(coordinator, target=_PY312)
        provider.fetch_versions("bar")
        provider.receive_partial_solution_hint({}, {"bar": V("2.0")})
        assert provider.choose_version("foo", SpecifierSet(">=1.0").to_range()) is None
        clauses = provider.consume_pending_clauses()
        blocker = clauses[0].terms[1]
        assert isinstance(blocker.constraint, VersionRange)
        assert len(blocker.constraint._bounds) == 1
        assert V("2.0") in blocker.constraint
        assert V("4.0") in blocker.constraint
        assert V("0.1") not in blocker.constraint
        assert V("9.0") not in blocker.constraint

    def test_blocker_membership_opens_no_admission_surface(self) -> None:
        """The complement inherits no arbitrary-string admission and drops the
        recorded ranges' pre-release opt-in region."""
        assert SpecifierSet(">=9.0rc1").to_range()._pre_region != ()
        coordinator = make_coordinator(
            listings={
                "foo": [make_wheel(v) for v in ("11.0", "10.0")],
                "bar": [make_wheel(v) for v in ("9.0", "2.0", "0.1")],
            },
            metadata_by_version={
                "10.0": make_metadata("foo", "10.0", "bar>=9.0rc1"),
                "11.0": make_metadata("foo", "11.0", "bar>=9.0rc1"),
            },
        )
        provider = Provider(coordinator, target=_PY312)
        provider.fetch_versions("bar")
        provider.receive_partial_solution_hint({}, {"bar": V("2.0")})
        assert provider.choose_version("foo", SpecifierSet(">=1.0").to_range()) is None
        clauses = provider.consume_pending_clauses()
        blocker = clauses[0].terms[1]
        assert isinstance(blocker.constraint, VersionRange)
        assert V("2.0") in blocker.constraint
        assert blocker.constraint._pre_region == ()
        assert blocker.constraint._admit_arbitrary is False
        assert "wat" not in blocker.constraint

    def test_blocker_keeps_the_gap_without_recorded_dep_ranges(self) -> None:
        """A producer that queues versions without dependency ranges (the
        extras block path) leaves the blocker on its neighbor gap."""
        coordinator = make_coordinator(
            listings={
                "foo": [make_wheel("10.0")],
                "bar": [make_wheel(v) for v in ("4.0", "2.0", "0.1")],
            },
        )
        provider = Provider(coordinator, target=_PY312)
        provider.fetch_versions("foo")
        provider.fetch_versions("bar")
        provider.pending_blocks[("foo", "bar", V("2.0"))].append(V("10.0"))
        provider._flush_pending_blocks()
        blocker = provider.consume_pending_clauses()[0].terms[1]
        assert V("2.0") in blocker.constraint
        assert V("2.5") in blocker.constraint
        assert V("0.1") not in blocker.constraint
        assert V("4.0") not in blocker.constraint

    def test_blocker_keeps_the_gap_when_one_rejection_lacks_a_range(self) -> None:
        """Membership widening needs the whole group: one queued version
        without a recorded range drops the group back to the gap."""
        coordinator = make_coordinator(
            listings={
                "foo": [make_wheel(v) for v in ("11.0", "10.0")],
                "bar": [make_wheel(v) for v in ("4.0", "2.0", "0.1")],
            },
            metadata_by_version={"10.0": make_metadata("foo", "10.0", "bar>=4.0")},
        )
        provider = Provider(coordinator, target=_PY312)
        provider.fetch_versions("bar")
        provider.receive_partial_solution_hint({}, {"bar": V("2.0")})
        assert provider._look_ahead_ok("foo", V("10.0")) is False
        provider.pending_blocks[("foo", "bar", V("2.0"))].append(V("11.0"))
        provider._flush_pending_blocks()
        blocker = provider.consume_pending_clauses()[0].terms[1]
        assert V("2.0") in blocker.constraint
        assert V("0.1") not in blocker.constraint
        assert V("4.0") not in blocker.constraint

    def test_blocker_keeps_the_gap_when_the_union_swallows_the_decision(self) -> None:
        """The containment fence: a recorded union that contains the blocker
        version cannot have produced the rejections, so the gap stands."""
        coordinator = make_coordinator(
            listings={
                "foo": [make_wheel("10.0")],
                "bar": [make_wheel(v) for v in ("4.0", "2.0", "0.1")],
            },
        )
        provider = Provider(coordinator, target=_PY312)
        provider.fetch_versions("foo")
        provider.fetch_versions("bar")
        key = ("foo", "bar", V("2.0"))
        provider.pending_blocks[key].append(V("10.0"))
        provider.pending_decision_dep_ranges[key] = DepRangeUnion(
            1, SpecifierSet(">=0.1").to_range()
        )
        provider._flush_pending_blocks()
        blocker = provider.consume_pending_clauses()[0].terms[1]
        assert V("2.0") in blocker.constraint
        assert V("0.1") not in blocker.constraint
        assert V("4.0") not in blocker.constraint

    def test_decision_blocker_term_is_satisfied_by_the_decision(self) -> None:
        """The blocker term has to be satisfied by the decision's singleton;
        containing the decided version is not enough.

        A ``===`` dependency range separates the two: its complement rejects
        the literal ``1.0.0`` by string, so ``V("1.0")`` reads as contained
        while the singleton is not a subset.  Such a term leaves the clause
        undetermined, which suppresses NO_VERSIONS and livelocks the resolve,
        so the fence declines it and the gap stands.
        """
        coordinator = make_coordinator(
            listings={
                "foo": [make_wheel("10.0")],
                "bar": [make_wheel(v) for v in ("2.0", "1.0")],
            },
        )
        provider = Provider(coordinator, target=_PY312)
        provider.fetch_versions("foo")
        provider.fetch_versions("bar")
        key = ("foo", "bar", V("1.0"))
        provider.pending_blocks[key].append(V("10.0"))
        provider.pending_decision_dep_ranges[key] = DepRangeUnion(
            1, SpecifierSet("===1.0.0").to_range()
        )
        provider._flush_pending_blocks()
        blocker = provider.consume_pending_clauses()[0].terms[1]
        assert blocker.satisfies(VersionRange.singleton(V("1.0")))

    def test_range_block_widens_to_the_membership_complement(self) -> None:
        """The range-keyed blocker term widens the same way, past the captured
        positive range it started from."""
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=make_metadata("foo", "1.0", "bar>=5.0"),
            package="foo",
        )
        root_reqs = {"baz": SpecifierSet(">=1.0").to_range()}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        pos_range = SpecifierSet("<2.0").to_range()
        provider.receive_partial_solution_hint({"bar": pos_range}, {})
        assert provider._look_ahead_ok("foo", V("1.0")) is False
        provider._flush_pending_blocks()
        blocker = provider.consume_pending_clauses()[0].terms[1]
        assert blocker.constraint is not pos_range
        assert V("1.0") in blocker.constraint
        assert V("3.0") in blocker.constraint
        assert V("5.0") not in blocker.constraint

    def test_range_block_keeps_its_range_when_membership_misses_it(self) -> None:
        """The containment fence on the range family: a recorded union that
        overlaps the positive range leaves the captured object in place."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(coordinator, target=_PY312)
        blocker_range = SpecifierSet("<2.0").to_range()
        key = ("foo", "bar", blocker_range)
        provider.pending_range_blocks[key].append(V("1.0"))
        provider.pending_range_dep_ranges[key] = DepRangeUnion(
            1, SpecifierSet(">=1.0").to_range()
        )
        provider._flush_pending_blocks()
        blocker = provider.consume_pending_clauses()[0].terms[1]
        assert blocker.constraint is blocker_range

    def test_lookahead_clause_prunes_the_other_blocker_versions(self) -> None:
        """End to end: the widened clause rules out every bar version at once,
        so the scan of foo runs once instead of once per bar decision."""
        coordinator = make_coordinator(
            listings={
                "foo": [make_wheel(v) for v in ("12.0", "11.0", "10.0")],
                "bar": [make_wheel(v) for v in ("2.0", "1.0")],
            },
            metadata_by_version={
                "10.0": make_metadata("foo", "10.0", "bar>=9.0"),
                "11.0": make_metadata("foo", "11.0", "bar>=9.0"),
                "12.0": make_metadata("foo", "12.0", "bar>=9.0"),
                "1.0": make_metadata("bar", "1.0"),
                "2.0": make_metadata("bar", "2.0"),
            },
        )
        root_reqs = {
            "bar": VersionRange.full(admit_arbitrary=False),
            "foo": VersionRange.full(admit_arbitrary=False),
        }
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        resolver = Resolver(provider, range_type=VersionRange, root_version="0")
        with pytest.raises(ResolutionError):
            resolver.resolve(dict(root_reqs))
        # One scan of foo's three versions. A gap-widened blocker names only
        # bar 2.0, so bar 1.0 gets decided next and foo is scanned again.
        assert provider.stats.look_ahead_rejections == 3

    def test_full_resolve_reports_lookahead_clause_as_incompatible(self) -> None:
        """The look-ahead grouped clause renders as an incompatibility.

        Root needs foo and app; foo 1.0 requires lib==9.0 while app 3.0
        requires lib==5.0.  The resolve fails, and the grouped clause that
        rejects app against the decided lib==9.0 carries the blocker term
        positive.  It must render as app being incompatible with lib 9.0, not
        as app depending on it; app's real dependency is lib 5.0.
        """

        def named_wheel(pkg: str, version: str) -> WheelFile:
            return WheelFile(
                filename=f"{pkg}-{version}-py3-none-any.whl",
                url=f"https://example.com/{pkg}-{version}.whl",
                version=version,
                requires_python=None,
                has_metadata=True,
                upload_time=None,
                local_path=None,
            )

        coordinator = make_coordinator(
            listings={
                "foo": [named_wheel("foo", "1.0")],
                "app": [named_wheel("app", "3.0")],
                "lib": [named_wheel("lib", "5.0"), named_wheel("lib", "9.0")],
            },
            metadata_by_version={
                "1.0": make_metadata("foo", "1.0", "lib==9.0"),
                "3.0": make_metadata("app", "3.0", "lib==5.0"),
                "5.0": make_metadata("lib", "5.0"),
                "9.0": make_metadata("lib", "9.0"),
            },
        )
        root_reqs = {
            "foo": VersionRange.full(admit_arbitrary=False),
            "app": VersionRange.full(admit_arbitrary=False),
        }
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        resolver = Resolver(provider, range_type=VersionRange, root_version="0")
        with pytest.raises(ResolutionError) as exc_info:
            resolver.resolve(dict(root_reqs))

        lines = str(exc_info.value).splitlines()
        assert not [
            ln for ln in lines if "app" in ln and "lib" in ln and "depends on" in ln
        ]
        assert any(
            "app" in ln and "lib" in ln and "incompatible with" in ln for ln in lines
        )

    def test_full_resolve_terminates_on_an_arbitrary_equality_blocker(self) -> None:
        """An unsatisfiable ``===`` dependency reports before the iteration cap.

        Both foo versions pin ``bar===1.0.0`` while root holds bar below 2.0,
        so bar decides at 1.0 and the scan rejects both.  The widened blocker
        term has to stay satisfiable by that decision, or the clause never
        asserts and the resolver re-picks foo forever.
        """
        coordinator = make_coordinator(
            listings={
                "foo": [make_wheel("10.0"), make_wheel("9.0")],
                "bar": [make_wheel("2.0"), make_wheel("1.0")],
            },
            metadata_by_version={
                "10.0": make_metadata("foo", "10.0", "bar===1.0.0"),
                "9.0": make_metadata("foo", "9.0", "bar===1.0.0"),
                "2.0": make_metadata("bar", "2.0"),
                "1.0": make_metadata("bar", "1.0"),
            },
        )
        root_reqs = {
            "foo": VersionRange.full(admit_arbitrary=False),
            "bar": SpecifierSet("<2.0").to_range(),
        }
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        resolver = Resolver(provider, range_type=VersionRange, root_version="0")
        with pytest.raises(ResolutionError) as exc_info:
            resolver.resolve(dict(root_reqs))

        assert "exceeded" not in str(exc_info.value)

    def test_full_resolve_report_spells_ranges_as_requirements(self) -> None:
        """A real failure report spells a dependency the way a user wrote it.

        The resolver runs with ``range_type=VersionRange``, which has no
        ``__str__``, so without the hook a ``==V`` dependency shows the debug
        repr and its internal ``AFTER_LOCALS`` boundary sentinel, and an
        unconstrained root requirement shows ``(-inf, +inf)``.
        """

        def named_wheel(pkg: str, version: str) -> WheelFile:
            return WheelFile(
                filename=f"{pkg}-{version}-py3-none-any.whl",
                url=f"https://example.com/{pkg}-{version}.whl",
                version=version,
                requires_python=None,
                has_metadata=True,
                upload_time=None,
                local_path=None,
            )

        coordinator = make_coordinator(
            listings={
                "foo": [named_wheel("foo", "1.0")],
                "app": [named_wheel("app", "3.0")],
                "lib": [named_wheel("lib", "5.0"), named_wheel("lib", "9.0")],
            },
            metadata_by_version={
                "1.0": make_metadata("foo", "1.0", "lib==9.0"),
                "3.0": make_metadata("app", "3.0", "lib==5.0"),
                "5.0": make_metadata("lib", "5.0"),
                "9.0": make_metadata("lib", "9.0"),
            },
        )
        root_reqs = {
            "foo": VersionRange.full(admit_arbitrary=False),
            "app": VersionRange.full(admit_arbitrary=False),
        }
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        resolver = Resolver(
            provider,
            range_type=VersionRange,
            root_version="0",
            format_range=provider.format_range,
        )
        with pytest.raises(ResolutionError) as exc_info:
            resolver.resolve(dict(root_reqs))

        lines = str(exc_info.value).splitlines()
        assert "because all versions of foo depend on lib ==9.0" in lines
        assert "because your project depends on foo" in lines


class TestLookAheadAbort:
    """Look-ahead abort path: when the scan rejects ``_LOOKAHEAD_ABORT_THRESHOLD``
    candidates all blamed on the same ``(blocker_pkg, blocker_version)``, the
    scan returns the first candidate without emitting any clauses so the
    resolver can decide it tentatively and learn the real dep-range clause
    via ``get_dependencies``.
    """

    def _provider(self, root_reqs: dict[str, VersionRange] | None = None) -> Provider:
        coordinator = make_coordinator([], package="foo")
        return Provider(coordinator, root_requirements=root_reqs)

    def test_should_abort_returns_blocker_for_single_decision_blocker(self) -> None:
        provider = self._provider()
        provider.pending_blocks[("foo", "bar", V("1.0"))].extend(
            [V("0.9"), V("0.8"), V("0.7")]
        )
        assert provider._should_abort_lookahead("foo") == ("bar", V("1.0"))

    def test_should_abort_none_when_no_blocks(self) -> None:
        provider = self._provider()
        assert provider._should_abort_lookahead("foo") is None

    def test_should_abort_none_when_two_distinct_blockers(self) -> None:
        provider = self._provider()
        provider.pending_blocks[("foo", "bar", V("1.0"))].append(V("0.9"))
        provider.pending_blocks[("foo", "baz", V("2.0"))].append(V("0.8"))
        assert provider._should_abort_lookahead("foo") is None

    def test_should_abort_none_when_two_blocker_versions(self) -> None:
        provider = self._provider()
        provider.pending_blocks[("foo", "bar", V("1.0"))].append(V("0.9"))
        provider.pending_blocks[("foo", "bar", V("2.0"))].append(V("0.8"))
        assert provider._should_abort_lookahead("foo") is None

    def test_should_abort_none_when_range_block_present(self) -> None:
        provider = self._provider()
        provider.pending_blocks[("foo", "bar", V("1.0"))].append(V("0.9"))
        provider.pending_range_blocks[
            ("foo", "baz", SpecifierSet("<2.0").to_range())
        ].append(V("0.9"))
        assert provider._should_abort_lookahead("foo") is None

    def test_should_abort_none_when_self_block_present(self) -> None:
        provider = self._provider()
        provider.pending_blocks[("foo", "bar", V("1.0"))].append(V("0.9"))
        provider.pending_self_blocks[("foo", SpecifierSet("==5.0").to_range())].append(
            V("0.9")
        )
        assert provider._should_abort_lookahead("foo") is None

    def test_should_abort_none_when_root_block_present(self) -> None:
        provider = self._provider()
        provider.pending_blocks[("foo", "bar", V("1.0"))].append(V("0.9"))
        provider.pending_root_blocks[
            (
                "foo",
                "qux",
                SpecifierSet("==2.0").to_range(),
                SpecifierSet("==1.0").to_range(),
            )
        ].append(V("0.9"))
        assert provider._should_abort_lookahead("foo") is None

    def test_should_abort_none_when_metadata_block_present(self) -> None:
        provider = self._provider()
        provider.pending_blocks[("foo", "bar", V("1.0"))].append(V("0.9"))
        provider.pending_metadata_blocks["foo"][V("0.8")] = "metadata failure"
        assert provider._should_abort_lookahead("foo") is None

    def test_should_abort_ignores_blocks_owned_by_other_packages(self) -> None:
        provider = self._provider()
        provider.pending_blocks[("foo", "bar", V("1.0"))].append(V("0.9"))
        # "other" has range/root/metadata blocks but they belong to a
        # different candidate and must not influence foo's decision.
        provider.pending_blocks[("other", "baz", V("2.0"))].append(V("0.8"))
        provider.pending_range_blocks[
            ("other", "qux", SpecifierSet("<2.0").to_range())
        ].append(V("0.8"))
        provider.pending_metadata_blocks["other"][V("0.5")] = "x"
        assert provider._should_abort_lookahead("foo") == ("bar", V("1.0"))

    def test_discard_drops_only_target_candidate_blocks(self) -> None:
        provider = self._provider()
        provider.pending_blocks[("foo", "bar", V("1.0"))].append(V("0.9"))
        provider.pending_blocks[("other", "baz", V("2.0"))].append(V("0.8"))
        provider._discard_pending_decision_blocks("foo")
        assert ("foo", "bar", V("1.0")) not in provider.pending_blocks
        assert provider.pending_blocks[("other", "baz", V("2.0"))] == [V("0.8")]

    def test_abort_returns_first_candidate_and_emits_no_clauses(self) -> None:
        """End-to-end: a scan that hits the abort threshold returns the
        first candidate, drops its singleton-blocker pending blocks, and
        emits *no* clauses.  The resolver then decides the candidate
        and ``get_dependencies`` produces the actual dep-range clause.
        """
        # Threshold 2 keeps the fixture small; foo has 3 versions all
        # rejected because bar=3.0 conflicts with foo's bar<2.0 dep.
        versions = ["3.0", "2.0", "1.0"]
        wheels = [make_wheel(v) for v in versions]
        meta_template = (
            "Metadata-Version: 2.1\nName: foo\nVersion: {ver}\nRequires-Dist: bar<2.0\n"
        )
        coordinator = make_coordinator(
            wheels,
            metadata_by_version={v: meta_template.format(ver=v) for v in versions},
            package="foo",
        )
        root_reqs = {"foo": VersionRange.full(admit_arbitrary=False)}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        provider.receive_partial_solution_hint({}, {"bar": V("3.0")})
        provider._LOOKAHEAD_ABORT_THRESHOLD = 2  # type: ignore[misc]
        chosen = provider.choose_version("foo", VersionRange.full())
        assert chosen == V("3.0")
        # Aborting look-ahead discards the pending clauses.
        assert provider.consume_pending_clauses() == []

    def test_no_abort_under_threshold(self) -> None:
        """Below the threshold, look-ahead emits the usual singleton-blocker
        clauses and returns ``None`` if no candidate passes.
        """
        versions = ["1.0", "0.9"]
        wheels = [make_wheel(v) for v in versions]
        meta_template = (
            "Metadata-Version: 2.1\nName: foo\nVersion: {ver}\nRequires-Dist: bar<0.5\n"
        )
        coordinator = make_coordinator(
            wheels,
            metadata_by_version={v: meta_template.format(ver=v) for v in versions},
            package="foo",
        )
        root_reqs = {"foo": VersionRange.full(admit_arbitrary=False)}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        provider.receive_partial_solution_hint({}, {"bar": V("3.0")})
        provider._LOOKAHEAD_ABORT_THRESHOLD = 64  # type: ignore[misc]
        # Only 2 candidates so we never cross 64; the scan returns None.
        assert provider.choose_version("foo", VersionRange.full()) is None
        # The 2 rejections still emit a clause via the normal flush path.
        clauses = provider.consume_pending_clauses()
        assert len(clauses) == 1

    def test_abort_queues_force_backtrack_target(self) -> None:
        """The abort path queues the blocker package on
        ``_force_backtrack_targets`` so the resolver can pick it up via
        ``consume_force_backtrack_targets`` and target-back-jump
        immediately instead of paying a natural-path conflict cycle.
        """
        versions = ["3.0", "2.0", "1.0"]
        wheels = [make_wheel(v) for v in versions]
        meta_template = (
            "Metadata-Version: 2.1\nName: foo\nVersion: {ver}\nRequires-Dist: bar<2.0\n"
        )
        coordinator = make_coordinator(
            wheels,
            metadata_by_version={v: meta_template.format(ver=v) for v in versions},
            package="foo",
        )
        root_reqs = {"foo": VersionRange.full(admit_arbitrary=False)}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        provider.receive_partial_solution_hint({}, {"bar": V("3.0")})
        provider._LOOKAHEAD_ABORT_THRESHOLD = 2  # type: ignore[misc]
        provider.choose_version("foo", VersionRange.full())
        assert provider.consume_force_backtrack_targets() == ["bar"]
        # Drained on consume.
        assert provider.consume_force_backtrack_targets() == []

    def test_force_backtrack_refires_up_to_cap(self) -> None:
        """A blocker can drive at most ``_MAX_FORCE_BACKTRACKS_PER_PKG``
        force-backtracks, mirroring uv's repeated ConflictTracker fires.
        After the cap the abort still fires but does not re-queue the
        force-backtrack target.
        """
        versions = ["3.0", "2.0", "1.0"]
        wheels = [make_wheel(v) for v in versions]
        meta_template = (
            "Metadata-Version: 2.1\nName: foo\nVersion: {ver}\nRequires-Dist: bar<2.0\n"
        )
        coordinator = make_coordinator(
            wheels,
            metadata_by_version={v: meta_template.format(ver=v) for v in versions},
            package="foo",
        )
        root_reqs = {"foo": VersionRange.full(admit_arbitrary=False)}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        provider.receive_partial_solution_hint({}, {"bar": V("3.0")})
        provider._LOOKAHEAD_ABORT_THRESHOLD = 2  # type: ignore[misc]
        provider._MAX_FORCE_BACKTRACKS_PER_PKG = 3  # type: ignore[misc]
        for _ in range(3):
            provider.choose_version("foo", VersionRange.full())
            assert provider.consume_force_backtrack_targets() == ["bar"]

        # Fourth abort blaming the same blocker is past the cap; no re-queue.
        provider.choose_version("foo", VersionRange.full())
        assert provider.consume_force_backtrack_targets() == []


class TestConstraintNotBlamedWhenProbeAborts:
    """A transitive-conflict failure must not be blamed on a user constraint
    just because the causation probe's un-narrowed range is large enough to
    trip the look-ahead abort.

    ``record_no_versions`` labels a clause CONSTRAINT only when
    ``has_satisfying_version`` finds a version over the un-narrowed range.  When
    that range holds enough candidates that all conflict with one decided
    dependency, the monolithic-rejection abort returns an optimistic pick; the
    probe must not read that as "the constraint hid a usable version".
    """

    @staticmethod
    def _wheel(name: str, version: str) -> WheelFile:
        return WheelFile(
            filename=f"{name}-{version}-py3-none-any.whl",
            url=f"https://example.com/{name}-{version}-py3-none-any.whl",
            version=version,
            requires_python=None,
            has_metadata=True,
            upload_time=None,
        )

    def _resolve(self, constraint: str, foo_deps: dict[str, str]) -> str:
        listings = {
            "foo": [self._wheel("foo", v) for v in foo_deps],
            "bar": [self._wheel("bar", "90.0"), self._wheel("bar", "50.0")],
            "baz": [self._wheel("baz", "3.0")],
        }
        metadata = {
            v: (
                f"Metadata-Version: 2.1\nName: foo\nVersion: {v}\n"
                f"Requires-Dist: {dep}\n\n"
            )
            for v, dep in foo_deps.items()
        }
        metadata["90.0"] = "Metadata-Version: 2.1\nName: bar\nVersion: 90.0\n\n"
        metadata["50.0"] = "Metadata-Version: 2.1\nName: bar\nVersion: 50.0\n\n"
        metadata["3.0"] = (
            "Metadata-Version: 2.1\nName: baz\nVersion: 3.0\n"
            "Requires-Dist: bar==50.0\n\n"
        )
        coordinator = make_coordinator(listings=listings, metadata_by_version=metadata)
        root_reqs = {
            "foo": VersionRange.full(admit_arbitrary=False),
            "baz": SpecifierSet("==3.0").to_range(),
        }
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        resolver = Resolver(provider, range_type=VersionRange, root_version="0")
        with pytest.raises(ResolutionError) as exc_info:
            resolver.resolve(
                root_reqs, constraints={"foo": SpecifierSet(constraint).to_range()}
            )
        return str(exc_info.value)

    def test_transitive_conflict_not_blamed_on_constraint(self) -> None:
        """Twelve foo versions leave eight in the probe range once the top four
        are excluded, so the abort fires inside the probe.

        Every foo needs ``bar==90.0`` but ``baz`` pins ``bar`` at ``50.0``, so
        the resolve fails on that transitive conflict with or without the
        constraint.  ``foo>=108.0`` clips only the top four versions, which also
        fail, so it is not causal and must not be named.
        """
        message = self._resolve(
            ">=108.0", {f"{100 + i}.0": "bar==90.0" for i in range(12)}
        )
        assert "the user constrained" not in message
        assert "no versions of foo" in message

    def test_constraint_that_hides_a_usable_version_is_still_blamed(self) -> None:
        """The suppression must not hide a usable version.

        ``foo==100.0`` needs ``bar==50.0`` (the pinned version), so it resolves
        without the constraint.  ``foo>=101.0`` excludes it; the probe scans
        past the eight blocked newer versions and still finds it, so the
        constraint keeps the blame.
        """
        foo_deps = {
            f"{100 + i}.0": ("bar==50.0" if i == 0 else "bar==90.0") for i in range(12)
        }
        message = self._resolve(">=101.0", foo_deps)
        assert "the user constrained foo" in message


class TestPrioritize:
    def test_counts_matching_versions(self) -> None:
        """prioritize returns tuple (tier, matching_count, is_base)."""
        wheels = [make_wheel(v) for v in ("1.0", "2.0", "3.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        spec = SpecifierSet(">=2.0")
        assert provider.prioritize("foo", spec.to_range(), {}) == (
            Provider.TIER_NORMAL,
            2,
            True,
        )

    def test_conflict_promoted_gets_higher_priority(self) -> None:
        """Packages with 5+ affected conflicts sort before others."""
        wheels = [make_wheel(v) for v in ("1.0", "2.0", "3.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        spec = SpecifierSet(">=1.0")
        normal = provider.prioritize("foo", spec.to_range(), {})
        promoted = provider.prioritize("foo", spec.to_range(), {"foo": 5})
        assert promoted < normal

    def test_culprit_counts_demote(self) -> None:
        """Persistent culprits are demoted to a tier below normal.

        Mirrors uv's "Package X has too many conflicts (culprit),
        deprioritizing" heuristic so the culprit decides AFTER its
        dependents narrow it.  Affected packages are still promoted
        above normal and culprits are below normal.
        """
        wheels = [make_wheel(v) for v in ("1.0", "2.0", "3.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        spec = SpecifierSet(">=1.0")
        normal = provider.prioritize("foo", spec.to_range(), {}, {})
        culprit = provider.prioritize("foo", spec.to_range(), {}, {"foo": 10})
        affected = provider.prioritize("foo", spec.to_range(), {"foo": 5}, {})
        assert affected < normal
        assert normal < culprit

    def test_culprit_below_threshold_not_demoted(self) -> None:
        """A culprit below the threshold stays in the normal tier."""
        wheels = [make_wheel(v) for v in ("1.0", "2.0", "3.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        spec = SpecifierSet(">=1.0")
        normal = provider.prioritize("foo", spec.to_range(), {}, {})
        below = provider.prioritize("foo", spec.to_range(), {}, {"foo": 2})
        assert below == normal

    def test_tied_culprits_not_demoted(self) -> None:
        """When the top-two culprits are near-tied (gap < threshold), neither demotes."""
        wheels = [make_wheel(v) for v in ("1.0", "2.0", "3.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        spec = SpecifierSet(">=1.0")
        normal = provider.prioritize("foo", spec.to_range(), {}, {})
        # foo's 6 vs bar's 8 vs baz's 5: bar dominates over foo, foo
        # itself stays in normal because foo - max(bar, baz) = -2 < 5.
        not_dominant = provider.prioritize(
            "foo", spec.to_range(), {}, {"foo": 6, "bar": 8, "baz": 5}
        )
        assert not_dominant == normal


class TestPrioritizeMatchingFromIndex:
    def test_matching_count_from_index(self) -> None:
        """prioritize computes matching count from index listing."""
        wheels = [make_wheel(v) for v in ("1.0", "2.0", "3.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        # Don't pre-fill versions_cache; let prioritize pull from index.
        spec = SpecifierSet(">=2.0")
        result = provider.prioritize("foo", spec.to_range(), {})
        assert result == (Provider.TIER_NORMAL, 2, True)

    def test_matching_count_defaults_to_1000(self) -> None:
        """prioritize returns 1000 when listing is not available."""
        coordinator = make_coordinator(None, package="foo")
        provider = Provider(coordinator)
        spec = SpecifierSet(">=1.0")
        result = provider.prioritize("foo", spec.to_range(), {})
        assert result == (Provider.TIER_NORMAL, 1000, True)

    def test_matching_recomputed_after_listing_arrives(self) -> None:
        """The in-flight placeholder is not cached; the next scan recomputes."""
        coordinator = make_coordinator(None, package="foo")
        provider = Provider(coordinator)
        rng = SpecifierSet(">=1.0").to_range()
        provider.begin_decision_scan()
        assert provider.prioritize("foo", rng, {}) == (
            Provider.TIER_NORMAL,
            1000,
            True,
        )
        wheels = [make_wheel(v) for v in ("1.0", "2.0", "3.0")]
        # Store directly into the index in the fetcher thread's format.
        coordinator.index.store_listing("foo", wheels)
        provider.begin_decision_scan()
        assert provider.prioritize("foo", rng, {}) == (
            Provider.TIER_NORMAL,
            3,
            True,
        )
        assert "foo" in provider.versions_cache

    def test_prioritize_uses_versions_cache(self) -> None:
        """prioritize skips index check when versions cache is populated."""
        wheels = [make_wheel(v) for v in ("1.0", "2.0", "3.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        # Pre-populate versions cache directly.
        provider.versions_cache["foo"] = [
            (V("3.0"), make_wheel("3.0")),
            (V("2.0"), make_wheel("2.0")),
            (V("1.0"), make_wheel("1.0")),
        ]
        spec = SpecifierSet(">=2.0")
        result = provider.prioritize("foo", spec.to_range(), {})
        assert result == (Provider.TIER_NORMAL, 2, True)

    def test_prioritize_priority_cache_hit(self) -> None:
        """Repeat prioritize with the same Range object returns the cached tuple."""
        wheels = [make_wheel(v) for v in ("1.0", "2.0", "3.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        spec = SpecifierSet(">=2.0")
        intervals = spec.to_range()
        first = provider.prioritize("foo", intervals, {})
        second = provider.prioritize("foo", intervals, {})
        assert first == second
        assert ("foo", intervals) in [
            (k, v[0]) for k, v in provider.priority_cache.items()
        ]

    def test_priority_cache_hit_skips_the_name_split(self) -> None:
        """A hit answers from the entry's own name, without re-splitting."""
        wheels = [make_wheel(v) for v in ("1.0", "2.0", "3.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        intervals = SpecifierSet(">=1.0").to_range()
        first = provider.prioritize("foo", intervals, {}, {})

        provider._package_parts.clear()

        assert provider.prioritize("foo", intervals, {}, {}) == first
        assert provider._package_parts == {}

    def test_priority_cache_hit_yields_to_force_backtrack(self) -> None:
        """A force-backtrack recorded after the entry was cached still demotes."""
        wheels = [make_wheel(v) for v in ("1.0", "2.0", "3.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        intervals = SpecifierSet(">=1.0").to_range()
        assert provider.prioritize("foo", intervals, {}, {})[0] == Provider.TIER_NORMAL

        provider._force_backtrack_counts["foo"] = 1
        demoted = provider.prioritize("foo", intervals, {}, {})

        assert demoted[0] == Provider.TIER_CULPRIT

    def test_priority_cache_hit_yields_to_dominant_culprit(self) -> None:
        """A package that becomes the dominant culprit after caching still demotes."""
        wheels = [make_wheel(v) for v in ("1.0", "2.0", "3.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        intervals = SpecifierSet(">=1.0").to_range()
        assert provider.prioritize("foo", intervals, {}, {})[0] == Provider.TIER_NORMAL

        demoted = provider.prioritize("foo", intervals, {}, {"foo": 10})

        assert demoted[0] == Provider.TIER_CULPRIT

    def test_priority_cache_hit_yields_to_new_conflict_count(self) -> None:
        """A conflict count that reaches the threshold after caching still promotes."""
        wheels = [make_wheel(v) for v in ("1.0", "2.0", "3.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        intervals = SpecifierSet(">=1.0").to_range()
        assert provider.prioritize("foo", intervals, {}, {})[0] == Provider.TIER_NORMAL

        promoted = provider.prioritize("foo", intervals, {"foo": 5}, {})

        assert promoted[0] == Provider.TIER_AFFECTED

    def test_matching_cache_inner_dict_reused_for_new_range(self) -> None:
        """Two distinct ranges for the same package share the inner dict."""
        wheels = [make_wheel(v) for v in ("1.0", "2.0", "3.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        spec_a = SpecifierSet(">=1.0").to_range()
        spec_b = SpecifierSet(">=2.0").to_range()
        provider.prioritize("foo", spec_a, {})
        # Second call with a different range should populate the same
        # inner dict instead of allocating a new one.
        provider.prioritize("foo", spec_b, {})
        per_pkg = provider.matching_cache["foo"]
        assert spec_a in per_pkg
        assert spec_b in per_pkg

    def test_matching_counts_one_entry_per_file(self) -> None:
        """versions_cache holds a row per file, so a dual-artifact release counts twice."""
        files = [make_wheel("2.0"), make_sdist("2.0"), make_wheel("1.0")]
        coordinator = make_coordinator(files, package="foo")
        provider = Provider(coordinator)
        version_range = SpecifierSet(">=2.0").to_range()
        result = provider.prioritize("foo", version_range, {})
        cached = provider.versions_cache["foo"]
        assert result[1] == sum(1 for v, _ in cached if v in version_range)
        assert result[1] == 2

    def test_matching_counts_an_in_bounds_prerelease(self) -> None:
        """The count admits an in-bounds pre-release the default policy would drop.

        The range's bounds are wider than its pre-release opt-in region, so the
        default policy buffers 2.0a1 behind the matching final release and drops
        it; only the admitting policy counts all three.
        """
        wheels = [make_wheel(v) for v in ("2.0", "2.0a1", "1.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        version_range = SpecifierSet(">=1.0,<3.0").to_range()
        result = provider.prioritize("foo", version_range, {})
        assert V("2.0a1") in version_range
        assert result[1] == 3

    def test_versions_only_cache_hit(self) -> None:
        """Calling versions_only twice returns the same cached list."""
        wheels = [make_wheel(v) for v in ("1.0", "2.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        version_list = provider.fetch_versions("foo")
        first = provider.versions_only("foo", version_list)
        second = provider.versions_only("foo", version_list)
        assert first is second

    def test_versions_only_is_descending_whatever_the_index_order(self) -> None:
        """The provider's version view presents the newest release first."""
        wheels = [make_wheel(v) for v in ("1.0", "3.0", "1.0a1", "2.0", "0.9")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        versions = provider.versions_only("foo", provider.fetch_versions("foo"))
        assert versions == sorted(versions, reverse=True)
        assert versions[0] == V("3.0")

    def test_version_view_filter_preserves_member_order(self) -> None:
        """Filtering the view keeps exactly its admitted versions in view order."""
        wheels = [make_wheel(v) for v in ("0.9", "1.0a1", "1.0", "1.5", "2.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        versions = provider.versions_only("foo", provider.fetch_versions("foo"))
        for spec in ("", ">=1.0", ">=1.0,<2.0", "!=1.0", ">=1.0a1", "===1.0"):
            version_range = SpecifierSet(spec).to_range()
            expected = [
                version
                for version in versions
                if version_range.contains(version, prereleases=True)
            ]
            assert list(version_range.filter(versions, prereleases=True)) == expected

    def test_wheel_by_version_cache_hit(self) -> None:
        """Calling _wheel_by_version twice returns the same cached dict."""
        wheels = [make_wheel(v) for v in ("1.0", "2.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        version_list = provider.fetch_versions("foo")
        first = provider._wheel_by_version("foo", version_list)
        second = provider._wheel_by_version("foo", version_list)
        assert first is second
