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

import pytest

from nab_markersets import _packaging
from nab_markersets._packaging import BACKEND

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


def test_the_first_installed_candidate_wins() -> None:
    found = _packaging._import_backend(("nab_markersets", "packaging"))

    assert found.__name__ == "nab_markersets"


def test_a_candidate_that_is_not_installed_is_passed_over() -> None:
    found = _packaging._import_backend((ABSENT, "packaging"))

    assert found.__name__ == "packaging"


def test_no_candidate_at_all_names_both_extras() -> None:
    with pytest.raises(ImportError, match=r"nab-markersets\[packaging\]"):
        _packaging._import_backend((ABSENT, f"{ABSENT}_either"))


@pytest.mark.parametrize("version", ["26.2", "0", "", "not-a-version"])
def test_a_copy_below_the_floor_is_refused(version: str) -> None:
    """An install that names no extra declares no floor, so this is where it holds."""
    with pytest.raises(ImportError, match=rf"packaging>={_packaging.MINIMUM}"):
        _packaging._require_floor("packaging", version)


@pytest.mark.parametrize("version", ["26.3", "26.4.dev0", "27.0"])
def test_a_copy_at_or_above_the_floor_passes(version: str) -> None:
    assert _packaging._require_floor("packaging", version) is None


def test_the_bound_copy_clears_the_floor() -> None:
    bound = _packaging._import_or_none(BACKEND)

    assert bound is not None
    assert _packaging._require_floor(BACKEND, bound.__version__) is None


def test_every_bound_name_comes_from_the_bound_backend() -> None:
    """No name is left over from the copy that lost the probe."""
    homes = {name: getattr(_packaging, name).__module__ for name in BOUND_NAMES}

    assert set(homes) == set(BOUND_NAMES)
    assert all(home.startswith(BACKEND) for home in homes.values()), homes


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
