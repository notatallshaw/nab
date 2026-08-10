"""Emission-time per-package marker simplification.

Covers the ``build_pylock`` wiring that finalises each per-package marker
to its shortest within-universe form before the disjointness gate, plus the
fail-closed verify on the emitted bytes.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from functools import reduce
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

from nab_python._lockfile import pylock
from nab_python._lockfile.coverage import CoverageError
from nab_python._lockfile.disjointness import validate_marker_disjointness
from nab_python._lockfile.pylock import (
    UnsoundSimplificationError,
    _emission_universe,
    _finalize_cached,
    _finalize_marker,
    build_pylock,
    render_lock,
)
from nab_python._vendor.packaging import _markersets as engine
from nab_python._vendor.packaging import markersets
from nab_python._vendor.packaging.markers import Marker
from nab_python._vendor.packaging.markersets import (
    DecisionStore,
    IntractableMarkerSet,
    MarkerSet,
)
from nab_python._vendor.packaging.pylock import Package, PackageWheel
from nab_python._vendor.packaging.utils import canonicalize_name
from nab_python._vendor.packaging.version import Version
from nab_python.lockfile import (
    DisjointnessError,
    IndexPin,
    LockInput,
    SdistArtifact,
    TargetLock,
    WheelArtifact,
)
from nab_python.tags import PlatformSpec
from nab_python.target import ResolveTarget, environment_declaration

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from nab_python._vendor.packaging._markersets import Atom, Cell

_spec = importlib.util.spec_from_file_location(
    "simplify_corpus_fixtures",
    Path(__file__).with_name("simplify_corpus_fixtures.py"),
)
assert _spec is not None
assert _spec.loader is not None
corpus = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(corpus)


def _target(python_version: str, platform: str = "linux_x86_64") -> ResolveTarget:
    return ResolveTarget.for_declared(
        python_version=python_version, spec=PlatformSpec(platform)
    )


def _index_pin(name: str = "foo", version: str = "1.0") -> IndexPin:
    return IndexPin(
        name=name,
        version=version,
        index="pypi",
        sdist=SdistArtifact(
            filename=f"{name}-{version}.tar.gz",
            url=f"https://example.com/{name}-{version}.tar.gz",
            hashes=(("sha256", "b" * 64),),
            size=2048,
        ),
        wheels=(
            WheelArtifact(
                filename=f"{name}-{version}-py3-none-any.whl",
                url=f"https://example.com/{name}-{version}-py3-none-any.whl",
                hashes=(("sha256", "a" * 64),),
                size=1024,
            ),
        ),
        requires_python=">=3.10",
    )


def _row(target: ResolveTarget) -> Marker:
    return Marker(environment_declaration(target, []))


def _union(markers: Sequence[Marker]) -> MarkerSet:
    return reduce(
        MarkerSet.union,
        (MarkerSet.from_marker(m) for m in markers),
        MarkerSet.empty(),
    )


# foo resolves on linux 3.10/3.11/3.12; bar everywhere. The declared
# environments span linux and windows across those three minors, so
# foo's verbose per-minor OR is within-universe equivalent to a bare
# ``sys_platform == "linux"``.
_LINUX = [_target(v) for v in ("3.10", "3.11", "3.12")]
_WIN = [_target(v, "windows_amd64") for v in ("3.10", "3.11", "3.12")]
_ENVS = [_row(t) for t in (*_LINUX, *_WIN)]


def _span_lock(order: Sequence[ResolveTarget] | None = None) -> LockInput:
    targets: dict[str, TargetLock] = {}
    for t in order if order is not None else (*_LINUX, *_WIN):
        pins: dict[str, IndexPin] = {"bar": _index_pin("bar")}
        if t in _LINUX:
            pins["foo"] = _index_pin("foo")
        targets[t.label] = TargetLock(target=t, pins=pins)
    return LockInput(targets=targets, environments=list(_ENVS))


def _emitted(lock_input: LockInput) -> dict[str, dict[str, Any]]:
    data = tomllib.loads(render_lock(lock_input))
    return {p["name"]: p for p in data["packages"]}


def _marker_of(lock_input: LockInput, name: str) -> str | None:
    return _emitted(lock_input)[name].get("marker")


class TestEmittedMarker:
    def test_verbose_tuple_emits_simplified(self) -> None:
        assert _marker_of(_span_lock(), "foo") == 'sys_platform == "linux"'

    def test_unconditional_package_emits_no_marker(self) -> None:
        assert "marker" not in _emitted(_span_lock())["bar"]

    def test_gate_only_marker_survives_unchanged(self) -> None:
        host = _target("3.11")
        lock_input = LockInput(
            targets={
                host.label: TargetLock(
                    target=host,
                    pins={"mytool": _index_pin("mytool")},
                    package_gates={"mytool": (("extra", "cli"),)},
                )
            },
            environments=[_row(host)],
            extras=("cli",),
        )
        assert _marker_of(lock_input, "mytool") == '"cli" in extras'

    def test_context_free_tier_keeps_python_axis(self) -> None:
        lin4 = [_target(v) for v in ("3.10", "3.11", "3.12", "3.13")]
        targets: dict[str, TargetLock] = {}
        for i, t in enumerate(lin4):
            pins: dict[str, IndexPin] = {"bar": _index_pin("bar")}
            if i < 3:
                pins["foo"] = _index_pin("foo")
            targets[t.label] = TargetLock(target=t, pins=pins)
        lock_input = LockInput(targets=targets, environments=[])
        assert _marker_of(lock_input, "foo") == (
            'platform_machine == "x86_64" and sys_platform == "linux"'
            ' and (python_version == "3.10" or python_version == "3.11"'
            ' or python_version == "3.12")'
        )


class TestEnvironmentsRows:
    def test_environments_rows_unchanged(self) -> None:
        data = tomllib.loads(render_lock(_span_lock()))
        assert data["environments"] == [str(m) for m in _ENVS]


class TestDisjointnessVerdictInvariance:
    def _pkg(self, version: str, marker: str) -> Package:
        return Package(
            name=canonicalize_name("foo"),
            version=Version(version),
            marker=Marker(marker),
            wheels=(
                PackageWheel(
                    name=f"foo-{version}-py3-none-any.whl",
                    url=f"https://x/foo-{version}-py3-none-any.whl",
                    hashes={"sha256": "a" * 64},
                ),
            ),
        )

    def _envs(self) -> Mapping[str, Mapping[str, str]]:
        return {t.label: t.marker_env for t in (*_LINUX, *_WIN)}

    def _passes(self, markers: Sequence[str]) -> bool:
        packages = [self._pkg(str(i), m) for i, m in enumerate(markers)]
        try:
            validate_marker_disjointness(
                packages, environments=self._envs(), extras=(), groups=()
            )
        except DisjointnessError:
            return False
        return True

    def _simplify(self, marker: str) -> str:
        text = (
            MarkerSet.from_marker(marker)
            .simplify(within=_union(_ENVS))
            .to_marker_string()
        )
        assert text is not None
        return text

    def test_disjoint_pair_stays_disjoint(self) -> None:
        linux = " or ".join(f"({t.environment_marker_string})" for t in _LINUX)
        windows = " or ".join(f"({t.environment_marker_string})" for t in _WIN)
        assert self._passes([linux, windows]) is True
        assert self._passes([self._simplify(linux), self._simplify(windows)]) is True

    def test_colliding_pair_still_collides(self) -> None:
        linux = " or ".join(f"({t.environment_marker_string})" for t in _LINUX)
        assert self._passes([linux, linux]) is False
        simplified = self._simplify(linux)
        assert self._passes([simplified, simplified]) is False


class TestOutOfUniverseDivergence:
    def test_simplified_marker_diverges_outside_universe(self) -> None:
        raw = " or ".join(f"({t.environment_marker_string})" for t in _LINUX)
        emitted = _marker_of(_span_lock(), "foo")
        assert emitted == 'sys_platform == "linux"'
        outside = {
            "python_version": "3.13",
            "python_full_version": "3.13.0",
            "sys_platform": "linux",
            "platform_machine": "x86_64",
        }
        assert MarkerSet.from_marker(emitted).evaluate(outside) is True
        assert MarkerSet.from_marker(raw).evaluate(outside) is False


class TestFailClosed:
    def test_injected_bug_raises_and_emits_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def diverge(
            self: MarkerSet, *, within: MarkerSet, store: DecisionStore | None = None
        ) -> MarkerSet:
            return MarkerSet.from_marker('sys_platform == "win32"')

        monkeypatch.setattr(MarkerSet, "simplify", diverge)
        with pytest.raises(UnsoundSimplificationError, match="foo"):
            build_pylock(_span_lock())

    def test_collapse_to_full_off_universe_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def collapse(
            self: MarkerSet, *, within: MarkerSet, store: DecisionStore | None = None
        ) -> MarkerSet:
            return MarkerSet.full()

        monkeypatch.setattr(MarkerSet, "simplify", collapse)
        raw = Marker('sys_platform == "linux"')
        with pytest.raises(UnsoundSimplificationError, match="foo"):
            _finalize_marker(raw, _union(_ENVS), "foo")


class TestFinalizeMarker:
    def test_none_marker_passes_through(self) -> None:
        assert _finalize_marker(None, MarkerSet.full()) is None

    def test_full_over_universe_emits_none(self) -> None:
        tautology = Marker('python_version == "3.11" or python_version != "3.11"')
        assert _finalize_marker(tautology, MarkerSet.full()) is None

    def test_intractable_emits_raw_byte_identical(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def intractable(
            self: MarkerSet, *, within: MarkerSet, store: DecisionStore | None = None
        ) -> MarkerSet:
            raise IntractableMarkerSet("over budget")

        monkeypatch.setattr(MarkerSet, "simplify", intractable)
        raw = Marker('sys_platform == "linux" and "gpu" not in extras')
        result = _finalize_marker(raw, _union(_ENVS), "torch")
        assert result is not None
        assert str(result) == str(raw)


@pytest.fixture
def ungated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand the coverage gate down for a hand-built declaration.

    ``build_pylock`` refuses a lock whose ``environments`` leave a resolved
    target uncovered, so a non-covering shape never reaches the simplification
    layer through the public entry point.  The tests that ask what that layer
    does with one drive it with the gate stood down.
    """
    monkeypatch.setattr(pylock, "validate_marker_coverage", lambda *_, **__: None)


