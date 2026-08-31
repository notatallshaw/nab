"""Tests for nab_project.build_backend.

Covers the static path (``extract_static_metadata``) and the
dynamic dispatch in ``extract_metadata``, including the
``BuildBackendError`` raised when the caller did not supply the
``ResolveInputs`` the runner needs, and the build-policy page's account
of when the dynamic path runs.
"""

from __future__ import annotations

import errno
import sys
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import tomli

from nab_project.build_backend import (
    BuildBackendError,
    extract_metadata,
    extract_static_metadata,
)
from nab_provider._provider.metadata_resolver import (
    extend_with_extras,
    parse_pyproject_deps,
)
from nab_provider._vendor.packaging.specifiers import SpecifierSet
from nab_provider._vendor.packaging.version import Version
from nab_provider.metadata import parse_metadata
from nab_provider.requirements_file import InvalidProjectRequirementError


def _write_pyproject(tmp: Path, body: str) -> Path:
    (tmp / "pyproject.toml").write_text(body, encoding="utf-8")
    return tmp


DOCS_BUILD_POLICY = (
    Path(__file__).resolve().parents[2] / "docs" / "reference" / "build-policy.md"
)


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

    def test_unsearchable_parent_reports_the_errno(
        self,
        tmp_path: Path,
        deny_access: Callable[[Path], AbstractContextManager[None]],
    ) -> None:
        # EACCES leaves the contents unknown, so it is neither "no static
        # metadata" nor a missing file: the errno is reported.
        _write_pyproject(tmp_path, '[project]\nname = "foo"\nversion = "1.0"\n')
        with (
            deny_access(tmp_path / "pyproject.toml"),
            pytest.raises(BuildBackendError, match="could not read.*Permission denied"),
        ):
            extract_static_metadata(tmp_path)

    def test_malformed_toml_reports_the_parse_error(self, tmp_path: Path) -> None:
        """Text that does not parse leaves the contents unknown."""
        _write_pyproject(tmp_path, "this is not toml [")
        with pytest.raises(
            BuildBackendError, match="could not read pyproject.toml"
        ) as caught:
            extract_static_metadata(tmp_path)
        assert isinstance(caught.value.__cause__, tomli.TOMLDecodeError)

    def test_non_utf8_toml_reports_the_decode_error(self, tmp_path: Path) -> None:
        """Bytes that do not decode leave the contents unknown too."""
        (tmp_path / "pyproject.toml").write_bytes(
            b'[project]\nname = "foo"\nversion = "1.0"\ndescription = "\xe9"\n'
        )
        with pytest.raises(
            BuildBackendError, match="could not read pyproject.toml"
        ) as caught:
            extract_static_metadata(tmp_path)
        assert isinstance(caught.value.__cause__, UnicodeDecodeError)

    def test_oversized_integer_toml_reports_the_parse_error(
        self, tmp_path: Path, oversized_integer: str
    ) -> None:
        """An integer past the int-from-string limit does not parse either."""
        _write_pyproject(
            tmp_path,
            '[project]\nname = "foo"\nversion = "1.0"\n'
            f"[tool.other]\ncount = {oversized_integer}\n",
        )
        with pytest.raises(
            BuildBackendError, match="could not read pyproject.toml"
        ) as caught:
            extract_static_metadata(tmp_path)
        assert isinstance(caught.value.__cause__, tomli.TOMLDecodeError)

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

    def test_oversized_version_raises(self, tmp_path: Path) -> None:
        """A release segment past the int-from-string limit is corrupt too."""
        oversized = "1" * (sys.get_int_max_str_digits() + 1)
        _write_pyproject(
            tmp_path, f'[project]\nname = "foo"\nversion = "{oversized}"\n'
        )
        with pytest.raises(
            InvalidProjectRequirementError,
            match=r"invalid \[project\]\.version",
        ):
            extract_static_metadata(tmp_path)

    def test_oversized_requires_python_raises(self, tmp_path: Path) -> None:
        """A specifier parses fine and only fails when something compares it."""
        oversized = "1" * (sys.get_int_max_str_digits() + 1)
        _write_pyproject(
            tmp_path,
            f'[project]\nname = "foo"\nversion = "1.0"\n'
            f'requires-python = ">={oversized}"\n',
        )
        with pytest.raises(
            InvalidProjectRequirementError,
            match=r"invalid \[project\]\.requires-python",
        ):
            extract_static_metadata(tmp_path)

    def test_oversized_dependency_version_raises(self, tmp_path: Path) -> None:
        """The same deferred conversion applies to a dependency's specifier."""
        oversized = "1" * (sys.get_int_max_str_digits() + 1)
        _write_pyproject(
            tmp_path,
            f'[project]\nname = "foo"\nversion = "1.0"\n'
            f'dependencies = ["bar>={oversized}"]\n',
        )
        with pytest.raises(InvalidProjectRequirementError, match="invalid requirement"):
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

    def test_over_nested_marker_raises(
        self, tmp_path: Path, over_nested_marker: str
    ) -> None:
        """A marker the parser cannot recurse through is a rejected dependency."""
        _write_pyproject(
            tmp_path,
            f'[project]\nname = "foo"\nversion = "1.0"\n'
            f'dependencies = ["bar ; {over_nested_marker}"]\n',
        )
        with pytest.raises(InvalidProjectRequirementError, match="invalid requirement"):
            extract_static_metadata(tmp_path)

    def test_over_nested_marker_on_an_optional_dep_raises(
        self, tmp_path: Path, over_nested_marker: str
    ) -> None:
        """An optional dependency parses on the extras path, and fails there."""
        _write_pyproject(
            tmp_path,
            f'[project]\nname = "foo"\nversion = "1.0"\n'
            f"[project.optional-dependencies]\n"
            f'fast = ["bar ; {over_nested_marker}"]\n',
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

    def test_pyproject_is_a_directory_reports_the_file_type(
        self, tmp_path: Path
    ) -> None:
        """A directory in place of the file is a read failure, not absence."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.mkdir()
        with pytest.raises(BuildBackendError) as caught:
            extract_static_metadata(tmp_path)
        assert str(caught.value) == f"{pyproject} exists but is not a regular file"

    def test_pyproject_vanishing_before_the_read_returns_none(
        self, tmp_path: Path
    ) -> None:
        # The presence check is racy: a file deleted between the stat and
        # the read is missing.
        _write_pyproject(tmp_path, '[project]\nname = "foo"\nversion = "1.0"\n')
        gone = FileNotFoundError(errno.ENOENT, "No such file or directory")
        extract_static_metadata.cache_clear()
        with patch.object(Path, "read_text", side_effect=gone):
            assert extract_static_metadata(tmp_path) is None
        extract_static_metadata.cache_clear()

    def test_pyproject_read_oserror_reports_the_errno(self, tmp_path: Path) -> None:
        # Any other read failure leaves the contents unknown, so it is
        # reported rather than reduced to "no static metadata".
        _write_pyproject(tmp_path, '[project]\nname = "foo"\nversion = "1.0"\n')
        broken = OSError(errno.EIO, "Input/output error")
        extract_static_metadata.cache_clear()
        with (
            patch.object(Path, "read_text", side_effect=broken),
            pytest.raises(BuildBackendError, match="could not read.*Input/output"),
        ):
            extract_static_metadata(tmp_path)
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
        with pytest.raises(
            BuildBackendError, match="dynamic-metadata path requires a ResolveInputs"
        ):
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
            "nab_project._build.runner.run_build_backend",
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
        from nab_project._build.runner import (
            BuildBackendError as RunnerError,
        )

        with (
            patch(
                "nab_project._build.runner.run_build_backend",
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


class TestBuildLocalDocSection:
    """The build-policy page's ``build-local`` section against the static read.

    A local checkout goes to a backend when ``extract_static_metadata``
    returns ``None`` for it, so the section names the shapes that produce
    one.  A read that raises instead, on an unreadable file or a corrupt
    static value, never reaches a backend and is outside that claim.
    """

    @staticmethod
    def _build_local_section() -> str:
        """Return the ``build-local`` section, down to the next heading."""
        text = DOCS_BUILD_POLICY.read_text(encoding="utf-8")
        start = text.index("## `build-local` (default)")
        return text[start:].split("\n## ", 1)[0]

    @pytest.mark.parametrize(
        "field",
        ["dependencies", "optional-dependencies", "version", "requires-python"],
    )
    def test_section_names_every_dynamic_field(
        self, field: str, tmp_path: Path
    ) -> None:
        _write_pyproject(
            tmp_path,
            f"""
            [project]
            name = "foo"
            version = "1.0"
            dynamic = ["{field}"]
            """,
        )
        assert extract_static_metadata(tmp_path) is None

        assert f"`{field}`" in self._build_local_section()

    def test_section_names_a_missing_project_table(self, tmp_path: Path) -> None:
        _write_pyproject(tmp_path, '[build-system]\nrequires = ["setuptools"]\n')
        assert extract_static_metadata(tmp_path) is None

        section = self._build_local_section()
        assert "missing" in section
        assert "`[project]`" in section
