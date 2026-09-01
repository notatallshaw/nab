"""Which copy of packaging the algebra binds, and what it does with neither.

The candidate list is probed at import, so the two orders it can take cannot
both be exercised in one interpreter. The unit tests below drive the probe with
candidates of their own; the subprocess probes cover the real thing, one
interpreter per install shape.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from typing import TYPE_CHECKING

import pytest

from nab_markersets import _packaging
from nab_markersets._packaging import BACKEND
from nab_markersets.markersets import MarkerSet, variable_names

if TYPE_CHECKING:
    from pathlib import Path

BOUND_NAMES = (
    "InvalidMarker",
    "InvalidSpecifier",
    "InvalidVersion",
    "Marker",
    "Op",
    "ParserSyntaxError",
    "Specifier",
    "UndefinedComparison",
    "UndefinedEnvironmentName",
    "Value",
    "Variable",
    "Version",
    "_eval_op",
    "canonicalize_name",
    "parse_marker",
)

ABSENT = "nab_markersets_no_such_backend"


def _probe(source: str) -> subprocess.CompletedProcess[str]:
    """Run a probe in a fresh interpreter and return the finished process."""
    return subprocess.run(  # noqa: S603
        [sys.executable, "-c", textwrap.dedent(source)],
        capture_output=True,
        text=True,
        check=False,
    )


def _stale_packaging(root: Path, name: str, version: str) -> None:
    """Write a packaging-shaped package at ``version``, for the probe to weigh."""
    package = root / name
    package.mkdir()
    (package / "__init__.py").write_text(f'__version__ = "{version}"\n')
    (package / "version.py").write_text(
        "from packaging.version import InvalidVersion, Version\n"
        "\n"
        "__all__ = ['InvalidVersion', 'Version']\n"
    )


def test_the_first_candidate_over_the_floor_wins() -> None:
    found = _packaging._import_backend(("packaging", ABSENT))

    assert found.__name__ == "packaging"


def test_a_candidate_that_is_not_installed_is_passed_over() -> None:
    found = _packaging._import_backend((ABSENT, "packaging"))

    assert found.__name__ == "packaging"


def test_a_candidate_below_the_floor_is_passed_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fork too old to run the algebra falls through to the released copy."""
    _stale_packaging(tmp_path, "nab_markersets_stale_backend", "26.2")
    monkeypatch.syspath_prepend(str(tmp_path))

    found = _packaging._import_backend(("nab_markersets_stale_backend", "packaging"))

    assert found.__name__ == "packaging"


def test_nothing_over_the_floor_names_what_it_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stale_packaging(tmp_path, "nab_markersets_only_stale", "26.2")
    monkeypatch.syspath_prepend(str(tmp_path))

    with pytest.raises(ImportError, match=r"found nab_markersets_only_stale 26\.2"):
        _packaging._import_backend(("nab_markersets_only_stale", ABSENT))


def test_a_candidate_with_no_version_is_passed_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stale_packaging(tmp_path, "nab_markersets_unversioned", "26.9")
    (tmp_path / "nab_markersets_unversioned" / "__init__.py").write_text("")
    monkeypatch.syspath_prepend(str(tmp_path))

    with pytest.raises(ImportError, match="found nab_markersets_unversioned"):
        _packaging._import_backend(("nab_markersets_unversioned", ABSENT))


def test_a_candidate_that_breaks_on_import_is_not_passed_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken copy says so where it broke, rather than reading as absent."""
    (tmp_path / "nab_markersets_broken_backend.py").write_text(
        'raise ImportError("something inside is missing")\n'
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    with pytest.raises(ImportError, match="something inside is missing"):
        _packaging._import_or_none("nab_markersets_broken_backend")


def test_no_candidate_at_all_names_both_extras() -> None:
    with pytest.raises(ImportError, match=r"nab-markersets\[packaging\]"):
        _packaging._import_backend((ABSENT, f"{ABSENT}_either"))


@pytest.mark.parametrize("version", ["26.2", "0", "", "not-a-version"])
def test_a_copy_below_the_floor_does_not_clear_it(version: str) -> None:
    """An install that names no extra declares no floor, so this is where it holds."""
    assert not _packaging._clears_floor("packaging", version)


@pytest.mark.parametrize("version", ["26.3", "26.4.dev0", "27.0"])
def test_a_copy_at_or_above_the_floor_clears_it(version: str) -> None:
    assert _packaging._clears_floor("packaging", version)


def test_the_bound_copy_clears_the_floor() -> None:
    bound = _packaging._import_or_none(BACKEND)

    assert bound is not None
    assert _packaging._clears_floor(BACKEND, bound.__version__)


def test_every_bound_name_comes_from_the_bound_backend() -> None:
    """No name is left over from the copy that lost the probe."""
    homes = {name: getattr(_packaging, name).__module__ for name in BOUND_NAMES}

    assert set(homes) == set(BOUND_NAMES)
    assert all(home.startswith(BACKEND) for home in homes.values()), homes


def test_a_marker_from_either_copy_is_accepted() -> None:
    """The copy that lost the probe still builds Marker objects callers pass in."""
    other = next(name for name in _packaging.BACKENDS if name != BACKEND)
    module = _packaging._import_or_none(f"{other}.markers")
    if module is None:
        pytest.skip(f"{other} is not installed")

    text = 'sys_platform == "linux"'
    from_other = MarkerSet.from_marker(module.Marker(text))

    assert from_other.equivalent(MarkerSet.from_marker(text))
    assert variable_names(module.Marker(text)) == frozenset({"sys_platform"})


class Marker:
    """Named like packaging's, from a module that is not a packaging.markers."""

    def __str__(self) -> str:
        """Return a marker string, so only the class check can refuse it."""
        return 'sys_platform == "linux"'


def test_a_class_named_marker_elsewhere_is_not_a_marker() -> None:
    """The class is matched on its module too, not just its name."""
    assert Marker.__qualname__ == "Marker"

    with pytest.raises(TypeError, match="expected str or packaging"):
        MarkerSet.from_marker(Marker())  # type: ignore[arg-type]


def test_the_fork_wins_when_both_are_installed() -> None:
    """Skipped where nab-provider is absent, which is the standalone install."""
    if _packaging._import_or_none("nab_provider") is None:
        pytest.skip("nab-provider is not installed")

    assert BACKEND == "nab_provider._vendor.packaging"


def test_released_packaging_is_bound_without_the_fork() -> None:
    # A None entry in sys.modules makes `import nab_provider` raise ImportError.
    result = _probe("""
        import sys

        sys.modules["nab_provider"] = None
        from nab_markersets._packaging import BACKEND
        from nab_markersets.markersets import MarkerSet

        assert BACKEND == "packaging", BACKEND
        assert MarkerSet.from_marker('os_name == "posix"').witness() is not None
    """)

    assert result.returncode == 0, result.stderr


def test_neither_copy_installed_refuses_the_import() -> None:
    result = _probe("""
        import sys

        sys.modules["nab_provider"] = None
        sys.modules["packaging"] = None
        try:
            import nab_markersets.markersets
        except ImportError as exc:
            print(exc)
        else:
            raise AssertionError("the import should have failed")
    """)

    assert result.returncode == 0, result.stderr
    assert "nab-markersets[packaging]" in result.stdout
    assert "nab-markersets[nab-vendored-packaging]" in result.stdout