def _non_covering_lock() -> LockInput:
    """A lock whose declared rows cover the linux targets only.

    ``build_lock_input`` derives the rows from the same targets it locked, so
    this shape needs a hand-built :class:`LockInput`; ``LockInput.environments``
    is public and takes any markers.
    """
    targets: dict[str, TargetLock] = {}
    for t in (*_LINUX, *_WIN):
        pins: dict[str, IndexPin] = {"bar": _index_pin("bar")}
        if t in _WIN:
            pins["pywin32"] = _index_pin("pywin32")
        targets[t.label] = TargetLock(target=t, pins=pins)
    return LockInput(targets=targets, environments=[_row(t) for t in _LINUX])


class TestNonCoveringEnvironments:
    """A package selecting nothing inside the declared universe ships raw.

    Its simplification is the empty set, which has no marker spelling, so there
    is nothing shorter to emit.
    """

    @pytest.mark.usefixtures("ungated")
    def test_uncovered_package_ships_raw_marker(self) -> None:
        emitted = _emitted(_non_covering_lock())["pywin32"]["marker"]
        expected = " or ".join(f"({t.environment_marker_string})" for t in _WIN)
        assert emitted == str(Marker(expected))

    @pytest.mark.usefixtures("ungated")
    def test_covered_packages_still_finalise(self) -> None:
        assert "marker" not in _emitted(_non_covering_lock())["bar"]

    def test_the_coverage_gate_refuses_the_shape(self) -> None:
        with pytest.raises(CoverageError, match="win32"):
            render_lock(_non_covering_lock())

    def test_direct_empty_within_universe_ships_raw(self) -> None:
        raw = Marker('sys_platform == "aix"')
        result = _finalize_marker(raw, _union(_ENVS), "aixonly")
        assert result is not None
        assert str(result) == str(raw)

    def test_globally_contradictory_marker_ships_raw(self) -> None:
        raw = Marker('sys_platform == "linux" and sys_platform != "linux"')
        result = _finalize_marker(raw, MarkerSet.full(), "never")
        assert result is not None
        assert str(result) == str(raw)


