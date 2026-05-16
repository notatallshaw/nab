"""Tests for the [tool.nab] config reader."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nab_python.config import (
    ConfigError,
    MatrixConfig,
    NabProjectConfig,
    ResolveMode,
    read_pyproject_config,
)
from nab_python.fetch import DEFAULT_INDEX_NAME, DEFAULT_INDEX_URL, IndexOverride
from nab_python.provider import (
    BuildPolicy,
    DistPolicy,
    LocalSource,
    ResolutionStrategy,
    VcsConfig,
    VcsPolicy,
    VcsSource,
)
from nab_python.workspace import WorkspaceConfig


def write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "pyproject.toml"
    p.write_text(body)
    return p


class TestDefaults:
    def test_no_tool_nab_table_returns_defaults(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[project]\nname = "x"\nversion = "0"\n')
        config = read_pyproject_config(path)
        assert config == NabProjectConfig()
        # Default index is PyPI
        assert config.indexes[0].name == DEFAULT_INDEX_NAME
        assert config.indexes[0].url == DEFAULT_INDEX_URL
        assert config.mode is ResolveMode.SPECIFIC
        assert config.dist_policy is DistPolicy.WHEEL_OR_SDIST
        assert config.build_policy is BuildPolicy.BUILD_LOCAL
        assert config.vcs == VcsConfig()
        assert config.matrix is None

    def test_empty_tool_nab_table_returns_defaults(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[tool.nab]\n")
        config = read_pyproject_config(path)
        assert config == NabProjectConfig()


class TestMode:
    def test_specific_explicit(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\nmode = "specific"\n')
        assert read_pyproject_config(path).mode is ResolveMode.SPECIFIC

    def test_universal_requires_matrix(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\nmode = "universal"\n')
        with pytest.raises(ConfigError, match="requires a \\[tool.nab.matrix\\]"):
            read_pyproject_config(path)

    def test_matrix_without_universal_mode_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab.matrix]\npython = ">=3.11"\nplatforms = ["linux_x86_64"]\n',
        )
        with pytest.raises(ConfigError, match="set mode = 'universal'"):
            read_pyproject_config(path)

    def test_invalid_mode_value(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\nmode = "bogus"\n')
        with pytest.raises(ConfigError, match="mode must be one of"):
            read_pyproject_config(path)

    def test_mode_must_be_string(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[tool.nab]\nmode = 1\n")
        with pytest.raises(ConfigError, match="mode must be a string"):
            read_pyproject_config(path)


class TestTopLevelKeys:
    def test_unknown_key_rejected(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\nbogus = "x"\n')
        with pytest.raises(ConfigError, match="unknown \\[tool.nab\\] keys"):
            read_pyproject_config(path)

    def test_tool_nab_must_be_table(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool]\nnab = "a string, not a table"\n')
        with pytest.raises(ConfigError, match="\\[tool.nab\\] must be a table"):
            read_pyproject_config(path)


class TestConstraints:
    def test_constraints_round_trip(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\nconstraints = ["urllib3<2", "click>=8"]\n')
        config = read_pyproject_config(path)
        assert config.constraints == ("urllib3<2", "click>=8")

    def test_constraints_must_be_list(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\nconstraints = "urllib3<2"\n')
        with pytest.raises(ConfigError, match="constraints must be a list"):
            read_pyproject_config(path)

    def test_constraints_entries_must_be_strings(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[tool.nab]\nconstraints = [1, 2]\n")
        with pytest.raises(ConfigError, match="constraints\\[0\\] must be a string"):
            read_pyproject_config(path)


class TestRequiresPython:
    def test_round_trip_specifier(self, tmp_path: Path) -> None:
        """A valid PEP 440 specifier round-trips as the raw string."""
        path = write(tmp_path, '[tool.nab]\nrequires-python = "==3.12.0"\n')
        assert read_pyproject_config(path).requires_python == "==3.12.0"

    def test_range_specifier_round_trips(self, tmp_path: Path) -> None:
        """A range specifier (``>=X,<Y``) round-trips as written."""
        path = write(tmp_path, '[tool.nab]\nrequires-python = ">=3.13,<3.14"\n')
        assert read_pyproject_config(path).requires_python == ">=3.13,<3.14"

    def test_bare_version_rejected(self, tmp_path: Path) -> None:
        """A bare version is not a valid specifier; reject with guidance."""
        path = write(tmp_path, '[tool.nab]\nrequires-python = "3.12"\n')
        with pytest.raises(
            ConfigError,
            match="requires-python must be a PEP 440 specifier",
        ):
            read_pyproject_config(path)

    def test_garbage_rejected(self, tmp_path: Path) -> None:
        """Free-form text is rejected with the same error."""
        path = write(tmp_path, '[tool.nab]\nrequires-python = "not a spec"\n')
        with pytest.raises(
            ConfigError,
            match="requires-python must be a PEP 440 specifier",
        ):
            read_pyproject_config(path)

    def test_must_be_string(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[tool.nab]\nrequires-python = 3\n")
        with pytest.raises(ConfigError, match="requires-python must be a string"):
            read_pyproject_config(path)


class TestUploadedPriorTo:
    def test_iso_string_with_z(self, tmp_path: Path) -> None:
        path = write(
            tmp_path, '[tool.nab]\nuploaded-prior-to = "2026-05-01T00:00:00Z"\n'
        )
        dt = read_pyproject_config(path).uploaded_prior_to
        assert dt == datetime(2026, 5, 1, tzinfo=timezone.utc)

    def test_iso_string_with_offset(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab]\nuploaded-prior-to = "2026-05-01T05:30:00+05:30"\n',
        )
        dt = read_pyproject_config(path).uploaded_prior_to
        assert dt is not None
        assert dt.utcoffset() == timedelta(hours=5, minutes=30)
        # Equivalent to 00:00 UTC.
        assert dt.astimezone(timezone.utc) == datetime(2026, 5, 1, tzinfo=timezone.utc)

    def test_naive_iso_string_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path, '[tool.nab]\nuploaded-prior-to = "2026-05-01T00:00:00"\n'
        )
        with pytest.raises(
            ConfigError, match="must include an explicit timezone offset"
        ):
            read_pyproject_config(path)

    def test_native_toml_datetime(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[tool.nab]\nuploaded-prior-to = 2026-05-01T00:00:00Z\n",
        )
        dt = read_pyproject_config(path).uploaded_prior_to
        assert dt == datetime(2026, 5, 1, tzinfo=timezone.utc)

    def test_naive_toml_datetime_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[tool.nab]\nuploaded-prior-to = 2026-05-01T00:00:00\n",
        )
        with pytest.raises(ConfigError, match="must have an explicit timezone offset"):
            read_pyproject_config(path)

    def test_duration_days(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\nuploaded-prior-to = "P4D"\n')
        before = datetime.now(timezone.utc) - timedelta(days=4)
        dt = read_pyproject_config(path).uploaded_prior_to
        after = datetime.now(timezone.utc) - timedelta(days=4)
        assert dt is not None
        assert before <= dt <= after
        assert dt.tzinfo is timezone.utc

    def test_duration_uses_explicit_anchor(self, tmp_path: Path) -> None:
        # When the caller passes an anchor, ``P<n>D`` resolves against
        # that anchor instead of ``now()``; this is the basis for
        # lockfile-anchored re-locks.
        path = write(tmp_path, '[tool.nab]\nuploaded-prior-to = "P4D"\n')
        anchor = datetime(2024, 1, 5, tzinfo=timezone.utc)
        config = read_pyproject_config(path, anchor=anchor)
        assert config.uploaded_prior_to == datetime(2024, 1, 1, tzinfo=timezone.utc)

    def test_duration_zero_days(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\nuploaded-prior-to = "P0D"\n')
        before = datetime.now(timezone.utc)
        dt = read_pyproject_config(path).uploaded_prior_to
        after = datetime.now(timezone.utc)
        assert dt is not None
        assert before <= dt <= after

    def test_duration_negative_rejected(self, tmp_path: Path) -> None:
        # ``P-1D`` is not a valid PnD duration; the regex requires \d+.
        path = write(tmp_path, '[tool.nab]\nuploaded-prior-to = "P-1D"\n')
        with pytest.raises(ConfigError, match="must be an ISO 8601 datetime"):
            read_pyproject_config(path)

    def test_duration_non_integer_rejected(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\nuploaded-prior-to = "P1.5D"\n')
        with pytest.raises(ConfigError, match="must be an ISO 8601 datetime"):
            read_pyproject_config(path)

    def test_duration_other_unit_rejected(self, tmp_path: Path) -> None:
        # Hours, weeks, months are not supported; only days.
        path = write(tmp_path, '[tool.nab]\nuploaded-prior-to = "PT4H"\n')
        with pytest.raises(ConfigError, match="must be an ISO 8601 datetime"):
            read_pyproject_config(path)

    def test_invalid_string_rejected(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\nuploaded-prior-to = "not-a-date"\n')
        with pytest.raises(ConfigError, match="must be an ISO 8601 datetime"):
            read_pyproject_config(path)

    def test_toml_local_date_rejected(self, tmp_path: Path) -> None:
        # Bare TOML date (no time) parses as ``datetime.date``; reject
        # with the type-mismatch path so the user gets a clear message
        # to add a timezone-aware datetime.
        path = write(tmp_path, "[tool.nab]\nuploaded-prior-to = 2026-05-01\n")
        with pytest.raises(
            ConfigError,
            match=(
                "must be a TOML offset-date-time, an ISO 8601"
                " datetime string with timezone, or a 'PnD' duration"
            ),
        ):
            read_pyproject_config(path)

    def test_wrong_type(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[tool.nab]\nuploaded-prior-to = 1\n")
        with pytest.raises(
            ConfigError,
            match=(
                "must be a TOML offset-date-time, an ISO 8601"
                " datetime string with timezone, or a 'PnD' duration"
            ),
        ):
            read_pyproject_config(path)


class TestUploadedPriorToPackage:
    """``[tool.nab.uploaded-prior-to-package]`` parses into a mapping."""

    def test_absent_table_is_empty_mapping(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[tool.nab]\n")
        assert read_pyproject_config(path).uploaded_prior_to_overrides == {}

    def test_disable_with_false(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[tool.nab.uploaded-prior-to-package]\n"
            "apache-airflow-providers-amazon = false\n",
        )
        overrides = read_pyproject_config(
            path, discover_workspace=False
        ).uploaded_prior_to_overrides
        assert overrides == {"apache-airflow-providers-amazon": None}

    def test_per_package_iso_datetime(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab.uploaded-prior-to-package]\nfoo = "2026-05-01T00:00:00Z"\n',
        )
        overrides = read_pyproject_config(
            path, discover_workspace=False
        ).uploaded_prior_to_overrides
        assert overrides == {"foo": datetime(2026, 5, 1, tzinfo=timezone.utc)}

    def test_per_package_duration(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab.uploaded-prior-to-package]\nfoo = "P4D"\n',
        )
        before = datetime.now(timezone.utc) - timedelta(days=4)
        overrides = read_pyproject_config(
            path, discover_workspace=False
        ).uploaded_prior_to_overrides
        after = datetime.now(timezone.utc) - timedelta(days=4)
        assert "foo" in overrides
        cutoff = overrides["foo"]
        assert cutoff is not None
        assert before <= cutoff <= after

    def test_per_package_duration_uses_explicit_anchor(self, tmp_path: Path) -> None:
        # Per-package ``P<n>D`` resolves against the same anchor as the
        # global value so all relative durations move in lockstep.
        path = write(
            tmp_path,
            '[tool.nab.uploaded-prior-to-package]\nfoo = "P4D"\n',
        )
        anchor = datetime(2024, 1, 5, tzinfo=timezone.utc)
        config = read_pyproject_config(path, discover_workspace=False, anchor=anchor)
        assert config.uploaded_prior_to_overrides == {
            "foo": datetime(2024, 1, 1, tzinfo=timezone.utc),
        }

    def test_canonicalises_keys(self, tmp_path: Path) -> None:
        # ``Pkg-Foo`` and ``pkg_foo`` canonicalise to ``pkg-foo``.
        path = write(
            tmp_path,
            '[tool.nab.uploaded-prior-to-package]\nPkg-Foo = "2026-05-01T00:00:00Z"\n',
        )
        overrides = read_pyproject_config(
            path, discover_workspace=False
        ).uploaded_prior_to_overrides
        assert "pkg-foo" in overrides
        assert "Pkg-Foo" not in overrides

    def test_duplicate_canonical_name_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[tool.nab.uploaded-prior-to-package]\n"
            'Pkg-Foo = "2026-05-01T00:00:00Z"\n'
            "pkg_foo = false\n",
        )
        with pytest.raises(ConfigError, match="duplicate canonical name"):
            read_pyproject_config(path, discover_workspace=False)

    def test_true_rejected(self, tmp_path: Path) -> None:
        # ``true`` makes no sense; the only meaningful boolean is
        # ``false`` ("no cooldown").
        path = write(
            tmp_path,
            "[tool.nab.uploaded-prior-to-package]\nfoo = true\n",
        )
        with pytest.raises(ConfigError, match="``true`` is not a valid override"):
            read_pyproject_config(path, discover_workspace=False)

    def test_must_be_table(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\nuploaded-prior-to-package = "x"\n')
        with pytest.raises(
            ConfigError,
            match=r"\[tool.nab.uploaded-prior-to-package\] must be a table",
        ):
            read_pyproject_config(path)

    def test_invalid_value_includes_package_in_error(self, tmp_path: Path) -> None:
        # A bad per-package value should mention which package failed
        # so the user does not have to grep through 100 entries.
        path = write(
            tmp_path,
            "[tool.nab.uploaded-prior-to-package]\n"
            'foo = "2026-05-01T00:00:00"\n',  # naive, no tz
        )
        with pytest.raises(
            ConfigError,
            match=r"\[tool.nab.uploaded-prior-to-package\]\.foo:",
        ):
            read_pyproject_config(path, discover_workspace=False)

    def test_mixed_overrides_round_trip(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[tool.nab]\n"
            'uploaded-prior-to = "2026-05-01T00:00:00Z"\n'
            "[tool.nab.uploaded-prior-to-package]\n"
            "apache-airflow-providers-amazon = false\n"
            'foo = "2025-01-01T00:00:00Z"\n',
        )
        config = read_pyproject_config(path, discover_workspace=False)
        assert config.uploaded_prior_to == datetime(2026, 5, 1, tzinfo=timezone.utc)
        assert config.uploaded_prior_to_overrides == {
            "apache-airflow-providers-amazon": None,
            "foo": datetime(2025, 1, 1, tzinfo=timezone.utc),
        }


class TestDistPolicyPackage:
    """``[tool.nab.dist-policy-package]`` parses into a mapping."""

    def test_absent_table_is_empty_mapping(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[tool.nab]\n")
        assert read_pyproject_config(path).dist_policy_overrides == {}

    def test_sdist_only_override(self, tmp_path: Path) -> None:
        # Airflow's --no-binary-package lxml shape.
        path = write(
            tmp_path,
            "[tool.nab.dist-policy-package]\n"
            'lxml = "sdist-only"\n'
            'xmlsec = "sdist-only"\n',
        )
        config = read_pyproject_config(path, discover_workspace=False)
        assert config.dist_policy_overrides == {
            "lxml": DistPolicy.SDIST_ONLY,
            "xmlsec": DistPolicy.SDIST_ONLY,
        }

    def test_each_policy_value_round_trips(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[tool.nab.dist-policy-package]\n"
            'a = "wheel-or-sdist"\n'
            'b = "no-sdist"\n'
            'c = "prefer-binary"\n'
            'd = "sdist-only"\n',
        )
        config = read_pyproject_config(path, discover_workspace=False)
        assert config.dist_policy_overrides == {
            "a": DistPolicy.WHEEL_OR_SDIST,
            "b": DistPolicy.NO_SDIST,
            "c": DistPolicy.PREFER_BINARY,
            "d": DistPolicy.SDIST_ONLY,
        }

    def test_canonicalises_keys(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab.dist-policy-package]\nPkg-Foo = "sdist-only"\n',
        )
        config = read_pyproject_config(path, discover_workspace=False)
        assert "pkg-foo" in config.dist_policy_overrides
        assert "Pkg-Foo" not in config.dist_policy_overrides

    def test_duplicate_canonical_name_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[tool.nab.dist-policy-package]\n"
            'Pkg-Foo = "sdist-only"\n'
            'pkg_foo = "wheel-or-sdist"\n',
        )
        with pytest.raises(ConfigError, match="duplicate canonical name"):
            read_pyproject_config(path, discover_workspace=False)

    def test_must_be_table(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\ndist-policy-package = "x"\n')
        with pytest.raises(
            ConfigError,
            match=r"\[tool.nab.dist-policy-package\] must be a table",
        ):
            read_pyproject_config(path)

    def test_value_must_be_string(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[tool.nab.dist-policy-package]\nlxml = 1\n",
        )
        with pytest.raises(
            ConfigError,
            match=r"\[tool.nab.dist-policy-package\]\.lxml must be a string",
        ):
            read_pyproject_config(path, discover_workspace=False)

    def test_invalid_policy_value(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab.dist-policy-package]\nlxml = "binary-only"\n',
        )
        with pytest.raises(
            ConfigError,
            match=r"\[tool.nab.dist-policy-package\]\.lxml must be one of",
        ):
            read_pyproject_config(path, discover_workspace=False)


class TestPolicies:
    def test_sdist_and_build_round_trip(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab]\ndist-policy = "no-sdist"\nbuild-policy = "build-local"\n',
        )
        config = read_pyproject_config(path)
        assert config.dist_policy is DistPolicy.NO_SDIST
        assert config.build_policy is BuildPolicy.BUILD_LOCAL

    def test_invalid_dist_policy(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\ndist-policy = "wrong"\n')
        with pytest.raises(ConfigError, match="dist-policy must be one of"):
            read_pyproject_config(path)

    def test_invalid_build_policy(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\nbuild-policy = "wrong"\n')
        with pytest.raises(ConfigError, match="build-policy must be one of"):
            read_pyproject_config(path)

    def test_policy_must_be_string(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[tool.nab]\ndist-policy = 0\n")
        with pytest.raises(ConfigError, match="dist-policy must be a string"):
            read_pyproject_config(path)


class TestResolution:
    def test_default_is_highest(self, tmp_path: Path) -> None:
        """Without a [tool.nab].resolution key, the default is HIGHEST."""
        path = write(tmp_path, "[tool.nab]\n")
        assert read_pyproject_config(path).resolution is ResolutionStrategy.HIGHEST

    def test_lowest(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\nresolution = "lowest"\n')
        assert read_pyproject_config(path).resolution is ResolutionStrategy.LOWEST

    def test_lowest_direct(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\nresolution = "lowest-direct"\n')
        assert (
            read_pyproject_config(path).resolution is ResolutionStrategy.LOWEST_DIRECT
        )

    def test_invalid_value_rejected(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\nresolution = "bogus"\n')
        with pytest.raises(ConfigError, match="resolution must be one of"):
            read_pyproject_config(path)

    def test_must_be_string(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[tool.nab]\nresolution = 1\n")
        with pytest.raises(ConfigError, match="resolution must be a string"):
            read_pyproject_config(path)


class TestMarkerEnvironment:
    def test_round_trip(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[tool.nab.marker-environment]\n"
            'platform_system = "Linux"\n'
            'sys_platform = "linux"\n',
        )
        env = read_pyproject_config(path).marker_environment
        assert env == {"platform_system": "Linux", "sys_platform": "linux"}

    def test_must_be_table(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\nmarker-environment = "no"\n')
        with pytest.raises(ConfigError, match="marker-environment must be a table"):
            read_pyproject_config(path)

    def test_entries_must_be_string_string(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[tool.nab.marker-environment]\nplatform_system = 1\n")
        with pytest.raises(
            ConfigError, match="marker-environment entries must be string"
        ):
            read_pyproject_config(path)


class TestIndexes:
    def test_round_trip(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[[tool.nab.indexes]]\n"
            'name = "pypi"\n'
            'url = "https://pypi.org/simple/"\n'
            "[[tool.nab.indexes]]\n"
            'name = "torch"\n'
            'url = "https://download.pytorch.org/whl/cpu"\n',
        )
        idxs = read_pyproject_config(path).indexes
        assert [i.name for i in idxs] == ["pypi", "torch"]
        assert idxs[1].url == "https://download.pytorch.org/whl/cpu"

    def test_default_when_omitted(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[tool.nab]\n")
        idxs = read_pyproject_config(path).indexes
        assert len(idxs) == 1
        assert idxs[0].name == DEFAULT_INDEX_NAME

    def test_must_be_array(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\nindexes = "x"\n')
        with pytest.raises(ConfigError, match="indexes must be an array"):
            read_pyproject_config(path)

    def test_empty_array_rejected(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[tool.nab]\nindexes = []\n")
        with pytest.raises(ConfigError, match="indexes must contain at least one"):
            read_pyproject_config(path)

    def test_entry_must_be_table(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\nindexes = ["nope"]\n')
        with pytest.raises(ConfigError, match="indexes\\[0\\] must be a table"):
            read_pyproject_config(path)

    def test_missing_keys(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[[tool.nab.indexes]]\nname = "pypi"\n')
        with pytest.raises(ConfigError, match="missing required key 'url'"):
            read_pyproject_config(path)

    def test_wrong_field_types(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[[tool.nab.indexes]]\nname = 1\nurl = 2\n")
        with pytest.raises(ConfigError, match="name and url must be strings"):
            read_pyproject_config(path)

    def test_duplicate_names_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[[tool.nab.indexes]]\n"
            'name = "x"\n'
            'url = "https://a/"\n'
            "[[tool.nab.indexes]]\n"
            'name = "x"\n'
            'url = "https://b/"\n',
        )
        with pytest.raises(ConfigError, match="duplicate index name"):
            read_pyproject_config(path)


class TestIndexOverrides:
    def test_round_trip(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[[tool.nab.index-overrides]]\n"
            'name = "torch"\n'
            'index = "torch-cpu"\n'
            "[[tool.nab.index-overrides]]\n"
            'name = "torch"\n'
            'index = "torch-rocm"\n'
            "marker = \"platform_machine == 'aarch64'\"\n",
        )
        ovr = read_pyproject_config(path).index_overrides
        assert ovr == (
            IndexOverride(name="torch", index="torch-cpu", marker=None),
            IndexOverride(
                name="torch",
                index="torch-rocm",
                marker="platform_machine == 'aarch64'",
            ),
        )

    def test_must_be_array(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\nindex-overrides = "x"\n')
        with pytest.raises(ConfigError, match="index-overrides must be an array"):
            read_pyproject_config(path)

    def test_entry_must_be_table(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\nindex-overrides = ["x"]\n')
        with pytest.raises(ConfigError, match="index-overrides\\[0\\] must be a table"):
            read_pyproject_config(path)

    def test_missing_keys(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[[tool.nab.index-overrides]]\nname = "torch"\n')
        with pytest.raises(ConfigError, match="missing required key 'index'"):
            read_pyproject_config(path)

    def test_name_and_index_must_be_strings(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[[tool.nab.index-overrides]]\nname = 1\nindex = 2\n")
        with pytest.raises(ConfigError, match="name and index must be strings"):
            read_pyproject_config(path)

    def test_marker_must_be_string(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[[tool.nab.index-overrides]]\nname = "torch"\nindex = "x"\nmarker = 1\n',
        )
        with pytest.raises(ConfigError, match="marker must be a string"):
            read_pyproject_config(path)


class TestVcs:
    def test_full_round_trip(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[tool.nab.vcs]\n"
            'policy = "allow"\n'
            'allowed-schemes = ["git+https"]\n'
            'allowed-repos = ["github.com/me/x"]\n'
            "require-pin = false\n",
        )
        vcs = read_pyproject_config(path).vcs
        assert vcs.policy is VcsPolicy.ALLOW
        assert vcs.allowed_schemes == frozenset({"git+https"})
        assert vcs.allowed_repos == ("github.com/me/x",)
        assert vcs.require_pin is False

    def test_default_block(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[tool.nab.vcs]\n")
        vcs = read_pyproject_config(path).vcs
        assert vcs.policy is VcsPolicy.BLOCK
        assert vcs.require_pin is True

    def test_must_be_table(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\nvcs = "x"\n')
        with pytest.raises(ConfigError, match="\\[tool.nab.vcs\\] must be a table"):
            read_pyproject_config(path)

    def test_unknown_key(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab.vcs]\nbogus = "1"\n')
        with pytest.raises(ConfigError, match="unknown \\[tool.nab.vcs\\] keys"):
            read_pyproject_config(path)

    def test_require_pin_must_be_bool(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab.vcs]\nrequire-pin = "yes"\n')
        with pytest.raises(ConfigError, match="vcs.require-pin must be a boolean"):
            read_pyproject_config(path)


class TestLocalSources:
    def test_relative_path_resolved_against_pyproject(self, tmp_path: Path) -> None:
        sibling = tmp_path.parent / "my-fork"
        path = write(
            tmp_path,
            '[[tool.nab.local-sources]]\nname = "my-fork"\npath = "../my-fork"\n',
        )
        srcs = read_pyproject_config(path).local_sources
        assert srcs == (LocalSource(name="my-fork", path=str(sibling.resolve())),)

    def test_absolute_path_unchanged(self, tmp_path: Path) -> None:
        abs_dir = tmp_path / "abs-fork"
        abs_dir.mkdir()
        path = write(
            tmp_path,
            f'[[tool.nab.local-sources]]\nname = "abs-fork"\npath = "{abs_dir!s}"\n',
        )
        srcs = read_pyproject_config(path).local_sources
        assert srcs == (LocalSource(name="abs-fork", path=str(abs_dir.resolve())),)

    def test_must_be_array(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\nlocal-sources = "x"\n')
        with pytest.raises(ConfigError, match="local-sources must be an array"):
            read_pyproject_config(path)

    def test_entry_must_be_table(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\nlocal-sources = ["x"]\n')
        with pytest.raises(ConfigError, match="local-sources\\[0\\] must be a table"):
            read_pyproject_config(path)

    def test_missing_keys(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[[tool.nab.local-sources]]\nname = "x"\n')
        with pytest.raises(ConfigError, match="missing required key 'path'"):
            read_pyproject_config(path)

    def test_field_types(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[[tool.nab.local-sources]]\nname = 1\npath = 2\n")
        with pytest.raises(ConfigError, match="name and path must be strings"):
            read_pyproject_config(path)

    def test_editable_defaults_false(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[[tool.nab.local-sources]]\nname = "x"\npath = "../x"\n',
        )
        srcs = read_pyproject_config(path).local_sources
        assert srcs[0].editable is False

    def test_editable_parsed(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[[tool.nab.local-sources]]\nname = "x"\npath = "../x"\neditable = true\n',
        )
        srcs = read_pyproject_config(path).local_sources
        assert srcs[0].editable is True

    def test_editable_must_be_bool(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[[tool.nab.local-sources]]\nname = "x"\npath = "../x"\neditable = "y"\n',
        )
        with pytest.raises(ConfigError, match="editable must be a boolean"):
            read_pyproject_config(path)

    def test_subdirectory_defaults_none(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[[tool.nab.local-sources]]\nname = "x"\npath = "../x"\n',
        )
        srcs = read_pyproject_config(path).local_sources
        assert srcs[0].subdirectory is None

    def test_subdirectory_parsed(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[[tool.nab.local-sources]]\n"
            'name = "x"\npath = "../x"\nsubdirectory = "pkg/lib"\n',
        )
        srcs = read_pyproject_config(path).local_sources
        assert srcs[0].subdirectory == "pkg/lib"

    def test_subdirectory_must_be_string(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[[tool.nab.local-sources]]\nname = "x"\npath = "../x"\nsubdirectory = 1\n',
        )
        with pytest.raises(ConfigError, match="subdirectory must be a string"):
            read_pyproject_config(path)

    def test_unknown_key_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[[tool.nab.local-sources]]\nname = "x"\npath = "../x"\nbogus = 1\n',
        )
        with pytest.raises(ConfigError, match="unknown local-sources"):
            read_pyproject_config(path)


class TestVcsSources:
    def test_round_trip(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[[tool.nab.vcs-sources]]\n"
            'name = "my-fork"\n'
            'url = "git+https://github.com/me/x.git@abc"\n',
        )
        srcs = read_pyproject_config(path).vcs_sources
        assert srcs == (
            VcsSource(name="my-fork", url="git+https://github.com/me/x.git@abc"),
        )

    def test_must_be_array(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\nvcs-sources = "x"\n')
        with pytest.raises(ConfigError, match="vcs-sources must be an array"):
            read_pyproject_config(path)

    def test_entry_must_be_table(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\nvcs-sources = ["x"]\n')
        with pytest.raises(ConfigError, match="vcs-sources\\[0\\] must be a table"):
            read_pyproject_config(path)

    def test_missing_keys(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[[tool.nab.vcs-sources]]\nname = "x"\n')
        with pytest.raises(ConfigError, match="missing required key 'url'"):
            read_pyproject_config(path)

    def test_field_types(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[[tool.nab.vcs-sources]]\nname = 1\nurl = 2\n")
        with pytest.raises(ConfigError, match="name and url must be strings"):
            read_pyproject_config(path)


class TestMatrix:
    def _matrix_body(self, **extra: str) -> str:
        body = (
            "[tool.nab]\n"
            'mode = "universal"\n'
            "[tool.nab.matrix]\n"
            'python = ">=3.11,<3.14"\n'
            'platforms = ["linux_x86_64", "macos_arm64"]\n'
        )
        for k, v in extra.items():
            body += f"{k} = {v}\n"
        return body

    def test_minimal(self, tmp_path: Path) -> None:
        path = write(tmp_path, self._matrix_body())
        config = read_pyproject_config(path)
        assert config.mode is ResolveMode.UNIVERSAL
        assert config.matrix == MatrixConfig(
            python=">=3.11,<3.14",
            platforms=("linux_x86_64", "macos_arm64"),
        )

    def test_python_order_and_patches(self, tmp_path: Path) -> None:
        body = self._matrix_body(
            **{
                "python-order": '"desc"',
                "python-patches": '{ "3.11" = "3.11.4" }',
            }
        )
        path = write(tmp_path, body)
        matrix = read_pyproject_config(path).matrix
        assert matrix is not None
        assert matrix.python_order == "desc"
        assert matrix.python_patches == {"3.11": "3.11.4"}

    def test_must_be_table(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab]\nmode = "universal"\nmatrix = "x"\n',
        )
        with pytest.raises(ConfigError, match="\\[tool.nab.matrix\\] must be a table"):
            read_pyproject_config(path)

    def test_unknown_key(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[tool.nab]\n"
            'mode = "universal"\n'
            "[tool.nab.matrix]\n"
            'python = ">=3.11"\n'
            'platforms = ["linux_x86_64"]\n'
            'bogus = "x"\n',
        )
        with pytest.raises(ConfigError, match="unknown \\[tool.nab.matrix\\] keys"):
            read_pyproject_config(path)

    def test_missing_required_keys(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab]\nmode = "universal"\n[tool.nab.matrix]\npython = ">=3.11"\n',
        )
        with pytest.raises(ConfigError, match="missing required key 'platforms'"):
            read_pyproject_config(path)

    def test_python_must_be_string(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[tool.nab]\n"
            'mode = "universal"\n'
            "[tool.nab.matrix]\n"
            "python = 311\n"
            'platforms = ["linux_x86_64"]\n',
        )
        with pytest.raises(ConfigError, match="matrix.python must be a string"):
            read_pyproject_config(path)

    def test_empty_platforms(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[tool.nab]\n"
            'mode = "universal"\n'
            "[tool.nab.matrix]\n"
            'python = ">=3.11"\n'
            "platforms = []\n",
        )
        with pytest.raises(
            ConfigError, match="matrix.platforms must list at least one"
        ):
            read_pyproject_config(path)

    def test_invalid_python_order(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[tool.nab]\n"
            'mode = "universal"\n'
            "[tool.nab.matrix]\n"
            'python = ">=3.11"\n'
            'platforms = ["linux_x86_64"]\n'
            'python-order = "sideways"\n',
        )
        with pytest.raises(ConfigError, match="python-order must be 'asc' or 'desc'"):
            read_pyproject_config(path)

    def test_python_patches_must_be_table(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[tool.nab]\n"
            'mode = "universal"\n'
            "[tool.nab.matrix]\n"
            'python = ">=3.11"\n'
            'platforms = ["linux_x86_64"]\n'
            'python-patches = "x"\n',
        )
        with pytest.raises(ConfigError, match="python-patches must be a table"):
            read_pyproject_config(path)

    def test_python_patches_entry_types(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[tool.nab]\n"
            'mode = "universal"\n'
            "[tool.nab.matrix]\n"
            'python = ">=3.11"\n'
            'platforms = ["linux_x86_64"]\n'
            "[tool.nab.matrix.python-patches]\n"
            '"3.11" = 1\n',
        )
        with pytest.raises(ConfigError, match="python-patches entries must be string"):
            read_pyproject_config(path)


class TestBuildPolicyPackage:
    """``[tool.nab.build-policy-package]`` parses into a name -> policy mapping."""

    def test_single_override(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[tool.nab]\n"
            'build-policy = "build-local"\n'
            "[tool.nab.build-policy-package]\n"
            'pyspark-client = "build-remote"\n',
        )
        config = read_pyproject_config(path, discover_workspace=False)
        assert config.build_policy is BuildPolicy.BUILD_LOCAL
        assert config.build_policy_overrides == {
            "pyspark-client": BuildPolicy.BUILD_REMOTE,
        }

    def test_multiple_overrides(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab.build-policy-package]\nfoo = "build-remote"\nbar = "never"\n',
        )
        config = read_pyproject_config(path, discover_workspace=False)
        assert config.build_policy_overrides == {
            "foo": BuildPolicy.BUILD_REMOTE,
            "bar": BuildPolicy.NEVER,
        }

    def test_invalid_policy_value_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab.build-policy-package]\nfoo = "wrong"\n',
        )
        with pytest.raises(ConfigError, match="must be one of"):
            read_pyproject_config(path, discover_workspace=False)

    def test_non_string_value_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[tool.nab.build-policy-package]\nfoo = 1\n",
        )
        with pytest.raises(ConfigError, match="must be a string"):
            read_pyproject_config(path, discover_workspace=False)

    def test_non_table_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab]\nbuild-policy-package = "not-a-table"\n',
        )
        with pytest.raises(ConfigError, match="must be a table"):
            read_pyproject_config(path, discover_workspace=False)

    def test_duplicate_canonical_rejected(self, tmp_path: Path) -> None:
        """Names that canonicalise to the same key are flagged."""
        path = write(
            tmp_path,
            "[tool.nab.build-policy-package]\n"
            'Foo-Bar = "build-local"\n'
            'foo_bar = "build-remote"\n',
        )
        with pytest.raises(ConfigError, match="duplicate canonical"):
            read_pyproject_config(path, discover_workspace=False)

    def test_default_is_empty(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[tool.nab]\n")
        config = read_pyproject_config(path, discover_workspace=False)
        assert config.build_policy_overrides == {}

    def test_canonicalises_keys(self, tmp_path: Path) -> None:
        """Keys are canonicalised on parse so lookups match the provider's keys."""
        path = write(
            tmp_path,
            '[tool.nab.build-policy-package]\nFoo-Bar = "build-remote"\n',
        )
        config = read_pyproject_config(path, discover_workspace=False)
        assert "foo-bar" in config.build_policy_overrides
        assert "Foo-Bar" not in config.build_policy_overrides


