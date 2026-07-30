"""Set-valued extra evaluation for dependency markers.

``dependency_marker_holds`` binds ``extra`` to the full set of active extras, so
a dep gated ``extra != "cpu"`` drops for a selection including ``cpu`` and one
gated ``extra == "a"`` activates for ``[a, b]``.
"""

from __future__ import annotations

from collections.abc import Set as AbstractSet

import pytest

from nab_python._conflict_kind import dependency_marker_holds
from nab_python._vendor.packaging.markers import Marker

_ENV: dict[str, str] = {
    "python_version": "3.11",
    "python_full_version": "3.11.2",
    "sys_platform": "linux",
    "os_name": "posix",
    "platform_machine": "x86_64",
    "implementation_name": "cpython",
}


def _holds(marker_text: str, extras: AbstractSet[str]) -> bool:
    return dependency_marker_holds(Marker(marker_text), {**_ENV, "extra": extras})


class TestDependencyMarkerHoldsSetExtra:
    def test_negated_extra_dropped_for_that_selection(self) -> None:
        """``extra != "cpu"`` must not hold for a ``[cpu]`` selection."""
        assert _holds('extra != "cpu"', frozenset({"cpu"})) is False

    def test_negated_extra_holds_for_other_selection(self) -> None:
        assert _holds('extra != "cpu"', frozenset({"gpu"})) is True

    def test_negated_extra_holds_for_base(self) -> None:
        """No extras active: ``extra != "cpu"`` is a base dependency."""
        assert _holds('extra != "cpu"', frozenset()) is True

    def test_positive_extra_activates_for_multi_selection(self) -> None:
        """``extra == "a"`` activates for ``pkg[a, b]``."""
        assert _holds('extra == "a"', frozenset({"a", "b"})) is True

    def test_positive_extra_inactive_when_not_selected(self) -> None:
        assert _holds('extra == "a"', frozenset({"b"})) is False

    def test_pep685_normalisation_both_sides(self) -> None:
        """The marker name and the active names both normalise before matching."""
        assert _holds('extra == "Fast.Path"', frozenset({"fast-path"})) is True
        assert _holds('extra != "Fast.Path"', frozenset({"fast_path"})) is False

    def test_env_atom_combined_with_extra(self) -> None:
        """A marker mixing an environment atom with a negated extra holds only
        where both do."""
        assert _holds('sys_platform == "linux" and extra != "cpu"', frozenset({"gpu"}))
        assert not _holds(
            'sys_platform == "linux" and extra != "cpu"', frozenset({"cpu"})
        )
        assert not _holds(
            'sys_platform == "win32" and extra != "cpu"', frozenset({"gpu"})
        )

    def test_extra_defaults_to_empty_when_unbound(self) -> None:
        """A marker evaluated with no ``extra`` binding sees no extras active."""
        assert dependency_marker_holds(Marker('extra != "cpu"'), _ENV) is True
        assert dependency_marker_holds(Marker('extra == "cpu"'), _ENV) is False

    def test_membership_set_variables_empty_at_resolve_time(self) -> None:
        """The lockfile-only set variables stay seeded empty, so a marker that
        tests ``extras`` evaluates False rather than raising."""
        assert _holds('"docs" not in extras', frozenset({"cpu"})) is True
        assert _holds('"docs" in extras', frozenset({"cpu"})) is False


class TestDependencyMarkerHoldsScalarExtra:
    def test_scalar_extra_binding(self) -> None:
        """A single extra name bound as a string is read as its one-name set."""
        env: dict[str, str] = {**_ENV, "extra": "cpu"}
        assert dependency_marker_holds(Marker('extra == "cpu"'), env) is True
        assert dependency_marker_holds(Marker('extra != "cpu"'), env) is False

    @pytest.mark.parametrize(
        ("marker_text", "want"),
        [
            ('python_version < "3.10"', False),
            ('python_version >= "3.11"', True),
            ('sys_platform == "linux"', True),
            ('sys_platform == "win32"', False),
        ],
    )
    def test_environment_only_markers_unaffected(
        self, marker_text: str, want: bool
    ) -> None:
        """A marker naming no extra evaluates against the environment alone."""
        assert dependency_marker_holds(Marker(marker_text), _ENV) is want