class TestEmissionUniverse:
    """The universe the emitter simplifies against.

    These declarations are hand-built; nab's own resolve never produces them.
    """

    def _with_environments(self, environments: Sequence[Marker]) -> LockInput:
        host = _target("3.11")
        return LockInput(
            targets={
                host.label: TargetLock(target=host, pins={"foo": _index_pin("foo")})
            },
            environments=list(environments),
        )

    def test_no_environments_is_the_full_set(self) -> None:
        assert _emission_universe(self._with_environments([])).is_full()

    def test_uninhabited_rows_fall_back_to_the_full_set(self) -> None:
        rows = [Marker('sys_platform == "linux" and sys_platform == "win32"')]
        assert _emission_universe(self._with_environments(rows)).is_full()

    def test_one_inhabited_row_keeps_the_declared_union(self) -> None:
        rows = [
            Marker('sys_platform == "linux" and sys_platform == "win32"'),
            Marker('sys_platform == "linux"'),
        ]
        universe = _emission_universe(self._with_environments(rows))
        assert universe.equivalent(MarkerSet.from_marker('sys_platform == "linux"'))

    @pytest.mark.usefixtures("ungated")
    def test_uninhabited_lock_still_emits(self) -> None:
        rows = [Marker('sys_platform == "linux" and sys_platform == "win32"')]
        data = tomllib.loads(render_lock(self._with_environments(rows)))
        assert [p["name"] for p in data["packages"]] == ["foo"]

    def test_uninhabited_rows_do_not_get_past_the_coverage_gate(self) -> None:
        rows = [Marker('sys_platform == "linux" and sys_platform == "win32"')]
        with pytest.raises(CoverageError):
            render_lock(self._with_environments(rows))

    def test_undecidable_row_counts_as_inhabited(self) -> None:
        wide = Marker(" and ".join(f'"e{i}" in extras' for i in range(20)))
        universe = _emission_universe(self._with_environments([wide]))
        # The full set would drop a tautology; an undecidable universe decides
        # nothing, so the marker ships raw.
        tautology = Marker('python_version == "3.11" or python_version != "3.11"')
        result = _finalize_marker(tautology, universe, "foo")
        assert result is not None
        assert str(result) == str(tautology)