class TestWorkspace:
    """``[tool.nab.workspace]`` parses into a typed :class:`WorkspaceConfig`."""

    def test_absent_workspace_is_none(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[tool.nab]\n")
        assert read_pyproject_config(path).workspace is None

    def test_members_round_trip(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[tool.nab.workspace]\n"
            'members = ["airflow-core", "task-sdk", "providers/amazon"]\n',
        )
        config = read_pyproject_config(path, discover_workspace=False)
        assert config.workspace == WorkspaceConfig(
            members=("airflow-core", "task-sdk", "providers/amazon"),
        )

    def test_empty_members_round_trip(self, tmp_path: Path) -> None:
        # ``members = []`` is still a valid workspace declaration.
        path = write(tmp_path, "[tool.nab.workspace]\nmembers = []\n")
        config = read_pyproject_config(path, discover_workspace=False)
        assert config.workspace == WorkspaceConfig(members=())

    def test_must_be_table(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\nworkspace = "not-a-table"\n')
        with pytest.raises(
            ConfigError, match=r"\[tool.nab.workspace\] must be a table"
        ):
            read_pyproject_config(path, discover_workspace=False)

    def test_unknown_key_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[tool.nab.workspace]\nmembers = []\nbogus = 1\n",
        )
        with pytest.raises(ConfigError, match=r"unknown \[tool.nab.workspace\] keys"):
            read_pyproject_config(path, discover_workspace=False)

    def test_members_must_be_strings(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[tool.nab.workspace]\nmembers = [1]\n")
        with pytest.raises(ConfigError, match=r"workspace.members\[0\]"):
            read_pyproject_config(path, discover_workspace=False)


class TestWorkspaceDiscoveryIntegration:
    """``read_pyproject_config`` runs workspace discovery by default."""

    def _ws(self, root: Path) -> Path:
        ws_pyproject = root / "pyproject.toml"
        ws_pyproject.parent.mkdir(parents=True, exist_ok=True)
        ws_pyproject.write_text(
            '[project]\nname = "ws"\nversion = "0"\n'
            "[tool.nab.workspace]\n"
            'members = ["pkg"]\n',
        )
        member_dir = root / "pkg"
        member_dir.mkdir(parents=True, exist_ok=True)
        (member_dir / "pyproject.toml").write_text(
            '[project]\nname = "alpha"\nversion = "0"\n',
        )
        return member_dir / "pyproject.toml"

    def test_default_discovery_synthesises_local_sources(self, tmp_path: Path) -> None:
        member = self._ws(tmp_path)
        config = read_pyproject_config(member)
        assert config.local_sources == (
            LocalSource(name="alpha", path=str(member.parent)),
        )
        assert config.build_policy is BuildPolicy.BUILD_LOCAL

    def test_explicit_local_source_wins_over_workspace_member(
        self, tmp_path: Path
    ) -> None:
        member = self._ws(tmp_path)
        member.write_text(
            '[project]\nname = "alpha"\nversion = "0"\n'
            "[tool.nab]\n"
            "[[tool.nab.local-sources]]\n"
            'name = "alpha"\n'
            'path = "/explicit/alpha"\n',
        )
        config = read_pyproject_config(member)
        assert config.local_sources == (
            LocalSource(name="alpha", path="/explicit/alpha"),
        )

    def test_workspace_promotes_never_to_build_local_and_logs(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An explicit ``never`` is floored at ``build-local`` for workspaces.

        The log line is informational so users can audit the auto-promote.
        """
        member = self._ws(tmp_path)
        member.write_text(
            '[project]\nname = "alpha"\nversion = "0"\n'
            "[tool.nab]\n"
            'build-policy = "never"\n',
        )
        with caplog.at_level("INFO", logger="nab_python.config"):
            config = read_pyproject_config(member)
        assert config.build_policy is BuildPolicy.BUILD_LOCAL
        assert any(
            "promoted build-policy" in record.getMessage() for record in caplog.records
        )

    def test_user_build_remote_policy_not_downgraded(self, tmp_path: Path) -> None:
        member = self._ws(tmp_path)
        member.write_text(
            '[project]\nname = "alpha"\nversion = "0"\n'
            "[tool.nab]\n"
            'build-policy = "build-remote"\n',
        )
        config = read_pyproject_config(member)
        assert config.build_policy is BuildPolicy.BUILD_REMOTE

    def test_no_discovery_skips_walk(self, tmp_path: Path) -> None:
        member = self._ws(tmp_path)
        config = read_pyproject_config(member, discover_workspace=False)
        assert config.local_sources == ()
        assert config.build_policy is BuildPolicy.BUILD_LOCAL

    def test_no_workspace_ancestor_returns_unchanged_config(
        self, tmp_path: Path
    ) -> None:
        path = write(tmp_path, '[project]\nname = "x"\nversion = "0"\n')
        config = read_pyproject_config(path)
        assert config == NabProjectConfig()

    def test_empty_members_does_not_promote(self, tmp_path: Path) -> None:
        # A workspace root with members = [] is still a workspace, but
        # there are no LocalSources to add and no policy to promote.
        ws_pyproject = tmp_path / "pyproject.toml"
        ws_pyproject.write_text(
            '[project]\nname = "ws"\nversion = "0"\n'
            "[tool.nab.workspace]\nmembers = []\n",
        )
        member_dir = tmp_path / "pkg"
        member_dir.mkdir()
        member = member_dir / "pyproject.toml"
        member.write_text('[project]\nname = "alpha"\nversion = "0"\n')
        config = read_pyproject_config(member)
        assert config.local_sources == ()
        assert config.build_policy is BuildPolicy.BUILD_LOCAL
