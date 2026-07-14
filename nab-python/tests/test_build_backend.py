"""Tests for nab_python.build_backend.

Covers the static path (``extract_static_metadata``) and the
dynamic dispatch in ``extract_metadata``, including the
``BuildBackendError`` raised when the caller did not supply the
``NabProjectConfig`` the runner needs.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nab_python._provider.metadata_resolver import (
    extend_with_extras,
    parse_pyproject_deps,
)
from nab_python._vendor.packaging.specifiers import SpecifierSet
from nab_python._vendor.packaging.version import Version
from nab_python.build_backend import (
    BuildBackendError,
    extract_metadata,
    extract_static_metadata,
)
from nab_python.metadata import parse_metadata
from nab_python.requirements_file import InvalidProjectRequirementError


def _write_pyproject(tmp: Path, body: str) -> Path:
    (tmp / "pyproject.toml").write_text(body, encoding="utf-8")
    return tmp


class TestExtractStaticMetadata:
    def test_minimal_project(self, tmp_path: Path) -> None:
        _write_pyproject(
            tmp_path,
            """
            [project]
            name = "Foo-Bar"
            version = "1.2.3"
            """,
        )
        meta = extract_static_metadata(tmp_path)
        assert meta is not None
        assert meta.name == "foo-bar"
        assert meta.version == Version("1.2.3")
        assert meta.requires_python is None
        assert meta.requires_dist == []
        assert meta.provides_extra == []

    def test_full_project(self, tmp_path: Path) -> None:
        _write_pyproject(
            tmp_path,
            """
            [project]
            name = "foo"
            version = "1.0"
            requires-python = ">=3.10"
            dependencies = ["requests>=2.0", "click<9"]
            [project.optional-dependencies]
            dev = ["pytest>=8"]
            docs = ["sphinx>=7"]
            """,
        )
        meta = extract_static_metadata(tmp_path)
        assert meta is not None
        assert meta.requires_python == SpecifierSet(">=3.10")
        names = sorted(r.name for r in meta.requires_dist)
        assert "requests" in names
        assert "click" in names
        assert "pytest" in names
        assert "sphinx" in names
        assert sorted(meta.provides_extra) == ["dev", "docs"]
        pytest_req = next(r for r in meta.requires_dist if r.name == "pytest")
        assert pytest_req.marker is not None
        assert 'extra == "dev"' in str(pytest_req.marker)

    def test_missing_pyproject_returns_none(self, tmp_path: Path) -> None:
        assert extract_static_metadata(tmp_path) is None

    def test_malformed_toml_returns_none(self, tmp_path: Path) -> None:
        _write_pyproject(tmp_path, "this is not toml [")
        assert extract_static_metadata(tmp_path) is None

    def test_non_utf8_toml_returns_none(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_bytes(
            b'[project]\nname = "foo"\nversion = "1.0"\ndescription = "\xe9"\n'
        )
        assert extract_static_metadata(tmp_path) is None

    def test_no_project_table_returns_none(self, tmp_path: Path) -> None:
        _write_pyproject(tmp_path, '[build-system]\nrequires = ["setuptools"]\n')
        assert extract_static_metadata(tmp_path) is None

    def test_dynamic_dependencies_returns_none(self, tmp_path: Path) -> None:
        _write_pyproject(
            tmp_path,
            """
            [project]
            name = "foo"
            version = "1.0"
            dynamic = ["dependencies"]
            """,
        )
        assert extract_static_metadata(tmp_path) is None

    def test_dynamic_optional_dependencies_returns_none(self, tmp_path: Path) -> None:
        _write_pyproject(
            tmp_path,
            """
            [project]
            name = "foo"
            version = "1.0"
            dynamic = ["optional-dependencies"]
            """,
        )
        assert extract_static_metadata(tmp_path) is None

    def test_dynamic_version_placeholder_returns_none(self, tmp_path: Path) -> None:
        _write_pyproject(
            tmp_path,
            """
            [project]
            name = "foo"
            version = "0.0.0"
            dynamic = ["version"]
            """,
        )
        assert extract_static_metadata(tmp_path) is None

    def test_dynamic_requires_python_returns_none(self, tmp_path: Path) -> None:
        _write_pyproject(
            tmp_path,
            """
            [project]
            name = "foo"
            version = "1.0"
            dynamic = ["requires-python"]
            """,
        )
        assert extract_static_metadata(tmp_path) is None

    def test_dynamic_non_version_keeps_static_version(self, tmp_path: Path) -> None:
        _write_pyproject(
            tmp_path,
            """
            [project]
            name = "foo"
            version = "1.0"
            dynamic = ["readme"]
            """,
        )
        meta = extract_static_metadata(tmp_path)
        assert meta is not None
        assert meta.version == Version("1.0")

    def test_missing_name_returns_none(self, tmp_path: Path) -> None:
        _write_pyproject(tmp_path, '[project]\nversion = "1.0"\n')
        assert extract_static_metadata(tmp_path) is None

    def test_missing_version_returns_none(self, tmp_path: Path) -> None:
        _write_pyproject(tmp_path, '[project]\nname = "foo"\n')
        assert extract_static_metadata(tmp_path) is None

    def test_invalid_version_raises(self, tmp_path: Path) -> None:
        """A static version that is not valid PEP 440 is corrupt; raise."""
        _write_pyproject(
            tmp_path, '[project]\nname = "foo"\nversion = "not.a.version!"\n'
        )
        with pytest.raises(
            InvalidProjectRequirementError,
            match=r"invalid \[project\]\.version 'not.a.version!'",
        ):
            extract_static_metadata(tmp_path)

    def test_invalid_requires_python_raises(self, tmp_path: Path) -> None:
        """A bare "3.11" has no operator, so it is not a valid specifier; raise."""
        _write_pyproject(
            tmp_path,
            '[project]\nname = "foo"\nversion = "1.0"\nrequires-python = "3.11"\n',
        )
        with pytest.raises(
            InvalidProjectRequirementError,
            match=r"invalid \[project\]\.requires-python '3.11'",
        ):
            extract_static_metadata(tmp_path)

    def test_unparseable_dep_raises(self, tmp_path: Path) -> None:
        """A dep string that is not valid PEP 508 is invalid metadata; raise."""
        _write_pyproject(
            tmp_path,
            """
            [project]
            name = "foo"
            version = "1.0"
            dependencies = ["requests>=2.0", "this is not a valid req"]
            """,
        )
        with pytest.raises(InvalidProjectRequirementError, match="invalid requirement"):
            extract_static_metadata(tmp_path)

    def test_unparseable_optional_dep_raises(self, tmp_path: Path) -> None:
        """An optional dep that is not valid PEP 508 is invalid metadata; raise."""
        _write_pyproject(
            tmp_path,
            """
            [project]
            name = "foo"
            version = "1.0"
            [project.optional-dependencies]
            bad = ["@@@"]
            ok = ["requests"]
            """,
        )
        with pytest.raises(InvalidProjectRequirementError, match="invalid requirement"):
            extract_static_metadata(tmp_path)

    def test_dependencies_with_non_string_entries_raises(self, tmp_path: Path) -> None:
        """A non-string entry is a structural error that raises."""
        _write_pyproject(
            tmp_path,
            """
            [project]
            name = "foo"
            version = "1.0"
            dependencies = ["valid", 42]
            """,
        )
        with pytest.raises(
            InvalidProjectRequirementError,
            match=r"\[project\]\.dependencies must be an array of strings",
        ):
            extract_static_metadata(tmp_path)

    def test_dependencies_not_list_raises(self, tmp_path: Path) -> None:
        _write_pyproject(
            tmp_path,
            '[project]\nname = "foo"\nversion = "1.0"\ndependencies = "not-a-list"\n',
        )
        with pytest.raises(
            InvalidProjectRequirementError,
            match=r"\[project\]\.dependencies must be an array of strings",
        ):
            extract_static_metadata(tmp_path)

    def test_dependencies_table_raises(self, tmp_path: Path) -> None:
        _write_pyproject(
            tmp_path,
            """
            [project]
            name = "foo"
            version = "1.0"
            [project.dependencies]
            a = "b"
            """,
        )
        with pytest.raises(
            InvalidProjectRequirementError,
            match=r"\[project\]\.dependencies must be an array of strings",
        ):
            extract_static_metadata(tmp_path)

    def test_optional_deps_not_dict_raises(self, tmp_path: Path) -> None:
        _write_pyproject(
            tmp_path,
            """
            [project]
            name = "foo"
            version = "1.0"
            optional-dependencies = "not-a-dict"
            """,
        )
        with pytest.raises(
            InvalidProjectRequirementError,
            match=r"\[project\]\.optional-dependencies must be a table",
        ):
            extract_static_metadata(tmp_path)

    def test_optional_deps_value_not_list_raises(self, tmp_path: Path) -> None:
        _write_pyproject(
            tmp_path,
            """
            [project]
            name = "foo"
            version = "1.0"
            [project.optional-dependencies]
            dev = "not-a-list"
            ok = ["pytest"]
            """,
        )
        with pytest.raises(
            InvalidProjectRequirementError,
            match=r"extra 'dev' must be an array of strings",
        ):
            extract_static_metadata(tmp_path)

    def test_requires_python_not_str_raises(self, tmp_path: Path) -> None:
        """A non-string requires-python is corrupt, not "no constraint"; raise."""
        _write_pyproject(
            tmp_path,
            """
            [project]
            name = "foo"
            version = "1.0"
            requires-python = 42
            """,
        )
        with pytest.raises(
            InvalidProjectRequirementError,
            match=r"\[project\]\.requires-python must be a string, got int",
        ):
            extract_static_metadata(tmp_path)

    def test_pyproject_unreadable_returns_none(self, tmp_path: Path) -> None:
        # A directory in place of pyproject.toml: ``is_file`` is False so
        # the static reader bails before the read.
        (tmp_path / "pyproject.toml").mkdir()
        assert extract_static_metadata(tmp_path) is None

    def test_pyproject_read_oserror_returns_none(self, tmp_path: Path) -> None:
        # An OSError raised between ``is_file`` and ``read_text`` (the
        # races a regular file becoming unreadable mid-call) falls
        # through to the same "treat as missing" branch.
        _write_pyproject(tmp_path, '[project]\nname = "foo"\nversion = "1.0"\n')
        extract_static_metadata.cache_clear()
        with patch.object(Path, "read_text", side_effect=PermissionError("locked")):
            assert extract_static_metadata(tmp_path) is None
        extract_static_metadata.cache_clear()

    def test_extra_marker_combined_with_existing(self, tmp_path: Path) -> None:
        _write_pyproject(
            tmp_path,
            """
            [project]
            name = "foo"
            version = "1.0"
            [project.optional-dependencies]
            dev = ['pytest ; python_version >= "3.10"']
            """,
        )
        meta = extract_static_metadata(tmp_path)
        assert meta is not None
        pytest_req = next(r for r in meta.requires_dist if r.name == "pytest")
        marker_str = str(pytest_req.marker)
        assert "python_version" in marker_str
        assert 'extra == "dev"' in marker_str

    def test_dynamic_other_fields_passes(self, tmp_path: Path) -> None:
        # Marking only ``version`` dynamic should NOT block static deps
        _write_pyproject(
            tmp_path,
            """
            [project]
            name = "foo"
            version = "1.0"
            dynamic = ["readme"]
            dependencies = ["requests"]
            """,
        )
        meta = extract_static_metadata(tmp_path)
        assert meta is not None
        names = [r.name for r in meta.requires_dist]
        assert "requests" in names


class TestExtractMetadata:
    def test_returns_static_when_available(self, tmp_path: Path) -> None:
        _write_pyproject(
            tmp_path,
            '[project]\nname = "foo"\nversion = "1.0"\n',
        )
        meta = extract_metadata(tmp_path)
        assert meta.name == "foo"

    def test_dynamic_path_without_transport_raises(self, tmp_path: Path) -> None:
        """The dynamic path requires both ``transport`` and ``config``;
        callers that pass neither (the historical static-only API)
        keep getting a clean ``BuildBackendError`` rather than a
        surprise subprocess.
        """
        _write_pyproject(
            tmp_path,
            """
            [project]
            name = "foo"
            version = "1.0"
            dynamic = ["dependencies"]
            """,
        )
        with pytest.raises(BuildBackendError, match="dynamic-metadata path"):
            extract_metadata(tmp_path)

    def test_dynamic_path_with_config_invokes_runner(self, tmp_path: Path) -> None:
        """When config is supplied, the dynamic path imports and calls
        ``run_build_backend``.  We mock the runner so the test stays
        hermetic: covering the import + happy-path call.
        """
        _write_pyproject(
            tmp_path,
            """
            [project]
            name = "foo"
            version = "1.0"
            dynamic = ["dependencies"]
            """,
        )
        sentinel = MagicMock(name="WheelMetadata")
        with patch(
            "nab_python._build.runner.run_build_backend",
            return_value=sentinel,
        ) as m:
            result = extract_metadata(tmp_path, config=MagicMock(name="config"))
        assert result is sentinel
        m.assert_called_once()

    def test_dynamic_path_remaps_runner_error(self, tmp_path: Path) -> None:
        """``BuildBackendError`` from the runner is re-raised under the
        legacy class so existing call sites' ``except`` clauses
        continue to match.
        """
        _write_pyproject(
            tmp_path,
            """
            [project]
            name = "foo"
            version = "1.0"
            dynamic = ["dependencies"]
            """,
        )
        from nab_python._build.runner import (
            BuildBackendError as RunnerError,
        )

        with (
            patch(
                "nab_python._build.runner.run_build_backend",
                side_effect=RunnerError("backend exploded"),
            ),
            pytest.raises(BuildBackendError, match="backend exploded"),
        ):
            extract_metadata(tmp_path, config=MagicMock(name="config"))


class TestStaticExtractAugmentParity:
    """Pin the static extractor against the parallel augment and index paths.

    Guards the two places they could silently diverge: extra-name
    normalisation and malformed-dependency handling.
    """

    def test_non_canonical_extra_canonicalized(self, tmp_path: Path) -> None:
        _write_pyproject(
            tmp_path,
            """
            [project]
            name = "foo"
            version = "1.0"
            [project.optional-dependencies]
            "My.Extra" = ["click>=8"]
            """,
        )
        meta = extract_static_metadata(tmp_path)
        assert meta is not None
        assert meta.provides_extra == ["my-extra"]
        click = next(r for r in meta.requires_dist if r.name == "click")
        assert str(click.marker) == 'extra == "my-extra"'

    def test_non_canonical_extra_marker_matches_augment_path(
        self, tmp_path: Path
    ) -> None:
        optional = {"My.Extra": ["click>=8"]}
        _write_pyproject(
            tmp_path,
            """
            [project]
            name = "foo"
            version = "1.0"
            [project.optional-dependencies]
            "My.Extra" = ["click>=8"]
            """,
        )
        meta = extract_static_metadata(tmp_path)
        assert meta is not None
        bb_click = next(r for r in meta.requires_dist if r.name == "click")

        augment_rd: list = []
        extend_with_extras(augment_rd, optional)
        aug_click = next(r for r in augment_rd if r.name == "click")

        assert bb_click.marker is not None
        assert aug_click.marker is not None
        assert str(bb_click.marker) == str(aug_click.marker)
        assert bb_click.marker.evaluate({"extra": "my-extra"})
        assert aug_click.marker.evaluate({"extra": "my-extra"})

    def test_semicolon_in_extra_url_dep_not_dropped(self) -> None:
        """A direct-URL extra dep with a ``;`` survives instead of being dropped."""
        augment_rd: list = []
        extend_with_extras(augment_rd, {"net": ["bar @ https://h/a;b/p.tar.gz"]})

        bar = next((r for r in augment_rd if r.name == "bar"), None)
        assert bar is not None
        assert bar.url == "https://h/a;b/p.tar.gz"
        assert str(bar.marker) == 'extra == "net"'

    def test_malformed_static_dep_raises_like_metadata(self, tmp_path: Path) -> None:
        """Every dep reader rejects a malformed dependency, not drops it.

        The static build-backend reader, the pyproject augment path, and the
        METADATA parser each raise on an invalid PEP 508 string, so the version
        is rejected rather than resolved with the dependency dropped.
        """
        deps = ["requests>=2", "this is not a valid !! req"]
        _write_pyproject(
            tmp_path,
            """
            [project]
            name = "foo"
            version = "1.0"
            dependencies = [
                "requests>=2",
                "this is not a valid !! req",
            ]
            """,
        )
        with pytest.raises(InvalidProjectRequirementError, match="invalid requirement"):
            extract_static_metadata(tmp_path)
        with pytest.raises(InvalidProjectRequirementError, match="invalid requirement"):
            parse_pyproject_deps(deps)

        md = (
            "Metadata-Version: 2.3\nName: foo\nVersion: 1.0\n"
            "Requires-Dist: this is not a valid !! req\n"
        )
        with pytest.raises(ValueError, match="Expected"):
            parse_metadata(md)
