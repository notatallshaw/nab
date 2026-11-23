"""Check pylock marker emission against the repository's fixtures and lockfiles."""

from pathlib import Path
from runpy import run_path

import tomli
from packaging.markers import Marker

from nab_markersets.markersets import MarkerSet
from nab_project._lockfile.pylock import _finalize_marker

_ROOT = Path(__file__).resolve().parents[1]


def _environment_union(rows: list[str]) -> MarkerSet:
    within = MarkerSet.empty()
    for row in rows:
        within = within.union(MarkerSet.from_marker(row))
    return within


def test_emitter_preserves_marker_simplifications() -> None:
    fixtures = run_path(
        str(_ROOT / "nab-markersets" / "tests" / "simplify_corpus_fixtures.py")
    )["FIXTURES"]
    raw_chars = emitted_chars = 0
    for fixture in fixtures:
        within = _environment_union(fixture["environments"])
        package = fixture["package"]
        raw = Marker(fixture["marker"])
        simplified = MarkerSet.from_marker(str(raw)).simplify(within=within)
        emitted = _finalize_marker(raw, within, package)
        text = str(emitted) if emitted is not None else None
        assert text == simplified.to_marker_string(), (fixture["lock"], package)
        if emitted is not None:
            again = _finalize_marker(emitted, within, package)
            assert again is not None, (fixture["lock"], package)
            assert str(again) == text, (fixture["lock"], package)
        raw_chars += len(fixture["marker"])
        emitted_chars += len(text or "")

    assert (raw_chars, emitted_chars) == (9742, 1010)


def test_committed_locks_refinalize_identically() -> None:
    for path in (
        _ROOT / "pylock.dev.toml",
        _ROOT / ".github/requirements/pylock.build.toml",
    ):
        data = tomli.loads(path.read_text(encoding="utf-8"))
        within = _environment_union(data.get("environments", []))
        for package in data["packages"]:
            if (marker := package.get("marker")) is not None:
                emitted = _finalize_marker(Marker(marker), within, package["name"])
                assert str(emitted) == marker, (path.name, package["name"])