class TestCorpusEmitterBudget:
    """What the emitter delivers on the corpus, next to what the operator computes.

    The verify runs per universe row, under the same budget as the operator, so
    it decides wherever the operator does and the lock carries every byte the
    operator computed.
    """

    def test_emitter_delivers_every_operator_simplification(self) -> None:
        raw_bytes = operator_bytes = emitted_bytes = 0
        discarded: list[tuple[str, str]] = []
        for fixture in corpus.FIXTURES:
            within = _union([Marker(e) for e in fixture["environments"]])
            raw = Marker(fixture["marker"])
            operator = (
                MarkerSet.from_marker(raw).simplify(within=within).to_marker_string()
                or ""
            )
            finalized = _finalize_marker(raw, within, fixture["package"])
            emitted = str(finalized) if finalized is not None else ""
            raw_bytes += len(fixture["marker"])
            operator_bytes += len(operator)
            emitted_bytes += len(emitted)
            if emitted != operator:
                discarded.append((fixture["lock"], fixture["package"]))
        assert discarded == []
        assert (raw_bytes, operator_bytes, emitted_bytes) == (9742, 1010, 1010)


class TestDeterminism:
    def test_shuffled_target_order_is_byte_identical(self) -> None:
        forward = render_lock(_span_lock((*_LINUX, *_WIN)))
        reversed_order = render_lock(_span_lock((*reversed(_WIN), *reversed(_LINUX))))
        assert forward == reversed_order


