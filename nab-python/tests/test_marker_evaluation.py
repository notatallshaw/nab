"""Marker-evaluation semantics the resolver depends on.

The PubGrub provider gates marker-conditioned dependencies on
``nab_python._vendor.packaging.markers.Marker.evaluate``. The vendored
snapshot tracks packaging PR #1182 (unreleased), so these tests pin the
boolean results for realistic markers and the deliberate string-key
operator semantics, so a future re-vendor cannot silently change which
dependencies a resolve includes.

Audited differentially against released packaging 24.2/25.0/26.2: every
realistic marker evaluates identically across all three (and nab matches
26.2 exactly); the only divergences are pathological forms no real
package writes. See kb 2026-06-04-marker-eval-operator-audited-clean.
"""

from __future__ import annotations

import pytest

from nab_python._vendor.packaging.markers import Marker

LINUX_CP311: dict[str, str] = {
    "os_name": "posix",
    "sys_platform": "linux",
    "platform_machine": "x86_64",
    "platform_system": "Linux",
    "platform_release": "6.6.0",
    "platform_version": "#1 SMP",
    "platform_python_implementation": "CPython",
    "implementation_name": "cpython",
    "python_version": "3.11",
    "python_full_version": "3.11.2",
    "implementation_version": "3.11.2",
    "extra": "",
}

PYPY_LINUX: dict[str, str] = {
    **LINUX_CP311,
    "platform_python_implementation": "PyPy",
    "implementation_name": "pypy",
    "python_version": "3.9",
    "python_full_version": "3.9.18",
    "implementation_version": "7.3.13",
}

CP314_PRE: dict[str, str] = {
    **LINUX_CP311,
    "python_version": "3.14",
    "python_full_version": "3.14.0a1",
    "implementation_version": "3.14.0a1",
}


def _ev(text: str, env: dict[str, str]) -> bool:
    return Marker(text).evaluate(env)


class TestRealisticMarkers:
    """Version-key comparisons and string-key equality/membership.

    These are the marker forms real packages write; they must match
    pip/uv (every packaging version agrees) or nab resolves differently.
    """

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ('python_version >= "3.8"', True),
            ('python_version < "3.8"', False),
            ('python_version == "3.11"', True),
            ('python_version != "3.11"', False),
            ('python_version == "3.11.0"', True),
            ('python_full_version >= "3.11.0"', True),
            ('python_full_version < "3.11.2"', False),
            ('python_full_version >= "3.12"', False),
            ('sys_platform == "linux"', True),
            ('sys_platform == "win32"', False),
            ('sys_platform != "win32"', True),
            ('os_name == "posix"', True),
            ('platform_machine == "x86_64"', True),
            ('platform_system == "Linux"', True),
            ('implementation_name == "cpython"', True),
            ('platform_python_implementation == "CPython"', True),
            ('"x86" in platform_machine', True),
            ('"arm" in platform_machine', False),
            ('"arm" not in platform_machine', True),
        ],
    )
    def test_linux_cpython(self, text: str, expected: bool) -> None:
        assert _ev(text, LINUX_CP311) is expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ('platform_python_implementation == "PyPy"', True),
            ('implementation_name == "pypy"', True),
            ('implementation_version >= "7.3"', True),
            ('python_full_version == "3.9.18"', True),
        ],
    )
    def test_pypy(self, text: str, expected: bool) -> None:
        assert _ev(text, PYPY_LINUX) is expected


class TestPrereleaseBoundaries:
    """PEP 440 prerelease semantics through the Specifier path."""

    def test_prerelease_matches_own_lower_bound(self) -> None:
        assert _ev('python_full_version >= "3.14.0a1"', CP314_PRE) is True

    def test_exclusive_upper_excludes_own_prerelease(self) -> None:
        # <V excludes prereleases of V, so 3.14.0a1 is not < "3.14".
        assert _ev('python_full_version < "3.14"', CP314_PRE) is False

    def test_minor_version_unaffected_by_prerelease(self) -> None:
        assert _ev('python_version == "3.14"', CP314_PRE) is True


