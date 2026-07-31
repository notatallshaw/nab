"""Emission-time per-package marker simplification.

Covers the ``build_pylock`` wiring that finalises each per-package marker
to its shortest within-universe form before the disjointness gate, plus the
fail-closed verify on the emitted bytes.
"""

from __future__ import annotations

import importlib.util
import sys
from functools import reduce
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

from nab_python._lockfile.disjointness import validate_marker_disjointness
from nab_python._lockfile.pylock import (
    UnsoundSimplificationError,
    _emission_universe,
    _finalize_marker,
    build_pylock,
    render_lock,
)
from nab_python._vendor.packaging.markers import Marker
from nab_python._vendor.packaging.markersets import IntractableMarkerSet, MarkerSet
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
    from collections.abc import Mapping, Sequence

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
        def diverge(self: MarkerSet, *, within: MarkerSet) -> MarkerSet:
            return MarkerSet.from_marker('sys_platform == "win32"')

        monkeypatch.setattr(MarkerSet, "simplify", diverge)
        with pytest.raises(UnsoundSimplificationError, match="foo"):
            build_pylock(_span_lock())

    def test_collapse_to_full_off_universe_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def collapse(self: MarkerSet, *, within: MarkerSet) -> MarkerSet:
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
        def intractable(self: MarkerSet, *, within: MarkerSet) -> MarkerSet:
            raise IntractableMarkerSet("over budget")

        monkeypatch.setattr(MarkerSet, "simplify", intractable)
        raw = Marker('sys_platform == "linux" and "gpu" not in extras')
        result = _finalize_marker(raw, _union(_ENVS), "torch")
        assert result is not None
        assert str(result) == str(raw)


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

    def test_uncovered_package_ships_raw_marker(self) -> None:
        emitted = _emitted(_non_covering_lock())["pywin32"]["marker"]
        expected = " or ".join(f"({t.environment_marker_string})" for t in _WIN)
        assert emitted == str(Marker(expected))

    def test_covered_packages_still_finalise(self) -> None:
        assert "marker" not in _emitted(_non_covering_lock())["bar"]

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

    def test_uninhabited_lock_still_emits(self) -> None:
        rows = [Marker('sys_platform == "linux" and sys_platform == "win32"')]
        data = tomllib.loads(render_lock(self._with_environments(rows)))
        assert [p["name"] for p in data["packages"]] == ["foo"]

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

    The verify complements the whole matrix in one shot, so it overruns before
    the operator does: on the corpus's widest universes three markers simplify
    and are then discarded, and the lock carries the raw bytes.
    """

    def test_verify_budget_discards_three_corpus_simplifications(self) -> None:
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
                assert emitted == fixture["marker"]
                discarded.append((fixture["lock"], fixture["package"]))
        assert discarded == [
            ("max-25-tuples", "colorama"),
            ("max-25-tuples", "importlib-metadata"),
            ("max-25-tuples", "zipp"),
        ]
        assert (raw_bytes, operator_bytes, emitted_bytes) == (9742, 1010, 3992)


class TestDeterminism:
    def test_shuffled_target_order_is_byte_identical(self) -> None:
        forward = render_lock(_span_lock((*_LINUX, *_WIN)))
        reversed_order = render_lock(_span_lock((*reversed(_WIN), *reversed(_LINUX))))
        assert forward == reversed_order