# Six platforms across ten python minors: the whole-matrix complement of a
# single-platform full span overruns the cell budget, so the short marker is
# reachable only through the row-restricted verify.
_WIDE_PLATS = (
    "linux_x86_64",
    "windows_amd64",
    "macos_arm64",
    "linux_aarch64",
    "windows_arm64",
    "macos_x86_64",
)
_WIDE_MINORS = tuple(f"3.{n}" for n in range(9, 19))
_WIDE_SHORT = 'platform_machine == "x86_64" and sys_platform == "linux"'


def _wide_targets() -> list[ResolveTarget]:
    return [_target(v, p) for p in _WIDE_PLATS for v in _WIDE_MINORS]


def _wide_rows(order: Sequence[ResolveTarget] | None = None) -> list[Marker]:
    return [_row(t) for t in (order if order is not None else _wide_targets())]


def _wide_linux_marker(minors: Sequence[str] | None = None) -> Marker:
    return Marker(
        " or ".join(
            f"({_target(v, 'linux_x86_64').environment_marker_string})"
            for v in (minors if minors is not None else _WIDE_MINORS)
        )
    )


class TestWideMatrixEmission:
    def test_wide_matrix_simplifies_to_short_marker(self) -> None:
        raw = _wide_linux_marker()
        result = _finalize_marker(raw, _union(_wide_rows()), "foo")
        assert result is not None
        assert str(result) == _WIDE_SHORT

    def test_row_verify_decides_where_whole_matrix_overruns(self) -> None:
        within = _union(_wide_rows())
        raw_set = MarkerSet.from_marker(_wide_linux_marker())
        emitted = MarkerSet.from_marker(_WIDE_SHORT)
        with pytest.raises(IntractableMarkerSet):
            (raw_set & within).equivalent(emitted & within)
        assert raw_set.equivalent_within(emitted, within) is True

    def test_determinism_under_shuffled_target_and_row_order(self) -> None:
        forward = _finalize_marker(_wide_linux_marker(), _union(_wide_rows()), "foo")
        back = _finalize_marker(
            _wide_linux_marker(list(reversed(_WIDE_MINORS))),
            _union(_wide_rows(list(reversed(_wide_targets())))),
            "foo",
        )
        assert forward is not None
        assert str(forward) == str(back) == _WIDE_SHORT


class TestWideMatrixFailClosed:
    def test_injected_bug_on_wide_matrix_raises_without_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def diverge(
            self: MarkerSet, *, within: MarkerSet, store: DecisionStore | None = None
        ) -> MarkerSet:
            return MarkerSet.from_marker('sys_platform == "win32"')

        monkeypatch.setattr(MarkerSet, "simplify", diverge)
        with pytest.raises(UnsoundSimplificationError, match="foo"):
            _finalize_marker(_wide_linux_marker(), _union(_wide_rows()), "foo")


class TestFreeMembershipPassthrough:
    def test_large_membership_sets_ship_raw(self) -> None:
        rows = [_row(_target(v)) for v in ("3.11", "3.12")]
        within = _union(rows)
        set_a = ("cpu", "cu118", "cu121", "rocm")
        set_b = ("mkl", "openblas", "accelerate", "blis")
        set_c = ("gpu", "nogpu")
        reached = ("cu121", "mkl")
        co_members = sorted(
            {*set_a, *set_b, *set_c}.difference(reached),
        )
        gate = " or ".join(f'"{m}" in extras' for m in reached)
        negations = " and ".join(f'"{m}" not in extras' for m in co_members)
        raw = Marker(
            " or ".join(
                f"({r} and ({gate}) and {negations})"
                for r in (_row(_target("3.11")), _row(_target("3.12")))
            )
        )
        with pytest.raises(IntractableMarkerSet):
            MarkerSet.from_marker(raw).simplify(within=within)
        result = _finalize_marker(raw, within, "torch")
        assert result is not None
        assert str(result) == str(raw)


