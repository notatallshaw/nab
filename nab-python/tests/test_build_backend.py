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

from nab_python._vendor.packaging.specifiers import SpecifierSet
from nab_python._vendor.packaging.version import Version
from nab_python.build_backend import (
    BuildBackendError,
    extract_metadata,
    extract_static_metadata,
)


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
        # The pytest entry has an extra marker
        pytest_req = next(r for r in meta.requires_dist if r.name == "pytest")
        assert pytest_req.marker is not None
        assert 'extra == "dev"' in str(pytest_req.marker)

    def test_missing_pyproject_returns_none(self, tmp_path: Path) -> None:
        assert extract_static_metadata(tmp_path) is None

    def test_malformed_toml_returns_none(self, tmp_path: Path) -> None:
        _write_pyproject(tmp_path, "this is not toml [")
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

    def test_missing_name_returns_none(self, tmp_path: Path) -> None:
        _write_pyproject(tmp_path, '[project]\nversion = "1.0"\n')
        assert extract_static_metadata(tmp_path) is None

    def test_missing_version_returns_none(self, tmp_path: Path) -> None:
        _write_pyproject(tmp_path, '[project]\nname = "foo"\n')
        assert extract_static_metadata(tmp_path) is None

    def test_invalid_version_returns_none(self, tmp_path: Path) -> None:
        _write_pyproject(
            tmp_path, '[project]\nname = "foo"\nversion = "not.a.version!"\n'
        )
        assert extract_static_metadata(tmp_path) is None

    def test_unparseable_dep_skipped_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _write_pyproject(
            tmp_path,
            """
            [project]
            name = "foo"
            version = "1.0"
            dependencies = ["requests>=2.0", "this is not a valid req"]
            """,
        )
        meta = extract_static_metadata(tmp_path)
        assert meta is not None
        names = [r.name for r in meta.requires_dist]
        assert "requests" in names
        assert any("unparseable" in rec.message for rec in caplog.records)

    def test_unparseable_optional_dep_skipped(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
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
        meta = extract_static_metadata(tmp_path)
        assert meta is not None
        names = [r.name for r in meta.requires_dist]
        assert "requests" in names
        assert any("unparseable" in rec.message for rec in caplog.records)

    def test_dependencies_with_non_string_entries_skipped(self, tmp_path: Path) -> None:
        """A TOML array with mixed types skips the non-string entries."""
        _write_pyproject(
            tmp_path,
            """
            [project]
            name = "foo"
            version = "1.0"
            dependencies = ["valid", 42]
            """,
        )
        meta = extract_static_metadata(tmp_path)
        assert meta is not None
        names = [r.name for r in meta.requires_dist]
        assert names == ["valid"]

    def test_dependencies_not_list_ignored(self, tmp_path: Path) -> None:
        _write_pyproject(
            tmp_path,
            '[project]\nname = "foo"\nversion = "1.0"\ndependencies = "not-a-list"\n',
        )
        meta = extract_static_metadata(tmp_path)
        assert meta is not None
        assert meta.requires_dist == []

    def test_optional_deps_not_dict_ignored(self, tmp_path: Path) -> None:
        _write_pyproject(
            tmp_path,
            """
            [project]
            name = "foo"
            version = "1.0"
            optional-dependencies = "not-a-dict"
            """,
        )
        meta = extract_static_metadata(tmp_path)
        assert meta is not None
        assert meta.provides_extra == []

    def test_optional_deps_value_not_list_ignored(self, tmp_path: Path) -> None:
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
        meta = extract_static_metadata(tmp_path)
        assert meta is not None
        names = [r.name for r in meta.requires_dist]
        assert "pytest" in names

    def test_requires_python_not_str_ignored(self, tmp_path: Path) -> None:
        _write_pyproject(
            tmp_path,
            """
            [project]
            name = "foo"
            version = "1.0"
            requires-python = 42
            """,
        )
        meta = extract_static_metadata(tmp_path)
        assert meta is not None
        assert meta.requires_python is None

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