class TestCompound:
    """and / or / parenthesised grouping."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ('python_version >= "3.8" and sys_platform == "linux"', True),
            ('python_version >= "3.8" and sys_platform == "win32"', False),
            ('python_version < "3.8" or os_name == "posix"', True),
            (
                '(python_version == "3.11" and sys_platform == "linux")'
                ' or platform_machine == "arm64"',
                True,
            ),
        ],
    )
    def test_compound(self, text: str, expected: bool) -> None:
        assert _ev(text, LINUX_CP311) is expected


class TestStringKeyOrderingSemantics:
    """Deliberate packaging 26.x behavior for ordering on string keys.

    ``<`` and ``>`` are always False and ``<=``/``>=`` reduce to equality
    for non-version markers. Older packaging compared lexicographically
    (``implementation_name < "darwin"`` was True); pinning catches a
    re-vendor that reverts to that.
    """

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ('sys_platform < "linux"', False),
            ('sys_platform > "linux"', False),
            ('sys_platform <= "linux"', True),
            ('sys_platform >= "linux"', True),
            ('sys_platform <= "windows"', False),
            ('implementation_name < "darwin"', False),
        ],
    )
    def test_string_ordering(self, text: str, expected: bool) -> None:
        assert _ev(text, LINUX_CP311) is expected


class TestStringKeyVersionLikeRhsIsRobust:
    """A version-like rhs on a string key returns a bool, never raises.

    Older packaging raised InvalidVersion here; nab stays robust.
    """

    @pytest.mark.parametrize(
        "text",
        [
            'sys_platform == "0"',
            'os_name >= "3.8"',
            'platform_machine != "1.0"',
        ],
    )
    def test_no_raise(self, text: str) -> None:
        assert isinstance(_ev(text, LINUX_CP311), bool)


class TestExtraNormalization:
    """metadata-context ``extra`` with PEP 685 name normalization."""

    @pytest.mark.parametrize(
        ("extra_value", "text", "expected"),
        [
            ("test", 'extra == "test"', True),
            ("test", 'extra == "docs"', False),
            ("dev-tools", 'extra == "dev_tools"', True),
            ("dev_tools", 'extra == "dev-tools"', True),
            ("", 'extra == "test"', False),
        ],
    )
    def test_extra(self, extra_value: str, text: str, expected: bool) -> None:
        assert _ev(text, {**LINUX_CP311, "extra": extra_value}) is expected


class TestSerializationRoundTrip:
    """``str(Marker(...))`` must re-parse to a marker that evaluates the same.

    A double-parenthesized group nested inside an and/or expression needs its
    parentheses for precedence, so serialization has to keep them.
    """

    @pytest.mark.parametrize(
        ("text", "env", "expected"),
        [
            (
                'python_version < "3.10" and '
                '((sys_platform == "linux" or sys_platform == "darwin"))',
                {"python_version": "3.12", "sys_platform": "darwin"},
                False,
            ),
            (
                'python_version < "3.10" and '
                '((sys_platform == "linux" or sys_platform == "darwin"))',
                {"python_version": "3.9", "sys_platform": "darwin"},
                True,
            ),
            (
                'extra != "c" or ((python_version > "3.8") and extra != "c") '
                'and ((python_version != "3.10" or (extra != "a")))',
                {"python_version": "3.8", "extra": "c"},
                False,
            ),
        ],
    )
    def test_nested_double_parens_round_trip(
        self, text: str, env: dict[str, str], expected: bool
    ) -> None:
        marker = Marker(text)
        assert marker.evaluate(env) is expected
        assert Marker(str(marker)).evaluate(env) is expected


class TestSetMarkers:
    """lock_file-context set membership (extras / dependency_groups)."""

    @pytest.mark.parametrize(
        ("text", "key", "value", "expected"),
        [
            ('"test" in extras', "extras", frozenset({"test"}), True),
            ('"test" not in extras', "extras", frozenset({"docs"}), True),
            (
                '"grp" in dependency_groups',
                "dependency_groups",
                frozenset({"grp"}),
                True,
            ),
        ],
    )
    def test_set_membership(
        self, text: str, key: str, value: frozenset[str], expected: bool
    ) -> None:
        env: dict[str, str | frozenset[str]] = {
            k: v for k, v in LINUX_CP311.items() if k != "extra"
        }
        env[key] = value
        assert Marker(text).evaluate(env, context="lock_file") is expected
