"""Emission-time per-package marker simplification.

Covers the ``build_pylock`` wiring that finalises each per-package marker
to its shortest within-universe form before the disjointness and coverage
gates, plus the fail-closed verify on the emitted bytes.
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

from nab_python._lockfile.coverage import CoverageError, validate_marker_coverage
from nab_python._lockfile.disjointness import validate_marker_disjointness
from nab_python._lockfile.pylock import (
    UnsoundSimplificationError,
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


class TestGatesRunOnSimplified:
    def test_gates_pass_on_simplified_output(self) -> None:
        emitted = _emitted(_span_lock())
        assert emitted["foo"].get("marker") == 'sys_platform == "linux"'

    def test_environments_rows_unchanged(self) -> None:
        data = tomllib.loads(render_lock(_span_lock()))
        assert data["environments"] == [str(m) for m in _ENVS]

    def test_non_covering_lock_still_fails(self) -> None:
        target = _target("3.13")
        lock_input = LockInput(
            targets={
                target.label: TargetLock(target=target, pins={"foo": _index_pin()})
            },
            environments=[
                Marker(
                    'python_version == "3.13" and sys_platform == "linux"'
                    ' and platform_machine == "x86_64"'
                    ' and python_full_version >= "3.13.2"'
                )
            ],
        )
        with pytest.raises(CoverageError):
            build_pylock(lock_input)


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
        def diverge(self: MarkerSet, *, within: MarkerSet) -> MarkerSet:
            return MarkerSet.from_marker('sys_platform == "win32"')

        monkeypatch.setattr(MarkerSet, "simplify", diverge)
        with pytest.raises(UnsoundSimplificationError, match="foo"):
            _finalize_marker(_wide_linux_marker(), _union(_wide_rows()), "foo")


class TestFreeMembershipPassthrough:
    def test_three_large_sets_write_lock_within_budget(self) -> None:
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


_LOCK_MARKERS = json.loads(
    (Path(__file__).parent / "data" / "m5m7b_lock_markers.json").read_text()
)

# (sys_platform, platform_machine) of a declared row to its PlatformSpec label.
_PLATFORM_LABEL = {
    ("linux", "x86_64"): "linux_x86_64",
    ("linux", "aarch64"): "linux_aarch64",
    ("darwin", "arm64"): "macos_arm64",
    ("darwin", "x86_64"): "macos_x86_64",
    ("win32", "AMD64"): "windows_amd64",
}

# The declared environments span 5 platforms x 5 minors (3.10 split at 3.10.2).
# 178 markers across the 7 universal locks became tractable; the docs lock's 34
# were already tractable and stay byte-identical.
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


def _coverage_targets(rows: Sequence[str]) -> list[ResolveTarget]:
    seen: set[tuple[str, str]] = set()
    targets: list[ResolveTarget] = []
    for row in rows:
        env = _row_env(row)
        key = (
            env["python_version"],
            _PLATFORM_LABEL[(env["sys_platform"], env["platform_machine"])],
        )
        if key in seen:
            continue
        seen.add(key)
        targets.append(_target(key[0], key[1]))
    return targets


class TestEightLockRegression:
    """The captured 8-lock fixture: 178 raw markers finalise offline to the
    committed goldens, and the emission gates still pass on the simplified
    output."""

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
            validate_marker_coverage(
                _coverage_targets(rows), environments=[Marker(r) for r in rows]
            )


class TestRegenLocksIdempotent:
    """Re-finalising the committed (short) locks changes no marker, so a real
    drift in any lock's markers is caught explicitly."""

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