class TestCorpusByteIdentity:
    def test_tractable_corpus_markers_emit_byte_identical(self) -> None:
        for fixture in corpus.FIXTURES:
            within = _union([Marker(e) for e in fixture["environments"]])
            raw = Marker(fixture["marker"])
            finalized = _finalize_marker(raw, within, fixture["package"])
            operator = (
                MarkerSet.from_marker(raw).simplify(within=within).to_marker_string()
            )
            shown = str(finalized) if finalized is not None else None
            assert shown == operator
            again = (
                _finalize_marker(finalized, within, fixture["package"])
                if finalized is not None
                else None
            )
            assert (str(again) if again is not None else None) == shown


class TestWorkBudget:
    """A run past the simplifier's work budget ships the raw marker.

    An overrun raises ``IntractableMarkerSet``, which is already the raw-marker
    path.
    """

    def test_overrun_ships_raw_byte_identical(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        raw = _wide_linux_marker()
        within = _union(_wide_rows())
        assert str(_finalize_marker(raw, within, "foo")) != str(raw)
        monkeypatch.setattr(markersets, "_MAX_WORK", 1)
        result = _finalize_marker(raw, within, "foo")
        assert result is not None
        assert str(result) == str(raw)


class TestFinalizeMemo:
    """Packages sharing one raw marker are finalised once per lock.

    The universe is fixed for a whole build, so the shortest form of a marker
    does not depend on which package carries it.
    """

    def test_repeated_marker_finalises_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []
        real = pylock._finalize_marker

        def counting(
            raw: Marker | None,
            within: MarkerSet,
            name: str = "",
            store: DecisionStore | None = None,
        ) -> Marker | None:
            calls.append(name)
            return real(raw, within, name, store)

        monkeypatch.setattr(pylock, "_finalize_marker", counting)
        targets: dict[str, TargetLock] = {}
        for t in (*_LINUX, *_WIN):
            pins: dict[str, IndexPin] = {"bar": _index_pin("bar")}
            if t in _LINUX:
                for i in range(5):
                    pins[f"foo{i}"] = _index_pin(f"foo{i}")
            targets[t.label] = TargetLock(target=t, pins=pins)
        lock_input = LockInput(targets=targets, environments=list(_ENVS))
        emitted = _emitted(lock_input)
        assert [emitted[f"foo{i}"]["marker"] for i in range(5)] == [
            'sys_platform == "linux"'
        ] * 5
        assert calls == ["foo0"]

    def test_memo_passes_none_through(self) -> None:
        memo: dict[str, Marker | None] = {}
        assert (
            _finalize_cached(None, MarkerSet.full(), "foo", memo, DecisionStore())
            is None
        )
        assert memo == {}


_LOCK_MARKERS = json.loads(
    (Path(__file__).parent / "data" / "ci_lock_markers.json").read_text()
)

# The declared environments span 5 platforms x 5 minors (3.10 split at 3.10.2).
# 178 markers across the 7 universal locks shorten; the docs lock's 34 already
# say the shortest thing they can and stay byte-identical.
_WIDE_MARKER_COUNT = 178
_DOCS_MARKER_COUNT = 34
_SUBSET_RAW_BYTES = 40183
_SUBSET_GOLD_BYTES = 5822


def _fixture_universe(rows: Sequence[str]) -> MarkerSet:
    return _union([Marker(r) for r in rows])


def _row_env(row: str) -> dict[str, str]:
    witness = MarkerSet.from_marker(Marker(row)).witness()
    assert witness is not None
    return {k: v for k, v in witness.items() if isinstance(v, str)}


class TestEightLockRegression:
    """The captured markers of the eight committed CI locks.

    Each raw marker finalises offline to the byte the lock now carries, and the
    emission gates still pass on the simplified output.
    """

    def test_wide_locks_become_tractable_and_byte_exact(self) -> None:
        raw_total = gold_total = count = 0
        for name, lock in _LOCK_MARKERS["locks"].items():
            if name == "docs":
                continue
            within = _fixture_universe(lock["environments"])
            for pkg in lock["packages"]:
                raw = Marker(pkg["raw"])
                simplified = MarkerSet.from_marker(raw).simplify(within=within)
                assert simplified is not None
                finalized = _finalize_marker(raw, within, pkg["name"])
                shown = str(finalized) if finalized is not None else ""
                assert shown == pkg["golden"]
                raw_total += len(pkg["raw"])
                gold_total += len(pkg["golden"])
                count += 1
        assert count == _WIDE_MARKER_COUNT
        assert raw_total == _SUBSET_RAW_BYTES
        assert gold_total == _SUBSET_GOLD_BYTES

    def test_docs_lock_stays_byte_identical(self) -> None:
        lock = _LOCK_MARKERS["locks"]["docs"]
        within = _fixture_universe(lock["environments"])
        count = 0
        for pkg in lock["packages"]:
            raw = Marker(pkg["raw"])
            simplified = MarkerSet.from_marker(raw).simplify(within=within)
            assert simplified is not None
            finalized = _finalize_marker(raw, within, pkg["name"])
            assert str(finalized) == pkg["raw"] == pkg["golden"]
            count += 1
        assert count == _DOCS_MARKER_COUNT

    def test_named_landmarks(self) -> None:
        golden = {
            (name, pkg["name"]): (len(pkg["raw"]), len(pkg["golden"]))
            for name, lock in _LOCK_MARKERS["locks"].items()
            for pkg in lock["packages"]
        }
        assert golden[("crosshair", "zipp")] == (4037, 66)
        assert golden[("release", "backports-tarfile")] == (2269, 89)
        per_lock = {
            name: (
                sum(len(p["raw"]) for p in lock["packages"]),
                sum(len(p["golden"]) for p in lock["packages"]),
            )
            for name, lock in _LOCK_MARKERS["locks"].items()
        }
        assert per_lock["release"] == (14241, 2045)
        assert per_lock["crosshair"] == (11304, 1176)

    def test_raw_golden_agree_under_upstream_evaluate(self) -> None:
        """Check every shipped golden against the upstream Marker evaluator.

        The operator and the emission verify share one row oracle; an independent
        evaluator catches a bug they would both miss. Evaluates raw and golden on
        every declared row under an empty and the lock's own group selection.
        """
        checked = 0
        for name, lock in _LOCK_MARKERS["locks"].items():
            envs = [_row_env(row) for row in lock["environments"]]
            selections = (frozenset[str](), frozenset({name}))
            for pkg in lock["packages"]:
                raw = Marker(pkg["raw"])
                golden = Marker(pkg["golden"]) if pkg["golden"] else None
                for base in envs:
                    for groups in selections:
                        env: dict[str, str | frozenset[str]] = {
                            **base,
                            "dependency_groups": groups,
                        }
                        gold_holds = (
                            golden.evaluate(env, context="lock_file")
                            if golden is not None
                            else True
                        )
                        assert raw.evaluate(env, context="lock_file") == gold_holds, (
                            f"{name}/{pkg['name']}: {pkg['raw']!r} and"
                            f" {pkg['golden']!r} disagree at {env!r}"
                        )
                checked += 1
        assert checked == _WIDE_MARKER_COUNT + _DOCS_MARKER_COUNT

    def test_gates_pass_on_simplified_output(self) -> None:
        for name, lock in _LOCK_MARKERS["locks"].items():
            rows = lock["environments"]
            packages = [
                Package(
                    name=canonicalize_name(pkg["name"]),
                    marker=Marker(pkg["golden"]) if pkg["golden"] else None,
                )
                for pkg in lock["packages"]
            ]
            environments = {f"e{i}": _row_env(row) for i, row in enumerate(rows)}
            validate_marker_disjointness(
                packages, environments=environments, extras=(), groups=(name,)
            )


class TestRegenLocksIdempotent:
    """Re-finalising a committed lock changes no marker.

    Simplification is idempotent, so a drift in any lock's markers shows up here
    rather than only in a lock refresh.
    """

    def test_committed_locks_refinalize_identically(self) -> None:
        lock_dir = Path(__file__).parents[2] / ".github" / "requirements"
        for name in _LOCK_MARKERS["locks"]:
            data = tomllib.loads((lock_dir / f"pylock.{name}.toml").read_text())
            within = _fixture_universe(data.get("environments", []))
            for pkg in data["packages"]:
                marker = pkg.get("marker")
                if marker is None:
                    continue
                again = _finalize_marker(Marker(marker), within, pkg["name"])
                assert str(again) == marker


def _partition_counter(monkeypatch: pytest.MonkeyPatch) -> Callable[[], int]:
    """Start counting axis partitions, and return a reader for the count."""
    calls = 0
    real = engine._partition_axis

    def counting(
        axis: tuple, atoms: Sequence[Atom], max_cells: int, memo: engine.Memo
    ) -> list[Cell]:
        nonlocal calls
        calls += 1
        return real(axis, atoms, max_cells, memo)

    monkeypatch.setattr(engine, "_partition_axis", counting)
    return lambda: calls


class TestEmissionStore:
    """One emission's decisions share one store, and answer as if they did not."""

    def test_one_store_across_a_lock_cuts_the_partition_work(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Finalising a lock's markers against one store partitions fewer axes.

        The universe is one per lock and its rows are what each decision
        complements against, so the axes carry over between packages that share
        no marker text. A ``store`` the engine ignored would leave the two counts
        equal.
        """
        lock = _LOCK_MARKERS["locks"]["tests"]
        within = _fixture_universe(lock["environments"])
        raws = [Marker(pkg["raw"]) for pkg in lock["packages"]]

        partitions = _partition_counter(monkeypatch)
        alone = [_finalize_marker(raw, within, "pkg") for raw in raws]
        cold = partitions()
        store = DecisionStore()
        shared = [_finalize_marker(raw, within, "pkg", store) for raw in raws]
        warm = partitions() - cold

        assert [str(m) for m in shared] == [str(m) for m in alone]
        assert warm < cold

    def test_finalisation_and_coverage_share_one_store(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every decision of one build_pylock is handed the same store.

        Dropping it at any hop leaves that decision cold, which no emitted byte
        would show.
        """
        seen: list[DecisionStore | None] = []
        real_simplify = MarkerSet.simplify
        real_equivalent = MarkerSet.equivalent_within
        real_coverage = pylock.validate_marker_coverage

        def recording_simplify(
            self: MarkerSet, *, within: MarkerSet, store: DecisionStore | None = None
        ) -> MarkerSet:
            seen.append(store)
            return real_simplify(self, within=within, store=store)

        def recording_equivalent(
            self: MarkerSet,
            other: MarkerSet,
            within: MarkerSet,
            *,
            store: DecisionStore | None = None,
        ) -> bool:
            seen.append(store)
            return real_equivalent(self, other, within, store=store)

        def recording_coverage(
            targets: Sequence[ResolveTarget],
            *,
            environments: Sequence[Marker],
            store: DecisionStore | None = None,
        ) -> None:
            seen.append(store)
            real_coverage(targets, environments=environments, store=store)

        monkeypatch.setattr(MarkerSet, "simplify", recording_simplify)
        monkeypatch.setattr(MarkerSet, "equivalent_within", recording_equivalent)
        monkeypatch.setattr(pylock, "validate_marker_coverage", recording_coverage)
        build_pylock(_span_lock())

        assert len(seen) >= 3
        assert {id(store) for store in seen} == {id(seen[0])}
        assert isinstance(seen[0], DecisionStore)
