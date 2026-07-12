"""Tests for the [tool.nab] config reader."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nab_python._vendor.packaging.version import Version
from nab_python.config import (
    ConfigError,
    ConflictKind,
    ConflictMember,
    ConflictPolicy,
    ConflictSelectionError,
    ConflictSet,
    MatrixConfig,
    NabProjectConfig,
    ResolveMode,
    conflict_exclusion_groups,
    conflict_forks,
    index_routes_from_config,
    read_pyproject_config,
    validate_conflict_minimums,
)
from nab_python.config_sources import (
    SourceConfigError,
    SourceRoots,
    build_cli_layer,
    discover_layers,
    read_env_layer,
    render_explain,
    render_get,
    resolve_config,
)
from nab_python.fetch import DEFAULT_INDEX_NAME, DEFAULT_INDEX_URL, IndexRoute
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


DOCS_CONFIGURATION = (
    Path(__file__).resolve().parents[2] / "docs" / "reference" / "configuration.md"
)


def first_tool_nab_example() -> str:
    """Return the first ``[tool.nab]`` fenced TOML block in the config reference."""
    text = DOCS_CONFIGURATION.read_text()
    for block in re.findall(r"```toml\n(.*?)```", text, re.DOTALL):
        if block.lstrip().startswith("[tool.nab]"):
            return block
    raise AssertionError("no [tool.nab] example block in configuration.md")


class TestCliOverridesFold:
    """``--project-*`` overrides fold into the resolved config."""

    def test_cli_override_beats_file_scalar(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\ndist-policy = "wheel-or-sdist"\n')
        config = read_pyproject_config(
            path, discover_workspace=False, cli_overrides={"dist-policy": "sdist-only"}
        )
        assert config.dist_policy is DistPolicy.SDIST_ONLY

    def test_cli_overrides_none_matches_no_overrides(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\ndist-policy = "sdist-only"\n')
        plain = read_pyproject_config(path, discover_workspace=False)
        explicit_none = read_pyproject_config(
            path, discover_workspace=False, cli_overrides=None
        )
        assert plain == explicit_none

    def test_cli_array_appends_after_files(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\nconstraints = ["a<1"]\n')
        config = read_pyproject_config(
            path, discover_workspace=False, cli_overrides={"constraints": ["b<2"]}
        )
        assert config.constraints == ("a<1", "b<2")

    def test_cli_mode_universal_requires_matrix(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[project]\nname = "x"\nversion = "0"\n')
        with pytest.raises(ConfigError, match=r"requires a \[tool.nab.matrix\]"):
            read_pyproject_config(
                path, discover_workspace=False, cli_overrides={"mode": "universal"}
            )


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

    def test_non_table_tool_returns_defaults(self, tmp_path: Path) -> None:
        path = write(tmp_path, 'tool = "not-a-table"\n')
        assert read_pyproject_config(path) == NabProjectConfig()


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

    def test_invalid_toml_rejected(self, tmp_path: Path) -> None:
        # A TOML syntax error (here a duplicated table) is reported as a
        # ConfigError, not a raw TOMLDecodeError, so the CLI renders it
        # under "Error in [tool.nab]" like every other config problem.
        path = write(
            tmp_path,
            '[tool.nab.packages.foo]\nindex = "a"\n'
            '[tool.nab.packages.foo]\ndist-policy = "wheel-only"\n',
        )
        with pytest.raises(ConfigError, match="not valid TOML"):
            read_pyproject_config(path)

    @pytest.mark.parametrize("user_key", ["offline = true", 'cache-dir = "x"'])
    def test_user_scope_key_in_pyproject_rejected(
        self, tmp_path: Path, user_key: str
    ) -> None:
        # A USER-scope registry option in pyproject [tool.nab] surfaces the
        # registry category error (single parse path), not the generic
        # unknown-key error.  This pins the reject_user_keys_in_pyproject
        # call ahead of the unknown-key check inside _parse_nab_table.
        path = write(tmp_path, f"[tool.nab]\n{user_key}\n")
        with pytest.raises(
            SourceConfigError,
            match="user-scope option and cannot be set in pyproject",
        ):
            read_pyproject_config(path)

    @pytest.mark.parametrize(
        "removed_key",
        [
            "dist-policy-package",
            "build-policy-package",
            "uploaded-prior-to-package",
            "index-overrides",
            "trust-unverified-sdist-deps",
        ],
    )
    def test_removed_legacy_key_rejected(
        self, tmp_path: Path, removed_key: str
    ) -> None:
        # The pre-1.0 clean break removed these keys with no alias; they now
        # fail loud as unknown [tool.nab] keys rather than being silently
        # ignored.
        path = write(tmp_path, f'[tool.nab]\n{removed_key} = "x"\n')
        with pytest.raises(ConfigError, match="unknown \\[tool.nab\\] keys"):
            read_pyproject_config(path)


class TestConflictRendering:
    """The ``__str__`` formats are codified in the conflicts guide."""

    def test_member_renders_kind_then_quoted_name(self) -> None:
        member = ConflictMember(ConflictKind.EXTRA, "cpu")
        assert str(member) == "extra 'cpu'"

    def test_group_member_renders_kind_then_quoted_name(self) -> None:
        member = ConflictMember(ConflictKind.GROUP, "black22")
        assert str(member) == "group 'black22'"

    def test_set_renders_policy_then_parenthesised_members(self) -> None:
        s = ConflictSet(
            members=(
                ConflictMember(ConflictKind.EXTRA, "cpu"),
                ConflictMember(ConflictKind.EXTRA, "gpu"),
            ),
            policy=ConflictPolicy.AT_MOST_ONE,
        )
        assert str(s) == "at-most-one (extra 'cpu', extra 'gpu')"


class TestConflicts:
    def test_default_is_empty(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[tool.nab]\n")
        assert read_pyproject_config(path).conflicts == ()

    def test_explicit_empty_list_is_empty(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[tool.nab]\nconflicts = []\n")
        assert read_pyproject_config(path).conflicts == ()

    def test_bare_set_defaults_to_at_most_one(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab]\nconflicts = [[{ extra = "cpu" }, { extra = "gpu" }]]\n',
        )
        conflicts = read_pyproject_config(path).conflicts
        assert conflicts == (
            ConflictSet(
                members=(
                    ConflictMember(ConflictKind.EXTRA, "cpu"),
                    ConflictMember(ConflictKind.EXTRA, "gpu"),
                ),
                policy=ConflictPolicy.AT_MOST_ONE,
            ),
        )

    def test_group_members(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[tool.nab]\nconflicts = ["
            '[{ group = "black22" }, { group = "black23" }, { group = "black24" }]]\n',
        )
        (conflict_set,) = read_pyproject_config(path).conflicts
        assert conflict_set.members == (
            ConflictMember(ConflictKind.GROUP, "black22"),
            ConflictMember(ConflictKind.GROUP, "black23"),
            ConflictMember(ConflictKind.GROUP, "black24"),
        )

    def test_mixed_extra_and_group_in_one_set(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab]\nconflicts = [[{ extra = "cpu" }, { group = "gpu" }]]\n',
        )
        (conflict_set,) = read_pyproject_config(path).conflicts
        assert conflict_set.members == (
            ConflictMember(ConflictKind.EXTRA, "cpu"),
            ConflictMember(ConflictKind.GROUP, "gpu"),
        )

    def test_names_are_canonicalised(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab]\nconflicts = [[{ extra = "My_Extra" }, { extra = "other" }]]\n',
        )
        (conflict_set,) = read_pyproject_config(path).conflicts
        assert conflict_set.members[0] == ConflictMember(ConflictKind.EXTRA, "my-extra")

    def test_policy_table_forms(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[tool.nab]\nconflicts = [\n"
            '  { members = [{ extra = "cpu" }, { extra = "gpu" }],'
            ' policy = "exactly-one" },\n'
            '  { members = [{ group = "a" }, { group = "b" }],'
            ' policy = "at-least-one" },\n'
            "]\n",
        )
        conflicts = read_pyproject_config(path).conflicts
        assert [c.policy for c in conflicts] == [
            ConflictPolicy.EXACTLY_ONE,
            ConflictPolicy.AT_LEAST_ONE,
        ]

    def test_table_without_policy_defaults_at_most_one(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[tool.nab]\nconflicts = ["
            '{ members = [{ extra = "cpu" }, { extra = "gpu" }] }]\n',
        )
        (conflict_set,) = read_pyproject_config(path).conflicts
        assert conflict_set.policy is ConflictPolicy.AT_MOST_ONE

    def test_table_missing_members_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab]\nconflicts = [{ policy = "exactly-one" }]\n',
        )
        with pytest.raises(ConfigError, match="must set 'members'"):
            read_pyproject_config(path)

    def test_snake_wrapping_key_form_rejected(self, tmp_path: Path) -> None:
        # The old ``{ exactly_one = [...] }`` wrapping-key form is gone.
        path = write(
            tmp_path,
            "[tool.nab]\nconflicts = ["
            '{ exactly_one = [{ extra = "cpu" }, { extra = "gpu" }] }]\n',
        )
        with pytest.raises(ConfigError, match="unknown conflict-set key"):
            read_pyproject_config(path)

    def test_invalid_policy_value_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[tool.nab]\nconflicts = ["
            '{ members = [{ extra = "a" }, { extra = "b" }], policy = "any-two" }]\n',
        )
        with pytest.raises(ConfigError, match="policy must be one of"):
            read_pyproject_config(path)

    def test_policy_must_be_string(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[tool.nab]\nconflicts = ["
            '{ members = [{ extra = "a" }, { extra = "b" }], policy = 1 }]\n',
        )
        with pytest.raises(ConfigError, match="policy must be a string"):
            read_pyproject_config(path)

    def test_multiple_independent_sets(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[tool.nab]\nconflicts = [\n"
            '  [{ extra = "a" }, { extra = "b" }],\n'
            '  [{ group = "x" }, { group = "y" }],\n'
            "]\n",
        )
        assert len(read_pyproject_config(path).conflicts) == 2

    def test_must_be_array(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\nconflicts = "cpu"\n')
        with pytest.raises(ConfigError, match="conflicts must be an array"):
            read_pyproject_config(path)

    def test_set_must_be_array_or_table(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[tool.nab]\nconflicts = [1]\n")
        with pytest.raises(ConfigError, match=r"conflicts\[0\] must be an array"):
            read_pyproject_config(path)

    def test_fewer_than_two_members_rejected(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\nconflicts = [[{ extra = "cpu" }]]\n')
        with pytest.raises(ConfigError, match="at least 2 members"):
            read_pyproject_config(path)

    def test_duplicate_member_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab]\nconflicts = [[{ extra = "cpu" }, { extra = "cpu" }]]\n',
        )
        with pytest.raises(ConfigError, match="more than once"):
            read_pyproject_config(path)

    def test_duplicate_member_after_canonicalisation_rejected(
        self, tmp_path: Path
    ) -> None:
        """``{extra="CPU"}`` and ``{extra="cpu"}`` canonicalise to one name,
        so the dedup check relies on :class:`ConflictMember` comparing under
        canonical form rather than raw text."""
        path = write(
            tmp_path,
            '[tool.nab]\nconflicts = [[{ extra = "CPU" }, { extra = "cpu" }]]\n',
        )
        with pytest.raises(ConfigError, match="more than once"):
            read_pyproject_config(path)

    def test_unknown_set_key_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab]\nconflicts = [{ any_two = [{ extra = "a" }, { extra = "b" }] }]\n',
        )
        with pytest.raises(ConfigError, match="unknown conflict-set key"):
            read_pyproject_config(path)

    def test_member_must_be_table(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\nconflicts = [["cpu", "gpu"]]\n')
        with pytest.raises(ConfigError, match="must be a table"):
            read_pyproject_config(path)

    def test_member_unknown_key_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab]\nconflicts = [[{ feature = "cpu" }, { extra = "gpu" }]]\n',
        )
        with pytest.raises(ConfigError, match="unknown member key"):
            read_pyproject_config(path)

    def test_member_must_name_exactly_one_kind(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab]\nconflicts = [[{ extra = "cpu", group = "g" }, '
            '{ extra = "gpu" }]]\n',
        )
        with pytest.raises(ConfigError, match="exactly one of"):
            read_pyproject_config(path)

    def test_member_name_must_be_nonempty_string(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab]\nconflicts = [[{ extra = "" }, { extra = "gpu" }]]\n',
        )
        with pytest.raises(ConfigError, match="non-empty string"):
            read_pyproject_config(path)

    def test_member_name_must_be_string(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[tool.nab]\nconflicts = [[{ extra = 1 }, { extra = 2 }]]\n",
        )
        with pytest.raises(ConfigError, match="non-empty string"):
            read_pyproject_config(path)

    def test_members_must_be_array(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\nconflicts = [{ members = "cpu" }]\n')
        with pytest.raises(ConfigError, match="must be an array of members"):
            read_pyproject_config(path)

    def test_member_in_two_sets_rejected(self, tmp_path: Path) -> None:
        # An overlapping member has no well-defined fork; the cartesian
        # product would otherwise produce a degenerate (cpu, cpu) combo.
        path = write(
            tmp_path,
            "[tool.nab]\nconflicts = [\n"
            '  [{ extra = "cpu" }, { extra = "gpu" }],\n'
            '  [{ extra = "cpu" }, { extra = "tpu" }],\n'
            "]\n",
        )
        with pytest.raises(ConfigError, match="more than one set"):
            read_pyproject_config(path)

    def test_same_name_extra_and_group_in_two_sets_allowed(
        self, tmp_path: Path
    ) -> None:
        # An extra and a group sharing a name are distinct members.
        path = write(
            tmp_path,
            "[tool.nab]\nconflicts = [\n"
            '  [{ extra = "cpu" }, { extra = "gpu" }],\n'
            '  [{ group = "cpu" }, { group = "tpu" }],\n'
            "]\n",
        )
        assert len(read_pyproject_config(path).conflicts) == 2

    def test_malformed_member_name_rejected(self, tmp_path: Path) -> None:
        # ``...`` canonicalises to ``-``, which is not a valid name.
        path = write(
            tmp_path,
            '[tool.nab]\nconflicts = [[{ extra = "..." }, { extra = "gpu" }]]\n',
        )
        with pytest.raises(ConfigError, match="not a valid extra/group name"):
            read_pyproject_config(path)

    def test_default_groups_conflict_with_at_most_one_rejected(
        self, tmp_path: Path
    ) -> None:
        path = write(
            tmp_path,
            "[tool.nab]\n"
            'default-groups = ["black22", "black23"]\n'
            'conflicts = [[{ group = "black22" }, { group = "black23" }]]\n',
        )
        with pytest.raises(ConfigError, match="declared mutually exclusive"):
            read_pyproject_config(path, discover_workspace=False)

    def test_default_groups_conflict_with_exactly_one_rejected(
        self, tmp_path: Path
    ) -> None:
        path = write(
            tmp_path,
            "[tool.nab]\n"
            'default-groups = ["black22", "black23"]\n'
            "conflicts = [{ members = "
            '[{ group = "black22" }, { group = "black23" }], policy = "exactly-one" }]\n',
        )
        with pytest.raises(ConfigError, match="declared mutually exclusive"):
            read_pyproject_config(path, discover_workspace=False)

    def test_default_groups_one_member_allowed(self, tmp_path: Path) -> None:
        # A single default group in an exclusive set is fine.
        path = write(
            tmp_path,
            "[tool.nab]\n"
            'default-groups = ["black22"]\n'
            'conflicts = [[{ group = "black22" }, { group = "black23" }]]\n',
        )
        config = read_pyproject_config(path, discover_workspace=False)
        assert config.default_groups == ("black22",)

    def test_default_groups_skip_at_least_one(self, tmp_path: Path) -> None:
        # at-least-one does not forbid co-selection, so two default
        # groups in such a set are allowed.
        path = write(
            tmp_path,
            "[tool.nab]\n"
            'default-groups = ["a", "b"]\n'
            'conflicts = [{ members = [{ group = "a" }, { group = "b" }],'
            ' policy = "at-least-one" }]\n',
        )
        config = read_pyproject_config(path, discover_workspace=False)
        assert config.default_groups == ("a", "b")


def _extras_set(policy: ConflictPolicy, *names: str) -> ConflictSet:
    """Build an extras-only ConflictSet for tests."""
    return ConflictSet(
        members=tuple(ConflictMember(ConflictKind.EXTRA, n) for n in names),
        policy=policy,
    )


class TestConflictExclusionGroups:
    def test_drops_at_least_one_keeps_others(self) -> None:
        groups = conflict_exclusion_groups(
            (
                _extras_set(ConflictPolicy.AT_MOST_ONE, "a", "b"),
                _extras_set(ConflictPolicy.AT_LEAST_ONE, "c", "d"),
                _extras_set(ConflictPolicy.EXACTLY_ONE, "e", "f"),
            )
        )
        assert groups == (
            frozenset({("extra", "a"), ("extra", "b")}),
            frozenset({("extra", "e"), ("extra", "f")}),
        )
        flat = {member for group in groups for member in group}
        assert ("extra", "c") not in flat
        assert ("extra", "d") not in flat


class TestValidateConflictMinimums:
    _set = staticmethod(_extras_set)

    def test_exactly_one_empty_raises(self) -> None:
        with pytest.raises(ConflictSelectionError, match="exactly one"):
            validate_conflict_minimums(
                (self._set(ConflictPolicy.EXACTLY_ONE, "cpu", "gpu"),), (), ()
            )

    def test_at_least_one_empty_raises(self) -> None:
        with pytest.raises(ConflictSelectionError, match="at least one"):
            validate_conflict_minimums(
                (self._set(ConflictPolicy.AT_LEAST_ONE, "cpu", "gpu"),), (), ()
            )

    def test_at_most_one_empty_does_not_raise(self) -> None:
        validate_conflict_minimums(
            (self._set(ConflictPolicy.AT_MOST_ONE, "cpu", "gpu"),), (), ()
        )

    def test_does_not_reject_co_selection(self) -> None:
        # The minimum check never forbids two active members.
        validate_conflict_minimums(
            (self._set(ConflictPolicy.EXACTLY_ONE, "cpu", "gpu"),),
            ("cpu", "gpu"),
            (),
        )

    def test_selection_canonicalised(self) -> None:
        # An active member spelled differently still satisfies the minimum.
        validate_conflict_minimums(
            (self._set(ConflictPolicy.EXACTLY_ONE, "fast-io", "gpu"),),
            ("Fast_IO",),
            (),
        )

    def test_at_least_one_message_lists_members_not_policy(self) -> None:
        # The message renders the members and cites the policy and key
        # once; no duplicate ``at_least_one`` prefix from ConflictSet.__str__.
        with pytest.raises(ConflictSelectionError) as info:
            validate_conflict_minimums(
                (self._set(ConflictPolicy.AT_LEAST_ONE, "cpu", "gpu"),), (), ()
            )
        message = str(info.value)
        assert "at least one of extra 'cpu', extra 'gpu' must be selected" in message
        assert "declared at-least-one in [tool.nab].conflicts" in message
        # No double policy word.
        assert message.count("at-least-one") == 1

    def test_exactly_one_message_lists_members_not_policy(self) -> None:
        with pytest.raises(ConflictSelectionError) as info:
            validate_conflict_minimums(
                (self._set(ConflictPolicy.EXACTLY_ONE, "cpu", "gpu"),), (), ()
            )
        message = str(info.value)
        assert "exactly one of extra 'cpu', extra 'gpu' must be selected" in message
        assert "declared exactly-one in [tool.nab].conflicts" in message


class TestConflictForks:
    def test_exactly_one_co_selection_forks_per_member(self) -> None:
        cs = ConflictSet(
            members=(
                ConflictMember(ConflictKind.EXTRA, "cpu"),
                ConflictMember(ConflictKind.EXTRA, "gpu"),
            ),
            policy=ConflictPolicy.EXACTLY_ONE,
        )
        forks = conflict_forks(("cpu", "gpu"), (), (cs,))
        assert [f.selection for f in forks] == [
            (("extra", "cpu"),),
            (("extra", "gpu"),),
        ]
        assert [f.active_extras for f in forks] == [("cpu",), ("gpu",)]


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

    def test_constraints_entries_must_be_valid_pep508(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab]\nconstraints = ["urllib3 (((not valid pep508"]\n',
        )
        with pytest.raises(
            ConfigError, match="constraints\\[0\\] is not a valid requirement"
        ):
            read_pyproject_config(path)


class TestDefaultGroups:
    def test_default_is_empty(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[tool.nab]\n")
        assert read_pyproject_config(path).default_groups == ()

    def test_round_trip(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\ndefault-groups = ["dev", "test"]\n')
        assert read_pyproject_config(path).default_groups == ("dev", "test")

    def test_must_be_list(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\ndefault-groups = "dev"\n')
        with pytest.raises(ConfigError, match="default-groups must be a list"):
            read_pyproject_config(path)

    def test_entries_must_be_strings(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[tool.nab]\ndefault-groups = [1]\n")
        with pytest.raises(ConfigError, match="default-groups\\[0\\] must be a string"):
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

    def test_top_level_doc_example_is_a_valid_specifier(self, tmp_path: Path) -> None:
        """The config guide's top-level example must parse as a valid specifier."""
        block = first_tool_nab_example()
        match = re.search(r'^requires-python = "([^"]*)"', block, re.MULTILINE)
        assert match is not None, "top-level example dropped requires-python"
        value = match.group(1)

        path = write(tmp_path, f'[tool.nab]\nrequires-python = "{value}"\n')
        assert read_pyproject_config(path).requires_python == value


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

    def test_duration_overflow_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path, '[tool.nab]\nuploaded-prior-to = "P99999999999999999999D"\n'
        )
        with pytest.raises(ConfigError, match="duration is too large"):
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


class TestPolicies:
    def test_sdist_and_build_round_trip(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab]\ndist-policy = "wheel-only"\nbuild-policy = "build-local"\n',
        )
        config = read_pyproject_config(path)
        assert config.dist_policy is DistPolicy.WHEEL_ONLY
        assert config.build_policy is BuildPolicy.BUILD_LOCAL
        assert config.trust_unverified_sdist_deps is False

    def test_each_dist_value_round_trips(self, tmp_path: Path) -> None:
        for value, expected in (
            ("wheel-only", DistPolicy.WHEEL_ONLY),
            ("prefer-wheel", DistPolicy.PREFER_WHEEL),
            ("wheel-or-sdist", DistPolicy.WHEEL_OR_SDIST),
            ("sdist-only", DistPolicy.SDIST_ONLY),
            ("sdist-install", DistPolicy.SDIST_INSTALL),
        ):
            path = write(tmp_path, f'[tool.nab]\ndist-policy = "{value}"\n')
            assert read_pyproject_config(path).dist_policy is expected

    def test_dist_policy_table_with_trust(self, tmp_path: Path) -> None:
        # The global dist-policy table folds in the sdist-trust flag.
        path = write(
            tmp_path,
            "[tool.nab]\n"
            'dist-policy = { policy = "sdist-only", trust-unverified-deps = true }\n',
        )
        config = read_pyproject_config(path)
        assert config.dist_policy is DistPolicy.SDIST_ONLY
        assert config.trust_unverified_sdist_deps is True

    def test_dist_policy_table_without_trust_defaults_false(
        self, tmp_path: Path
    ) -> None:
        path = write(
            tmp_path,
            '[tool.nab]\ndist-policy = { policy = "sdist-only" }\n',
        )
        config = read_pyproject_config(path)
        assert config.dist_policy is DistPolicy.SDIST_ONLY
        assert config.trust_unverified_sdist_deps is False

    def test_dist_policy_table_unknown_key_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab]\ndist-policy = { policy = "sdist-only", bogus = true }\n',
        )
        with pytest.raises(ConfigError, match="unknown key"):
            read_pyproject_config(path)

    def test_dist_policy_table_missing_policy_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[tool.nab]\ndist-policy = { trust-unverified-deps = true }\n",
        )
        with pytest.raises(ConfigError, match="table must set 'policy'"):
            read_pyproject_config(path)

    def test_dist_policy_table_trust_must_be_bool(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab]\ndist-policy = { policy = "sdist-only",'
            ' trust-unverified-deps = "x" }\n',
        )
        with pytest.raises(ConfigError, match="must be a boolean"):
            read_pyproject_config(path)

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


_UNIVERSAL_MATRIX = (
    '[tool.nab.matrix]\npython = ">=3.11,<3.12"\nplatforms = ["linux_x86_64"]\n'
)


class TestUniversalBuildPolicy:
    """Universal mode cannot build on the host: build-policy is forced never."""

    def test_defaults_to_never(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab]\nmode = "universal"\n' + _UNIVERSAL_MATRIX,
        )
        assert read_pyproject_config(path).build_policy is BuildPolicy.NEVER

    def test_explicit_never_accepted(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab]\nmode = "universal"\nbuild-policy = "never"\n'
            + _UNIVERSAL_MATRIX,
        )
        assert read_pyproject_config(path).build_policy is BuildPolicy.NEVER

    def test_explicit_build_local_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab]\nmode = "universal"\nbuild-policy = "build-local"\n'
            + _UNIVERSAL_MATRIX,
        )
        with pytest.raises(ConfigError, match="universal.*build-policy.*never"):
            read_pyproject_config(path)

    def test_explicit_build_remote_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab]\nmode = "universal"\nbuild-policy = "build-remote"\n'
            + _UNIVERSAL_MATRIX,
        )
        with pytest.raises(ConfigError, match="universal.*build-policy.*never"):
            read_pyproject_config(path)

    def test_package_override_build_policy_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab]\nmode = "universal"\n'
            + _UNIVERSAL_MATRIX
            + '[tool.nab.packages.foo]\nbuild-policy = "build-remote"\n',
        )
        with pytest.raises(ConfigError, match="packages.foo"):
            read_pyproject_config(path)

    def test_package_override_without_build_policy_ok(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab]\nmode = "universal"\n'
            + _UNIVERSAL_MATRIX
            + '[tool.nab.packages.foo]\ndist-policy = "wheel-only"\n',
        )
        assert read_pyproject_config(path).build_policy is BuildPolicy.NEVER

    def test_index_override_build_policy_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab]\nmode = "universal"\n'
            + _UNIVERSAL_MATRIX
            + '[tool.nab.index.pypi]\nbuild-policy = "build-remote"\n',
        )
        with pytest.raises(ConfigError, match="index.pypi"):
            read_pyproject_config(path)

    def test_index_override_without_build_policy_ok(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab]\nmode = "universal"\n'
            + _UNIVERSAL_MATRIX
            + '[tool.nab.index.pypi]\ndist-policy = "wheel-only"\n',
        )
        assert read_pyproject_config(path).build_policy is BuildPolicy.NEVER

    def test_specific_mode_build_local_unaffected(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\nbuild-policy = "build-local"\n')
        assert read_pyproject_config(path).build_policy is BuildPolicy.BUILD_LOCAL


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

    def test_unknown_variable_rejected(self, tmp_path: Path) -> None:
        # A misspelled PEP 508 variable (kebab, not snake) fails loud.
        path = write(
            tmp_path, '[tool.nab.marker-environment]\npython-version = "3.12"\n'
        )
        with pytest.raises(ConfigError, match="unknown marker-environment variable"):
            read_pyproject_config(path)

    def test_version_value_round_trip(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[tool.nab.marker-environment]\n"
            'python_version = "3.12"\n'
            'python_full_version = "3.12.4"\n',
        )
        env = read_pyproject_config(path).marker_environment
        assert env == {"python_version": "3.12", "python_full_version": "3.12.4"}

    def test_python_version_must_be_pep440(self, tmp_path: Path) -> None:
        path = write(
            tmp_path, '[tool.nab.marker-environment]\npython_version = "3.x"\n'
        )
        with pytest.raises(
            ConfigError,
            match=r"marker-environment.python_version must be a PEP 440 version,"
            r" got '3.x'",
        ):
            read_pyproject_config(path)

    def test_python_full_version_must_be_pep440(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab.marker-environment]\npython_full_version = "not-a-version"\n',
        )
        with pytest.raises(
            ConfigError,
            match=r"marker-environment.python_full_version must be a PEP 440"
            r" version, got 'not-a-version'",
        ):
            read_pyproject_config(path)

    def test_rejected_in_universal_mode(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab]\nmode = "universal"\n'
            "[tool.nab.matrix]\n"
            'python = ">=3.11"\n'
            'platforms = ["linux_x86_64"]\n'
            "[tool.nab.marker-environment]\n"
            'platform_system = "Windows"\n',
        )
        with pytest.raises(
            ConfigError, match="does not support .tool.nab.marker-environment."
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

    def test_unknown_key_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[[tool.nab.indexes]]\nname = "x"\nurl = "https://a/"\nbogus = 1\n',
        )
        with pytest.raises(ConfigError, match="unknown indexes"):
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

    def test_unknown_scheme_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab.vcs]\npolicy = "allow"\nallowed-schemes = ["git+htps"]\n',
        )
        with pytest.raises(
            ConfigError,
            match=r"unknown vcs.allowed-schemes: \['git\+htps'\]; nab recognises",
        ):
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

    def test_relative_path_base_is_symlink_dir(self, tmp_path: Path) -> None:
        # A relative local-sources path resolves against the symlink's own
        # directory, not the symlink target's directory.
        target_dir = tmp_path / "real"
        target_dir.mkdir()
        link_dir = tmp_path / "link_dir"
        link_dir.mkdir()
        real_pyproject = target_dir / "pyproject.toml"
        real_pyproject.write_text(
            '[[tool.nab.local-sources]]\nname = "foo"\npath = "./libs/foo"\n'
        )
        link_pyproject = link_dir / "pyproject.toml"
        link_pyproject.symlink_to(real_pyproject)
        srcs = read_pyproject_config(
            link_pyproject, discover_workspace=False
        ).local_sources
        assert srcs == (
            LocalSource(name="foo", path=str((link_dir / "libs" / "foo").resolve())),
        )

    def test_absolute_path_unchanged(self, tmp_path: Path) -> None:
        abs_dir = tmp_path / "abs-fork"
        abs_dir.mkdir()
        # POSIX form avoids ``\U`` escapes in the TOML on Windows.
        path = write(
            tmp_path,
            f'[[tool.nab.local-sources]]\nname = "abs-fork"\n'
            f'path = "{abs_dir.as_posix()}"\n',
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

    def test_subdirectory_parent_escape_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[[tool.nab.local-sources]]\n"
            'name = "x"\npath = "../x"\nsubdirectory = "../../../../etc"\n',
        )
        with pytest.raises(ConfigError, match="escapes the source tree"):
            read_pyproject_config(path)

    def test_subdirectory_absolute_escape_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[[tool.nab.local-sources]]\n"
            'name = "x"\npath = "../x"\nsubdirectory = "/etc/secrets"\n',
        )
        with pytest.raises(ConfigError, match="escapes the source tree"):
            read_pyproject_config(path)

    def test_unknown_key_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[[tool.nab.local-sources]]\nname = "x"\npath = "../x"\nbogus = 1\n',
        )
        with pytest.raises(ConfigError, match="unknown local-sources"):
            read_pyproject_config(path)

    def test_duplicate_canonical_name_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[[tool.nab.local-sources]]\nname = "Foo-Bar"\npath = "../a"\n'
            '[[tool.nab.local-sources]]\nname = "foo_bar"\npath = "../b"\n',
        )
        with pytest.raises(ConfigError, match="duplicate canonical name"):
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

    def test_unknown_key_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[[tool.nab.vcs-sources]]\nname = "x"\nurl = "git+https://h/x@a"\nbogus = 1\n',
        )
        with pytest.raises(ConfigError, match="unknown vcs-sources"):
            read_pyproject_config(path)

    def test_duplicate_canonical_name_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[[tool.nab.vcs-sources]]\nname = "Foo-Bar"\n'
            'url = "git+https://github.com/me/a.git@abc"\n'
            '[[tool.nab.vcs-sources]]\nname = "foo_bar"\n'
            'url = "git+https://github.com/me/b.git@def"\n',
        )
        with pytest.raises(ConfigError, match="duplicate canonical name"):
            read_pyproject_config(path)

    def test_canonical_name_collides_with_local_source(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[[tool.nab.local-sources]]\nname = "Foo-Bar"\npath = "../a"\n'
            '[[tool.nab.vcs-sources]]\nname = "foo_bar"\n'
            'url = "git+https://github.com/me/b.git@abc"\n',
        )
        with pytest.raises(ConfigError, match="duplicate canonical name"):
            read_pyproject_config(path)


class TestArchiveSources:
    _URL = "https://ex.com/foo-1.0.tar.gz#sha256=" + "e" * 64

    def test_round_trip(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            f'[[tool.nab.archive-sources]]\nname = "foo"\nurl = "{self._URL}"\n',
        )
        (source,) = read_pyproject_config(path).archive_sources
        assert source.name == "foo"
        assert source.url == self._URL

    def test_must_be_array(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\narchive-sources = "x"\n')
        with pytest.raises(ConfigError, match="archive-sources must be an array"):
            read_pyproject_config(path)

    def test_entry_must_be_table(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\narchive-sources = ["x"]\n')
        with pytest.raises(ConfigError, match=r"archive-sources\[0\] must be a table"):
            read_pyproject_config(path)

    def test_missing_keys(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[[tool.nab.archive-sources]]\nname = "x"\n')
        with pytest.raises(ConfigError, match="missing required key 'url'"):
            read_pyproject_config(path)

    def test_field_types(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[[tool.nab.archive-sources]]\nname = 1\nurl = 2\n")
        with pytest.raises(ConfigError, match="name and url must be strings"):
            read_pyproject_config(path)

    def test_unknown_key_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            f'[[tool.nab.archive-sources]]\nname = "x"\nurl = "{self._URL}"\nbogus = 1\n',
        )
        with pytest.raises(ConfigError, match="unknown archive-sources"):
            read_pyproject_config(path)

    def test_no_hash_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[[tool.nab.archive-sources]]\nname = "x"\n'
            'url = "https://ex.com/foo-1.0.tar.gz"\n',
        )
        with pytest.raises(ConfigError, match="has no hash"):
            read_pyproject_config(path)

    def test_empty_digest_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[[tool.nab.archive-sources]]\nname = "x"\n'
            'url = "https://ex.com/foo-1.0.tar.gz#sha256="\n',
        )
        with pytest.raises(ConfigError, match="has no hash"):
            read_pyproject_config(path)

    def test_non_targz_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[[tool.nab.archive-sources]]\nname = "x"\n'
            'url = "https://ex.com/foo-1.0.whl#sha256=' + "e" * 64 + '"\n',
        )
        with pytest.raises(ConfigError, match="not a .tar.gz archive"):
            read_pyproject_config(path)

    def test_malformed_fragment_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[[tool.nab.archive-sources]]\nname = "x"\n'
            'url = "https://ex.com/foo-1.0.tar.gz#egg=foo"\n',
        )
        with pytest.raises(ConfigError, match="unknown archive URL fragment"):
            read_pyproject_config(path)

    def test_duplicate_collides_with_vcs_source(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[[tool.nab.vcs-sources]]\nname = "Foo-Bar"\n'
            'url = "git+https://github.com/me/b.git@abc"\n'
            f'[[tool.nab.archive-sources]]\nname = "foo_bar"\nurl = "{self._URL}"\n',
        )
        with pytest.raises(ConfigError, match="duplicate canonical name"):
            read_pyproject_config(path)


class TestPackageSugar:
    """``[tool.nab.packages.<name>]`` parses into per-package overrides."""

    def test_absent_is_empty(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[tool.nab]\n")
        config = read_pyproject_config(path)
        assert config.package_overrides == ()
        assert config.index_overrides == {}

    def test_dist_policy_string(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab.packages.lxml]\ndist-policy = "sdist-only"\n')
        (override,) = read_pyproject_config(
            path, discover_workspace=False
        ).package_overrides
        assert override.name == "lxml"
        assert override.dist_policy is DistPolicy.SDIST_ONLY

    def test_name_canonicalised(self, tmp_path: Path) -> None:
        path = write(
            tmp_path, '[tool.nab.packages.Pkg_Foo]\ndist-policy = "sdist-only"\n'
        )
        (override,) = read_pyproject_config(
            path, discover_workspace=False
        ).package_overrides
        assert override.name == "pkg-foo"

    def test_version_specifier_in_quoted_key_scopes_the_entry(
        self, tmp_path: Path
    ) -> None:
        path = write(
            tmp_path, '[tool.nab.packages."lxml <= 2"]\ndist-policy = "sdist-only"\n'
        )
        (override,) = read_pyproject_config(
            path, discover_workspace=False
        ).package_overrides
        assert override.name == "lxml"
        assert str(override.requirement.specifier) == "<=2"
        assert Version("1.0") in override.version_range
        assert Version("3.0") not in override.version_range

    def test_dist_policy_table_with_trust(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[tool.nab.packages.lxml]\n"
            'dist-policy = { policy = "sdist-only", trust-unverified-deps = true }\n',
        )
        (override,) = read_pyproject_config(
            path, discover_workspace=False
        ).package_overrides
        assert override.dist_policy is DistPolicy.SDIST_ONLY
        assert override.dist_trust_unverified_deps is True

    def test_dist_policy_table_without_trust(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab.packages.lxml]\ndist-policy = { policy = "sdist-only" }\n',
        )
        (override,) = read_pyproject_config(
            path, discover_workspace=False
        ).package_overrides
        assert override.dist_policy is DistPolicy.SDIST_ONLY
        assert override.dist_trust_unverified_deps is None

    def test_build_policy_and_uploaded_prior_to(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[tool.nab.packages.foo]\n"
            'build-policy = "build-remote"\n'
            'uploaded-prior-to = "2026-05-01T00:00:00Z"\n',
        )
        (override,) = read_pyproject_config(
            path, discover_workspace=False
        ).package_overrides
        assert override.build_policy is BuildPolicy.BUILD_REMOTE
        assert override.uploaded_prior_to == datetime(2026, 5, 1, tzinfo=timezone.utc)

    def test_uploaded_prior_to_false_disables(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[tool.nab]\n"
            'uploaded-prior-to = "2026-05-01T00:00:00Z"\n'
            "[tool.nab.packages.foo]\n"
            "uploaded-prior-to = false\n",
        )
        config = read_pyproject_config(path, discover_workspace=False)
        assert config.uploaded_prior_to == datetime(2026, 5, 1, tzinfo=timezone.utc)
        (override,) = config.package_overrides
        assert override.uploaded_prior_to is None
        assert override.uploaded_prior_to_disabled is True

    def test_uploaded_prior_to_true_rejected(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[tool.nab.packages.foo]\nuploaded-prior-to = true\n")
        with pytest.raises(ConfigError, match="``true`` is not a valid value"):
            read_pyproject_config(path, discover_workspace=False)

    def test_uploaded_prior_to_invalid_value(self, tmp_path: Path) -> None:
        path = write(
            tmp_path, '[tool.nab.packages.foo]\nuploaded-prior-to = "not-a-date"\n'
        )
        with pytest.raises(ConfigError, match=r"\.uploaded-prior-to:"):
            read_pyproject_config(path, discover_workspace=False)

    def test_routing_index(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[[tool.nab.indexes]]\n"
            'name = "pypi"\n'
            'url = "https://pypi.org/simple/"\n'
            "[[tool.nab.indexes]]\n"
            'name = "internal"\n'
            'url = "https://pkgs.example.com/simple/"\n'
            '[tool.nab.packages.acme-core]\nindex = "internal"\n',
        )
        config = read_pyproject_config(path, discover_workspace=False)
        assert index_routes_from_config(config) == [
            IndexRoute(name="acme-core", index="internal"),
        ]

    def test_routing_with_version_specifier_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[[tool.nab.indexes]]\n"
            'name = "pypi"\n'
            'url = "https://pypi.org/simple/"\n'
            "[[tool.nab.indexes]]\n"
            'name = "internal"\n'
            'url = "https://pkgs.example.com/simple/"\n'
            '[tool.nab.packages."foo <= 2"]\nindex = "internal"\n',
        )
        with pytest.raises(ConfigError, match="bare-name requirements"):
            read_pyproject_config(path, discover_workspace=False)

    def test_routing_unknown_index_rejected(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab.packages.foo]\nindex = "nope"\n')
        with pytest.raises(
            ConfigError, match=r"packages\.'foo'\.index routes to undeclared index"
        ):
            read_pyproject_config(path, discover_workspace=False)

    def test_routing_unknown_index_via_package_rules_names_that_surface(
        self, tmp_path: Path
    ) -> None:
        path = write(
            tmp_path,
            '[[tool.nab.package-rules]]\nmatch = ["foo"]\nindex = "nope"\n',
        )
        with pytest.raises(
            ConfigError,
            match=r"package-rules\[0\]\.index routes to undeclared index",
        ):
            read_pyproject_config(path, discover_workspace=False)

    def test_index_must_be_string(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[tool.nab.packages.foo]\nindex = 1\n")
        with pytest.raises(ConfigError, match="index must be a string"):
            read_pyproject_config(path, discover_workspace=False)

    def test_strict_true_is_accepted(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[[tool.nab.indexes]]\n"
            'name = "pypi"\n'
            'url = "https://pypi.org/simple/"\n'
            '[tool.nab.packages.foo]\nindex = "pypi"\nstrict = true\n',
        )
        (override,) = read_pyproject_config(
            path, discover_workspace=False
        ).package_overrides
        assert override.index == "pypi"

    def test_strict_false_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[[tool.nab.indexes]]\n"
            'name = "pypi"\n'
            'url = "https://pypi.org/simple/"\n'
            '[tool.nab.packages.foo]\nindex = "pypi"\nstrict = false\n',
        )
        with pytest.raises(ConfigError, match="strict = false"):
            read_pyproject_config(path, discover_workspace=False)

    def test_strict_without_index_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab.packages.foo]\ndist-policy = "sdist-only"\nstrict = true\n',
        )
        with pytest.raises(ConfigError, match="only meaningful alongside"):
            read_pyproject_config(path, discover_workspace=False)

    def test_strict_must_be_bool(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[[tool.nab.indexes]]\n"
            'name = "pypi"\n'
            'url = "https://pypi.org/simple/"\n'
            '[tool.nab.packages.foo]\nindex = "pypi"\nstrict = "x"\n',
        )
        with pytest.raises(ConfigError, match="strict must be a boolean"):
            read_pyproject_config(path, discover_workspace=False)

    def test_dist_policy_must_be_string_or_table(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[tool.nab.packages.foo]\ndist-policy = 1\n")
        with pytest.raises(ConfigError, match="must be a policy string or a table"):
            read_pyproject_config(path, discover_workspace=False)

    def test_dist_policy_table_unknown_key(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[tool.nab.packages.foo]\n"
            'dist-policy = { policy = "sdist-only", bogus = 1 }\n',
        )
        with pytest.raises(ConfigError, match="has unknown key"):
            read_pyproject_config(path, discover_workspace=False)

    def test_dist_policy_table_missing_policy(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[tool.nab.packages.foo]\ndist-policy = { trust-unverified-deps = true }\n",
        )
        with pytest.raises(ConfigError, match="table must set 'policy'"):
            read_pyproject_config(path, discover_workspace=False)

    def test_dist_policy_table_trust_must_be_bool(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[tool.nab.packages.foo]\n"
            'dist-policy = { policy = "sdist-only", trust-unverified-deps = 1 }\n',
        )
        with pytest.raises(
            ConfigError, match="trust-unverified-deps must be a boolean"
        ):
            read_pyproject_config(path, discover_workspace=False)

    def test_must_set_a_body(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[tool.nab.packages.foo]\n")
        with pytest.raises(ConfigError, match="sets no policy"):
            read_pyproject_config(path, discover_workspace=False)

    def test_body_must_be_table(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[tool.nab.packages]\nfoo = 1\n")
        with pytest.raises(ConfigError, match="'foo' must be a table"):
            read_pyproject_config(path, discover_workspace=False)

    def test_unknown_key_rejected(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[tool.nab.packages.foo]\nbogus = 1\n")
        with pytest.raises(ConfigError, match="unknown override key"):
            read_pyproject_config(path, discover_workspace=False)

    def test_deferred_key_rejected(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab.packages.foo]\nresolution = "lowest"\n')
        with pytest.raises(ConfigError, match="are not supported") as excinfo:
            read_pyproject_config(path, discover_workspace=False)
        # A non-metadata deferred key carries no flat-body advice.
        assert "flat body keys" not in str(excinfo.value)

    def test_metadata_key_advises_flat_body(self, tmp_path: Path) -> None:
        # The nested ``metadata`` table is rejected with a hint pointing at
        # the flat body keys the package surface accepts.
        path = write(
            tmp_path,
            '[tool.nab.packages.foo]\nmetadata = { requires-python = ">=3.6" }\n',
        )
        with pytest.raises(ConfigError, match="flat body keys") as excinfo:
            read_pyproject_config(path, discover_workspace=False)
        assert "are not supported" in str(excinfo.value)

    def test_marker_in_key_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[tool.nab.packages.\"foo ; sys_platform == 'linux'\"]\n"
            'dist-policy = "sdist-only"\n',
        )
        with pytest.raises(ConfigError, match="extras, markers, and URLs"):
            read_pyproject_config(path, discover_workspace=False)

    def test_extra_in_key_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path, '[tool.nab.packages."foo[bar]"]\ndist-policy = "sdist-only"\n'
        )
        with pytest.raises(ConfigError, match="extras, markers, and URLs"):
            read_pyproject_config(path, discover_workspace=False)

    def test_url_in_key_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab.packages."foo @ https://example.com/foo.whl"]\n'
            'dist-policy = "sdist-only"\n',
        )
        with pytest.raises(ConfigError, match="extras, markers, and URLs"):
            read_pyproject_config(path, discover_workspace=False)

    def test_glob_key_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path, '[tool.nab.packages."acme-*"]\ndist-policy = "sdist-only"\n'
        )
        with pytest.raises(ConfigError, match="not a valid PEP 508 requirement"):
            read_pyproject_config(path, discover_workspace=False)

    def test_non_routing_entry_skipped_in_routes(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab.packages.foo]\ndist-policy = "sdist-only"\n')
        config = read_pyproject_config(path, discover_workspace=False)
        assert index_routes_from_config(config) == []

    def test_uppercase_name_and_specifier_in_one_key(self, tmp_path: Path) -> None:
        # The key is Requirement()-parsed before only its .name is
        # canonicalised, so casing and the specifier both survive.
        path = write(
            tmp_path, '[tool.nab.packages."Pkg_Foo <= 2"]\ndist-policy = "sdist-only"\n'
        )
        (override,) = read_pyproject_config(
            path, discover_workspace=False
        ).package_overrides
        assert override.name == "pkg-foo"
        assert str(override.requirement.specifier) == "<=2"
        assert Version("1.0") in override.version_range
        assert Version("3.0") not in override.version_range

    def test_as_array_rejected(self, tmp_path: Path) -> None:
        # The plural ``packages`` key is the sugar table; an array there is
        # almost certainly a mistyped ``[[tool.nab.package-rules]]``.
        path = write(tmp_path, '[[tool.nab.packages]]\nmatch = ["foo"]\n')
        with pytest.raises(ConfigError, match="name-keyed table form") as excinfo:
            read_pyproject_config(path, discover_workspace=False)
        message = str(excinfo.value)
        assert "[[tool.nab.package-rules]]" in message
        assert "match" in message

    def test_must_be_table(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\npackages = "x"\n')
        with pytest.raises(ConfigError, match="must be a table keyed by package name"):
            read_pyproject_config(path, discover_workspace=False)


class TestPackageRules:
    """``[[tool.nab.package-rules]]`` applies one body across several requirements."""

    def test_match_several_packages(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[[tool.nab.package-rules]]\n"
            'match = ["lxml", "xmlsec"]\n'
            'dist-policy = "sdist-only"\n',
        )
        first, second = read_pyproject_config(
            path, discover_workspace=False
        ).package_overrides
        assert first.name == "lxml"
        assert second.name == "xmlsec"
        assert first.dist_policy is DistPolicy.SDIST_ONLY
        assert second.dist_policy is DistPolicy.SDIST_ONLY

    def test_routing_many_packages_to_one_index(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[[tool.nab.indexes]]\n"
            'name = "pypi"\n'
            'url = "https://pypi.org/simple/"\n'
            "[[tool.nab.indexes]]\n"
            'name = "internal"\n'
            'url = "https://pkgs.example.com/simple/"\n'
            "[[tool.nab.package-rules]]\n"
            'match = ["acme-core", "acme-utils"]\n'
            'index = "internal"\n',
        )
        config = read_pyproject_config(path, discover_workspace=False)
        assert index_routes_from_config(config) == [
            IndexRoute(name="acme-core", index="internal"),
            IndexRoute(name="acme-utils", index="internal"),
        ]

    def test_routing_with_version_specifier_rejected(self, tmp_path: Path) -> None:
        # The rule form shares the bare-name routing guard with the table
        # form: a match entry carrying a specifier together with index is
        # rejected.
        path = write(
            tmp_path,
            "[[tool.nab.indexes]]\n"
            'name = "pypi"\n'
            'url = "https://pypi.org/simple/"\n'
            "[[tool.nab.indexes]]\n"
            'name = "internal"\n'
            'url = "https://pkgs.example.com/simple/"\n'
            "[[tool.nab.package-rules]]\n"
            'match = ["foo <= 2"]\n'
            'index = "internal"\n',
        )
        with pytest.raises(ConfigError, match="bare-name requirements"):
            read_pyproject_config(path, discover_workspace=False)

    def test_version_specifier_scopes_the_entry(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[[tool.nab.package-rules]]\nmatch = ["lxml <= 2"]\ndist-policy = "sdist-only"\n',
        )
        (override,) = read_pyproject_config(
            path, discover_workspace=False
        ).package_overrides
        assert str(override.requirement.specifier) == "<=2"

    def test_must_carry_match(self, tmp_path: Path) -> None:
        path = write(
            tmp_path, '[[tool.nab.package-rules]]\ndist-policy = "sdist-only"\n'
        )
        with pytest.raises(ConfigError, match="must carry a 'match' selector"):
            read_pyproject_config(path, discover_workspace=False)

    def test_match_must_be_list(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[[tool.nab.package-rules]]\nmatch = "lxml"\ndist-policy = "sdist-only"\n',
        )
        with pytest.raises(ConfigError, match="match must be a list of strings"):
            read_pyproject_config(path, discover_workspace=False)

    def test_must_set_a_body(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[[tool.nab.package-rules]]\nmatch = ["foo"]\n')
        with pytest.raises(ConfigError, match="sets no policy"):
            read_pyproject_config(path, discover_workspace=False)

    def test_unknown_key_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path, '[[tool.nab.package-rules]]\nmatch = ["foo"]\nbogus = 1\n'
        )
        with pytest.raises(ConfigError, match="unknown override key"):
            read_pyproject_config(path, discover_workspace=False)

    def test_deferred_key_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path, '[[tool.nab.package-rules]]\nmatch = ["foo"]\nmarker = "x"\n'
        )
        with pytest.raises(ConfigError, match="are not supported"):
            read_pyproject_config(path, discover_workspace=False)

    def test_match_with_marker_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[[tool.nab.package-rules]]\n"
            "match = [\"foo ; sys_platform == 'linux'\"]\n"
            'dist-policy = "sdist-only"\n',
        )
        with pytest.raises(ConfigError, match="extras, markers, and URLs"):
            read_pyproject_config(path, discover_workspace=False)

    def test_as_table_rejected(self, tmp_path: Path) -> None:
        # ``package-rules`` is the array form; a name-keyed table belongs in
        # ``[tool.nab.packages.<name>]``.
        path = write(
            tmp_path, '[tool.nab.package-rules.foo]\ndist-policy = "sdist-only"\n'
        )
        with pytest.raises(ConfigError, match="must be an array of tables") as excinfo:
            read_pyproject_config(path, discover_workspace=False)
        assert "[tool.nab.packages.<name>]" in str(excinfo.value)

    def test_entry_must_be_table(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\npackage-rules = ["x"]\n')
        with pytest.raises(ConfigError, match=r"package-rules\[0\] must be a table"):
            read_pyproject_config(path, discover_workspace=False)

    def test_intra_rule_duplicate_package_conflicts(self, tmp_path: Path) -> None:
        # Two match entries that canonicalise to one package and set the same
        # field overlap (full range): a conflict, not a silent dedup.
        path = write(
            tmp_path,
            '[[tool.nab.package-rules]]\nmatch = ["foo", "Foo"]\ndist-policy = "sdist-only"\n',
        )
        with pytest.raises(ConfigError, match="overlapping versions"):
            read_pyproject_config(path, discover_workspace=False)


class TestPackageOverrideConflicts:
    """The combined per-package overrides must not conflict on one field."""

    def test_overlapping_ranges_for_one_field_rejected(self, tmp_path: Path) -> None:
        # ``lxml <= 2`` and ``lxml >= 1`` overlap on [1, 2]; setting the
        # same field for both is a parse-time conflict (no precedence).
        path = write(
            tmp_path,
            '[tool.nab.packages."lxml <= 2"]\n'
            'dist-policy = "sdist-only"\n'
            '[tool.nab.packages."lxml >= 1"]\n'
            'dist-policy = "wheel-only"\n',
        )
        with pytest.raises(ConfigError, match="overlapping versions"):
            read_pyproject_config(path, discover_workspace=False)

    def test_disjoint_ranges_for_one_field_ok(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab.packages."lxml <= 2"]\n'
            'dist-policy = "sdist-only"\n'
            '[tool.nab.packages."lxml >= 3"]\n'
            'dist-policy = "wheel-only"\n',
        )
        low, high = read_pyproject_config(
            path, discover_workspace=False
        ).package_overrides
        assert low.dist_policy is DistPolicy.SDIST_ONLY
        assert high.dist_policy is DistPolicy.WHEEL_ONLY

    def test_overlap_check_is_per_field(self, tmp_path: Path) -> None:
        # Overlapping ranges that set DIFFERENT fields are fine; the
        # non-overlap rule is per (package, field).
        path = write(
            tmp_path,
            '[tool.nab.packages."lxml <= 2"]\n'
            'dist-policy = "sdist-only"\n'
            '[tool.nab.packages."lxml >= 1"]\n'
            'build-policy = "build-remote"\n',
        )
        config = read_pyproject_config(path, discover_workspace=False)
        assert len(config.package_overrides) == 2

    def test_single_entry_with_global_default_is_not_a_conflict(
        self, tmp_path: Path
    ) -> None:
        path = write(
            tmp_path,
            "[tool.nab]\n"
            'dist-policy = "wheel-or-sdist"\n'
            '[tool.nab.packages.lxml]\ndist-policy = "sdist-only"\n',
        )
        (override,) = read_pyproject_config(
            path, discover_workspace=False
        ).package_overrides
        assert override.dist_policy is DistPolicy.SDIST_ONLY

    def test_bare_name_overlaps_a_scoped_entry(self, tmp_path: Path) -> None:
        # A bare name is the full range, so it overlaps any scoped range
        # for the same package and field.
        path = write(
            tmp_path,
            "[tool.nab.packages.lxml]\n"
            'dist-policy = "sdist-only"\n'
            '[tool.nab.packages."lxml >= 3"]\n'
            'dist-policy = "wheel-only"\n',
        )
        with pytest.raises(ConfigError, match="overlapping versions"):
            read_pyproject_config(path, discover_workspace=False)

    def test_two_routes_for_one_package_rejected(self, tmp_path: Path) -> None:
        # One route from each surface for the same package always overlaps.
        path = write(
            tmp_path,
            "[[tool.nab.indexes]]\n"
            'name = "pypi"\n'
            'url = "https://pypi.org/simple/"\n'
            "[[tool.nab.indexes]]\n"
            'name = "internal"\n'
            'url = "https://pkgs.example.com/simple/"\n'
            '[tool.nab.packages.foo]\nindex = "pypi"\n'
            "[[tool.nab.package-rules]]\n"
            'match = ["foo"]\nindex = "internal"\n',
        )
        with pytest.raises(ConfigError, match="overlapping versions"):
            read_pyproject_config(path, discover_workspace=False)

    def test_sugar_and_rule_conflict_on_one_field(self, tmp_path: Path) -> None:
        # Both surfaces expand into one list before the conflict check.
        path = write(
            tmp_path,
            '[tool.nab.packages.foo]\ndist-policy = "sdist-only"\n'
            "[[tool.nab.package-rules]]\n"
            'match = ["foo"]\ndist-policy = "wheel-only"\n',
        )
        with pytest.raises(ConfigError, match="overlapping versions"):
            read_pyproject_config(path, discover_workspace=False)

    def test_sugar_and_rule_union_distinct_packages(self, tmp_path: Path) -> None:
        # Distinct packages from both surfaces survive as the union, in
        # declared order, each keeping its own body.
        path = write(
            tmp_path,
            '[tool.nab.packages.foo]\ndist-policy = "sdist-only"\n'
            "[[tool.nab.package-rules]]\n"
            'match = ["bar"]\nbuild-policy = "build-remote"\n',
        )
        foo, bar = read_pyproject_config(
            path, discover_workspace=False
        ).package_overrides
        assert foo.name == "foo"
        assert foo.dist_policy is DistPolicy.SDIST_ONLY
        assert foo.build_policy is None
        assert bar.name == "bar"
        assert bar.build_policy is BuildPolicy.BUILD_REMOTE
        assert bar.dist_policy is None

    @pytest.mark.parametrize(
        "body",
        [
            'build-policy = "build-remote"',
            'uploaded-prior-to = "2026-05-01T00:00:00Z"',
            "uploaded-prior-to = false",
        ],
    )
    def test_overlapping_same_field_rejected_per_field(
        self, tmp_path: Path, body: str
    ) -> None:
        path = write(
            tmp_path,
            f'[tool.nab.packages."foo <= 2"]\n{body}\n'
            f'[tool.nab.packages."foo >= 1"]\n{body}\n',
        )
        with pytest.raises(ConfigError, match="overlapping versions"):
            read_pyproject_config(path, discover_workspace=False)

    def test_uploaded_prior_to_cutoff_vs_disable_overlap_rejected(
        self, tmp_path: Path
    ) -> None:
        # A datetime cutoff and a ``false`` disable are two forms of the one
        # uploaded-prior-to field, so overlapping ranges still conflict (and
        # across surfaces too).
        path = write(
            tmp_path,
            '[tool.nab.packages."foo <= 2"]\n'
            'uploaded-prior-to = "2026-05-01T00:00:00Z"\n'
            "[[tool.nab.package-rules]]\n"
            'match = ["foo >= 1"]\nuploaded-prior-to = false\n',
        )
        with pytest.raises(ConfigError, match="overlapping versions"):
            read_pyproject_config(path, discover_workspace=False)

    def test_overlapping_different_fields_do_not_conflict(self, tmp_path: Path) -> None:
        # uploaded-prior-to (disabled) and build-policy are different fields,
        # so overlapping ranges are fine.
        path = write(
            tmp_path,
            '[tool.nab.packages."foo <= 2"]\nuploaded-prior-to = false\n'
            '[tool.nab.packages."foo >= 1"]\nbuild-policy = "build-remote"\n',
        )
        config = read_pyproject_config(path, discover_workspace=False)
        assert len(config.package_overrides) == 2


class TestPackageOverrideDependencies:
    """The ``dependencies`` metadata override replaces a package's deps."""

    def test_parses_a_list_of_requirements(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[tool.nab.packages.chumpy]\n"
            'dependencies = ["numpy>=1.8.1", "six>=1.11.0 ; python_version < \'3\'"]\n',
        )
        (override,) = read_pyproject_config(
            path, discover_workspace=False
        ).package_overrides
        assert override.name == "chumpy"
        assert override.dependencies is not None
        rendered = [str(r) for r in override.dependencies]
        assert rendered == ["numpy>=1.8.1", 'six>=1.11.0; python_version < "3"']

    def test_empty_list_stored_as_empty_tuple(self, tmp_path: Path) -> None:
        # An empty list is a first-class value (replace with zero deps),
        # distinct from the key being absent, and lifts the empty-body gate.
        path = write(tmp_path, "[tool.nab.packages.broken]\ndependencies = []\n")
        (override,) = read_pyproject_config(
            path, discover_workspace=False
        ).package_overrides
        assert override.dependencies == ()
        assert override.dist_policy is None

    def test_only_dependencies_is_a_valid_body(self, tmp_path: Path) -> None:
        # A body carrying only ``dependencies`` is not rejected as empty.
        path = write(
            tmp_path,
            '[tool.nab.packages.foo]\ndependencies = ["bar"]\n',
        )
        (override,) = read_pyproject_config(
            path, discover_workspace=False
        ).package_overrides
        assert [str(r) for r in override.dependencies or ()] == ["bar"]

    def test_bad_pep508_string_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab.packages.foo]\ndependencies = ["not a valid == req =="]\n',
        )
        with pytest.raises(ConfigError, match="not a valid PEP 508"):
            read_pyproject_config(path, discover_workspace=False)

    def test_non_list_value_rejected(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab.packages.foo]\ndependencies = "bar"\n')
        with pytest.raises(ConfigError, match="must be a list of PEP 508"):
            read_pyproject_config(path, discover_workspace=False)

    def test_non_string_item_rejected(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[tool.nab.packages.foo]\ndependencies = [1]\n")
        with pytest.raises(ConfigError, match=r"dependencies\[0\] must be a string"):
            read_pyproject_config(path, discover_workspace=False)

    def test_via_package_rules(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[[tool.nab.package-rules]]\n"
            'match = ["some-pkg <= 2.0"]\n'
            'dependencies = ["requests>=2"]\n',
        )
        (override,) = read_pyproject_config(
            path, discover_workspace=False
        ).package_overrides
        assert override.name == "some-pkg"
        assert [str(r) for r in override.dependencies or ()] == ["requests>=2"]

    def test_overlapping_dependencies_entries_rejected(self, tmp_path: Path) -> None:
        # Two entries both setting ``dependencies`` over non-disjoint ranges
        # is a parse error: which list wins for a version in the overlap is
        # ambiguous.
        path = write(
            tmp_path,
            '[tool.nab.packages."foo <= 2"]\ndependencies = ["a"]\n'
            '[tool.nab.packages."foo >= 1"]\ndependencies = ["b"]\n',
        )
        with pytest.raises(ConfigError, match="overlapping versions"):
            read_pyproject_config(path, discover_workspace=False)

    def test_disjoint_dependencies_entries_ok(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab.packages."foo <= 2"]\ndependencies = ["a"]\n'
            '[tool.nab.packages."foo >= 3"]\ndependencies = ["b"]\n',
        )
        low, high = read_pyproject_config(
            path, discover_workspace=False
        ).package_overrides
        assert [str(r) for r in low.dependencies or ()] == ["a"]
        assert [str(r) for r in high.dependencies or ()] == ["b"]

    def test_empty_and_populated_overlap_still_rejected(self, tmp_path: Path) -> None:
        # An empty list counts as set, so it overlaps a populated entry.
        path = write(
            tmp_path,
            '[tool.nab.packages."foo <= 2"]\ndependencies = []\n'
            '[tool.nab.packages."foo >= 1"]\ndependencies = ["b"]\n',
        )
        with pytest.raises(ConfigError, match="overlapping versions"):
            read_pyproject_config(path, discover_workspace=False)

    def test_dependencies_and_dist_policy_co_set(self, tmp_path: Path) -> None:
        # Distinct fields, so both may sit on one entry with no conflict.
        path = write(
            tmp_path,
            "[tool.nab.packages.foo]\n"
            'dist-policy = "sdist-only"\n'
            'dependencies = ["bar>=1"]\n',
        )
        (override,) = read_pyproject_config(
            path, discover_workspace=False
        ).package_overrides
        assert override.dist_policy is DistPolicy.SDIST_ONLY
        assert [str(r) for r in override.dependencies or ()] == ["bar>=1"]

    def test_dependencies_key_rejected_on_index_surface(self, tmp_path: Path) -> None:
        # Metadata fields live only on the per-package surface; the index
        # body's own unknown-key check rejects ``dependencies``.
        path = write(
            tmp_path,
            "[[tool.nab.indexes]]\n"
            'name = "internal"\n'
            'url = "https://pkgs.example.com/simple/"\n'
            '[tool.nab.index.internal]\ndependencies = ["bar"]\n',
        )
        with pytest.raises(ConfigError):
            read_pyproject_config(path, discover_workspace=False)


class TestPackageOverrideRequiresPython:
    """The ``requires-python`` metadata override replaces the Python spec."""

    def test_parses_a_specifier(self, tmp_path: Path) -> None:
        # A body carrying only ``requires-python`` is a valid (non-empty)
        # body, and the specifier stores raw like the top-level field.
        path = write(
            tmp_path,
            '[tool.nab.packages.flask]\nrequires-python = ">=3.6"\n',
        )
        (override,) = read_pyproject_config(
            path, discover_workspace=False
        ).package_overrides
        assert override.requires_python == ">=3.6"
        assert override.dependencies is None

    def test_absent_key_is_none(self, tmp_path: Path) -> None:
        # An absent requires-python parses to None, like the sibling fields.
        path = write(
            tmp_path,
            '[tool.nab.packages.flask]\ndist-policy = "wheel-only"\n',
        )
        (override,) = read_pyproject_config(
            path, discover_workspace=False
        ).package_overrides
        assert override.requires_python is None

    def test_bare_version_rejected_names_the_entry(self, tmp_path: Path) -> None:
        # A bare "3.13" is not a specifier: the rejection names the entry
        # selector and reuses the top-level "Did you mean" suggestion.
        path = write(
            tmp_path,
            '[tool.nab.packages.flask]\nrequires-python = "3.13"\n',
        )
        with pytest.raises(
            ConfigError,
            match=r"packages\.'flask'\.requires-python must be.*Did you mean",
        ):
            read_pyproject_config(path, discover_workspace=False)

    def test_non_string_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[tool.nab.packages.flask]\nrequires-python = 3\n",
        )
        with pytest.raises(ConfigError, match="must be a string"):
            read_pyproject_config(path, discover_workspace=False)

    def test_overlapping_entries_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab.packages."foo <= 2"]\nrequires-python = ">=3.6"\n'
            '[tool.nab.packages."foo >= 1"]\nrequires-python = ">=3.7"\n',
        )
        with pytest.raises(ConfigError, match="overlapping versions"):
            read_pyproject_config(path, discover_workspace=False)


class TestPackageOverrideProvidesExtra:
    """The ``provides-extra`` metadata override replaces the declared extras."""

    def test_parses_and_normalises(self, tmp_path: Path) -> None:
        # PEP 685: the names normalise, so spelling does not matter.
        path = write(
            tmp_path,
            '[tool.nab.packages.flask]\nprovides-extra = ["Dot_Env", "async"]\n',
        )
        (override,) = read_pyproject_config(
            path, discover_workspace=False
        ).package_overrides
        assert override.provides_extra == ("dot-env", "async")

    def test_empty_list_stored_as_empty_tuple(self, tmp_path: Path) -> None:
        # A present-but-empty list declares no extras, distinct from absent,
        # and lifts the empty-body gate.
        path = write(tmp_path, "[tool.nab.packages.flask]\nprovides-extra = []\n")
        (override,) = read_pyproject_config(
            path, discover_workspace=False
        ).package_overrides
        assert override.provides_extra == ()

    def test_non_list_rejected(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab.packages.flask]\nprovides-extra = "x"\n')
        with pytest.raises(ConfigError, match="must be a list of extra names"):
            read_pyproject_config(path, discover_workspace=False)

    def test_non_string_item_rejected(self, tmp_path: Path) -> None:
        path = write(tmp_path, "[tool.nab.packages.flask]\nprovides-extra = [1]\n")
        with pytest.raises(ConfigError, match=r"provides-extra\[0\] must be a string"):
            read_pyproject_config(path, discover_workspace=False)

    def test_overlapping_entries_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab.packages."foo <= 2"]\nprovides-extra = ["a"]\n'
            '[tool.nab.packages."foo >= 1"]\nprovides-extra = ["b"]\n',
        )
        with pytest.raises(ConfigError, match="overlapping versions"):
            read_pyproject_config(path, discover_workspace=False)


class TestPackageMetadataBundle:
    """All three metadata fields sit together on one entry (uv parity)."""

    def test_full_bundle_parses(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[tool.nab.packages.flask]\n"
            'dependencies = ["werkzeug>=0.14", "click>=5.1 ; extra == \'dotenv\'"]\n'
            'requires-python = ">=3.6"\n'
            'provides-extra = ["dotenv"]\n',
        )
        (override,) = read_pyproject_config(
            path, discover_workspace=False
        ).package_overrides
        assert [str(r) for r in override.dependencies or ()] == [
            "werkzeug>=0.14",
            'click>=5.1; extra == "dotenv"',
        ]
        assert override.requires_python == ">=3.6"
        assert override.provides_extra == ("dotenv",)

    def test_distinct_fields_do_not_overlap(self, tmp_path: Path) -> None:
        # Different metadata fields over overlapping ranges are legal: the
        # overlap check is per field, so deps on one range and
        # requires-python on another (overlapping) range coexist.
        path = write(
            tmp_path,
            '[tool.nab.packages."foo <= 2"]\ndependencies = ["a"]\n'
            '[tool.nab.packages."foo >= 1"]\nrequires-python = ">=3.7"\n',
        )
        low, high = read_pyproject_config(
            path, discover_workspace=False
        ).package_overrides
        assert [str(r) for r in low.dependencies or ()] == ["a"]
        assert high.requires_python == ">=3.7"


class TestIndexOverrides:
    """``[tool.nab.index.<name>]`` parses into a name-keyed policy map."""

    def _two_indexes(self) -> str:
        return (
            "[[tool.nab.indexes]]\n"
            'name = "pypi"\n'
            'url = "https://pypi.org/simple/"\n'
            "[[tool.nab.indexes]]\n"
            'name = "internal"\n'
            'url = "https://pkgs.example.com/simple/"\n'
        )

    def test_dist_policy(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            self._two_indexes()
            + '[tool.nab.index.internal]\ndist-policy = "wheel-only"\n',
        )
        config = read_pyproject_config(path, discover_workspace=False)
        assert config.index_overrides["internal"].dist_policy is DistPolicy.WHEEL_ONLY

    def test_inline_form(self, tmp_path: Path) -> None:
        # The inline map spelling parses to the same dict as the header form.
        path = write(
            tmp_path,
            self._two_indexes()
            + '[tool.nab.index]\ninternal = { dist-policy = "wheel-only" }\n',
        )
        config = read_pyproject_config(path, discover_workspace=False)
        assert config.index_overrides["internal"].dist_policy is DistPolicy.WHEEL_ONLY

    def test_full_body(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            self._two_indexes() + "[tool.nab.index.internal]\n"
            'build-policy = "build-remote"\n'
            'uploaded-prior-to = "2026-05-01T00:00:00Z"\n'
            'dist-policy = { policy = "sdist-only", trust-unverified-deps = true }\n',
        )
        override = read_pyproject_config(
            path, discover_workspace=False
        ).index_overrides["internal"]
        assert override.build_policy is BuildPolicy.BUILD_REMOTE
        assert override.uploaded_prior_to == datetime(2026, 5, 1, tzinfo=timezone.utc)
        assert override.dist_policy is DistPolicy.SDIST_ONLY
        assert override.dist_trust_unverified_deps is True

    def test_uploaded_prior_to_false_disables(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            self._two_indexes()
            + "[tool.nab.index.internal]\nuploaded-prior-to = false\n",
        )
        override = read_pyproject_config(
            path, discover_workspace=False
        ).index_overrides["internal"]
        assert override.uploaded_prior_to is None
        assert override.uploaded_prior_to_disabled is True

    def test_unknown_index_rejected(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab.index.nope]\ndist-policy = "wheel-only"\n')
        with pytest.raises(ConfigError, match="names undeclared index"):
            read_pyproject_config(path, discover_workspace=False)

    def test_must_be_table(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\nindex = "x"\n')
        with pytest.raises(ConfigError, match="must be a table keyed by index name"):
            read_pyproject_config(path, discover_workspace=False)

    def test_body_must_be_table(self, tmp_path: Path) -> None:
        path = write(tmp_path, self._two_indexes() + "[tool.nab.index]\ninternal = 1\n")
        with pytest.raises(ConfigError, match=r"index\.internal must be a table"):
            read_pyproject_config(path, discover_workspace=False)

    def test_no_routing_key(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            self._two_indexes() + '[tool.nab.index.internal]\nindex = "pypi"\n',
        )
        with pytest.raises(ConfigError, match="unknown override key"):
            read_pyproject_config(path, discover_workspace=False)

    def test_must_set_a_body(self, tmp_path: Path) -> None:
        path = write(tmp_path, self._two_indexes() + "[tool.nab.index.internal]\n")
        with pytest.raises(ConfigError, match="sets no policy"):
            read_pyproject_config(path, discover_workspace=False)

    def test_deferred_key_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            self._two_indexes() + '[tool.nab.index.internal]\nmarker = "x"\n',
        )
        with pytest.raises(ConfigError, match="are not supported"):
            read_pyproject_config(path, discover_workspace=False)

    def test_metadata_key_rejected_without_flat_advice(self, tmp_path: Path) -> None:
        # The flat body keys are rejected here too, so the nested ``metadata``
        # table drops the package surface's flat-body hint.
        path = write(
            tmp_path,
            self._two_indexes()
            + '[tool.nab.index.internal]\nmetadata = { requires-python = ">=3.6" }\n',
        )
        with pytest.raises(ConfigError, match="are not supported") as excinfo:
            read_pyproject_config(path, discover_workspace=False)
        assert "flat body keys" not in str(excinfo.value)


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

    def test_implementations_default_is_cpython(self, tmp_path: Path) -> None:
        path = write(tmp_path, self._matrix_body())
        matrix = read_pyproject_config(path).matrix
        assert matrix is not None
        assert matrix.implementations == ("cpython",)

    def test_implementations_parsed(self, tmp_path: Path) -> None:
        body = self._matrix_body(implementations='["cpython", "pypy"]')
        matrix = read_pyproject_config(write(tmp_path, body)).matrix
        assert matrix is not None
        assert matrix.implementations == ("cpython", "pypy")

    def test_implementations_empty_rejected(self, tmp_path: Path) -> None:
        body = self._matrix_body(implementations="[]")
        with pytest.raises(ConfigError, match="at least one implementation"):
            read_pyproject_config(write(tmp_path, body))

    def test_implementations_unknown_rejected(self, tmp_path: Path) -> None:
        body = self._matrix_body(implementations='["jython"]')
        with pytest.raises(ConfigError, match="unknown matrix.implementations"):
            read_pyproject_config(write(tmp_path, body))

    def test_duplicate_implementations_rejected(self, tmp_path: Path) -> None:
        # A duplicate makes len(implementations) > 1, which flips
        # multi_implementation on and emits a spurious implementation_name
        # marker a sole-cpython matrix omits.
        body = self._matrix_body(implementations='["cpython", "cpython"]')
        with pytest.raises(ConfigError, match="duplicate entry"):
            read_pyproject_config(write(tmp_path, body))

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

    def _python_axis_body(self, python: str) -> str:
        return (
            "[tool.nab]\n"
            'mode = "universal"\n'
            "[tool.nab.matrix]\n"
            f"python = {python!r}\n"
            'platforms = ["linux_x86_64"]\n'
        )

    @pytest.mark.parametrize(
        "python",
        [">=3.11", "==3.12", "<3.13", "~=3.11", ">=3.11,<3.14", "==3.11.*", ">=3", ""],
    )
    def test_python_minor_axis_accepted(self, tmp_path: Path, python: str) -> None:
        path = write(tmp_path, self._python_axis_body(python))
        matrix = read_pyproject_config(path).matrix
        assert matrix is not None
        assert matrix.python == python

    @pytest.mark.parametrize(
        "python",
        [">=3.11.5", "==3.10.2", "~=3.11.5", "==3.11.0", "==3.11a1", "!=3.11.5"],
    )
    def test_python_finer_than_minor_rejected(
        self, tmp_path: Path, python: str
    ) -> None:
        path = write(tmp_path, self._python_axis_body(python))
        with pytest.raises(ConfigError, match=r"language \(minor\) version"):
            read_pyproject_config(path)

    @pytest.mark.parametrize("python", ["3.11", "garbage", "===foo"])
    def test_python_unparseable_rejected(self, tmp_path: Path, python: str) -> None:
        path = write(tmp_path, self._python_axis_body(python))
        with pytest.raises(ConfigError):
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

    def test_unknown_platform_rejected(self, tmp_path: Path) -> None:
        body = self._matrix_body()
        body = body.replace('["linux_x86_64", "macos_arm64"]', '["frobnicate"]')
        with pytest.raises(ConfigError, match="Unknown platform ids"):
            read_pyproject_config(write(tmp_path, body))

    def test_duplicate_platforms_rejected(self, tmp_path: Path) -> None:
        body = self._matrix_body()
        body = body.replace(
            '["linux_x86_64", "macos_arm64"]', '["linux_x86_64", "linux_x86_64"]'
        )
        with pytest.raises(ConfigError, match="duplicate entry"):
            read_pyproject_config(write(tmp_path, body))

    def test_empty_python_range_rejected(self, tmp_path: Path) -> None:
        body = self._matrix_body()
        body = body.replace('">=3.11,<3.14"', '"==3.99"')
        with pytest.raises(ConfigError, match="No known Python versions match"):
            read_pyproject_config(write(tmp_path, body))

    def test_malformed_python_specifier_rejected(self, tmp_path: Path) -> None:
        body = self._matrix_body()
        body = body.replace('">=3.11,<3.14"', '"not a specifier"')
        with pytest.raises(ConfigError, match="must be a PEP 440 specifier"):
            read_pyproject_config(write(tmp_path, body))

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

    def test_python_patches_minor_mismatch_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[tool.nab]\n"
            'mode = "universal"\n'
            "[tool.nab.matrix]\n"
            'python = ">=3.11"\n'
            'platforms = ["linux_x86_64"]\n'
            "[tool.nab.matrix.python-patches]\n"
            '"3.11" = "3.12.1"\n',
        )
        with pytest.raises(ConfigError, match="not a patch release of '3.11'"):
            read_pyproject_config(path)

    def test_python_patches_unparseable_version_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[tool.nab]\n"
            'mode = "universal"\n'
            "[tool.nab.matrix]\n"
            'python = ">=3.11"\n'
            'platforms = ["linux_x86_64"]\n'
            "[tool.nab.matrix.python-patches]\n"
            '"3.11" = "not-a-version"\n',
        )
        with pytest.raises(ConfigError, match="python-patches expects version"):
            read_pyproject_config(path)

    def test_python_patches_non_minor_key_rejected(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "[tool.nab]\n"
            'mode = "universal"\n'
            "[tool.nab.matrix]\n"
            'python = ">=3.11"\n'
            'platforms = ["linux_x86_64"]\n'
            "[tool.nab.matrix.python-patches]\n"
            '"3.11.0" = "3.11.9"\n',
        )
        with pytest.raises(ConfigError, match="python_patches"):
            read_pyproject_config(path)


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
            LocalSource(name="alpha", path=str(member.parent), editable=True),
        )
        assert config.build_policy is BuildPolicy.BUILD_LOCAL

    def test_explicit_local_source_wins_over_workspace_member(
        self, tmp_path: Path
    ) -> None:
        member = self._ws(tmp_path)
        # Bare ``/explicit/...`` is drive-relative on Windows; use tmp_path.
        explicit = (tmp_path / "explicit-alpha").resolve()
        member.write_text(
            '[project]\nname = "alpha"\nversion = "0"\n'
            "[tool.nab]\n"
            "[[tool.nab.local-sources]]\n"
            'name = "alpha"\n'
            f'path = "{explicit.as_posix()}"\n',
        )
        config = read_pyproject_config(member)
        assert config.local_sources == (LocalSource(name="alpha", path=str(explicit)),)

    def test_shadowed_member_excluded_from_workspace_member_names(
        self, tmp_path: Path
    ) -> None:
        member = self._ws(tmp_path)
        explicit = (tmp_path / "explicit-alpha").resolve()
        member.write_text(
            '[project]\nname = "alpha"\nversion = "0"\n'
            "[tool.nab]\n"
            "[[tool.nab.local-sources]]\n"
            'name = "alpha"\n'
            f'path = "{explicit.as_posix()}"\n',
        )
        config = read_pyproject_config(member)
        assert config.workspace_member_names == frozenset()

    def test_member_colliding_with_vcs_source_rejected(self, tmp_path: Path) -> None:
        """A discovered member sharing a vcs-source name is rejected at parse."""
        root = tmp_path / "pyproject.toml"
        root.write_text(
            '[project]\nname = "ws"\nversion = "0"\n'
            "[tool.nab.workspace]\n"
            'members = ["pkg"]\n'
            "[[tool.nab.vcs-sources]]\n"
            'name = "Alpha"\n'
            'url = "git+https://github.com/me/alpha.git@abc"\n',
        )
        member_dir = tmp_path / "pkg"
        member_dir.mkdir()
        (member_dir / "pyproject.toml").write_text(
            '[project]\nname = "alpha"\nversion = "0"\n',
        )
        with pytest.raises(ConfigError, match="duplicate canonical name"):
            read_pyproject_config(root)

    def test_member_colliding_with_archive_source_rejected(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "pyproject.toml"
        root.write_text(
            '[project]\nname = "ws"\nversion = "0"\n'
            "[tool.nab.workspace]\n"
            'members = ["pkg"]\n'
            "[[tool.nab.archive-sources]]\n"
            'name = "alpha"\n'
            'url = "https://ex.com/alpha-1.0.tar.gz#sha256=' + "e" * 64 + '"\n',
        )
        member_dir = tmp_path / "pkg"
        member_dir.mkdir()
        (member_dir / "pyproject.toml").write_text(
            '[project]\nname = "alpha"\nversion = "0"\n',
        )
        with pytest.raises(ConfigError, match="duplicate canonical name"):
            read_pyproject_config(root)

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

    def test_universal_member_not_promoted(self, tmp_path: Path) -> None:
        """Universal mode keeps never; workspace discovery does not promote it.

        A host build cannot reflect a non-host matrix tuple, so the
        BUILD_LOCAL floor applied to workspace members is skipped.
        """
        member = self._ws(tmp_path)
        member.write_text(
            '[project]\nname = "alpha"\nversion = "0"\n'
            "[tool.nab]\n"
            'mode = "universal"\n' + _UNIVERSAL_MATRIX,
        )
        config = read_pyproject_config(member)
        assert config.build_policy is BuildPolicy.NEVER

    def test_marker_environment_member_not_promoted(self, tmp_path: Path) -> None:
        """A marker overlay keeps never; discovery does not promote it.

        A host build cannot reflect the impersonated marker target, so the
        BUILD_LOCAL floor applied to workspace members is skipped.
        """
        member = self._ws(tmp_path)
        member.write_text(
            '[project]\nname = "alpha"\nversion = "0"\n'
            "[tool.nab]\n"
            'build-policy = "never"\n'
            "[tool.nab.marker-environment]\n"
            'python_version = "3.9"\n',
        )
        config = read_pyproject_config(member)
        assert config.build_policy is BuildPolicy.NEVER

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

    def test_root_conflicts_and_defaults_do_not_flow_to_member(
        self, tmp_path: Path
    ) -> None:
        # Per the documented scope, only workspace members and the
        # build-policy floor cross the root/member boundary; conflicts,
        # default-groups, and constraints stay scoped to the file being
        # locked.
        ws_pyproject = tmp_path / "pyproject.toml"
        ws_pyproject.write_text(
            '[project]\nname = "ws"\nversion = "0"\n'
            "[tool.nab]\n"
            'default-groups = ["dev"]\n'
            'constraints = ["foo<2"]\n'
            'conflicts = [[{ extra = "cpu" }, { extra = "gpu" }]]\n'
            "[tool.nab.workspace]\n"
            'members = ["pkg"]\n',
        )
        member_dir = tmp_path / "pkg"
        member_dir.mkdir()
        member = member_dir / "pyproject.toml"
        member.write_text('[project]\nname = "alpha"\nversion = "0"\n')
        config = read_pyproject_config(member)
        assert config.default_groups == ()
        assert config.constraints == ()
        assert config.conflicts == ()


def _inspect(path: Path, key: str) -> str:
    """Render ``nab config get <key>`` the way the inspector would.

    Discovers the same project-dir ladder ``read_pyproject_config`` reads
    (pyproject + project-dir ``nab.toml``) and renders the effective value,
    so a test can assert the inspector reports exactly what the resolve
    consumes.
    """
    roots = SourceRoots(project_dir=path.parent.resolve(), pyproject=path.resolve())
    layers = discover_layers(roots)
    effective = resolve_config(layers, read_env_layer({}), build_cli_layer({}))
    return render_get(effective, key).strip()


def _explain(path: Path, key: str) -> str:
    roots = SourceRoots(project_dir=path.parent.resolve(), pyproject=path.resolve())
    layers = discover_layers(roots)
    effective = resolve_config(layers, read_env_layer({}), build_cli_layer({}))
    return render_explain(effective, key)


class TestProjectNabTomlConfiguresResolve:
    """A project-dir ``nab.toml`` functionally configures every PROJECT key.

    Each test sets a value ONLY in a project-dir ``nab.toml`` (never in
    pyproject) and asserts (a) ``read_pyproject_config`` (what the resolve
    consumes) reflects it and (b) ``nab config get/explain`` (the
    inspector) agrees, one representative key per structural type.
    """

    def _write(self, tmp_path: Path, pyproject: str, nab_toml: str) -> Path:
        path = tmp_path / "pyproject.toml"
        path.write_text(pyproject)
        (tmp_path / "nab.toml").write_text(nab_toml)
        return path

    def test_scalar_build_policy(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            '[project]\nname = "x"\nversion = "0"\n',
            'build-policy = "never"\n',
        )
        config = read_pyproject_config(path, discover_workspace=False)
        assert config.build_policy is BuildPolicy.NEVER
        assert _inspect(path, "build-policy") == "never"
        assert "project" in _explain(path, "build-policy")

    def test_scalar_dist_policy_folds_trust(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            '[project]\nname = "x"\nversion = "0"\n',
            '[dist-policy]\npolicy = "wheel-only"\ntrust-unverified-deps = true\n',
        )
        config = read_pyproject_config(path, discover_workspace=False)
        assert config.dist_policy is DistPolicy.WHEEL_ONLY
        assert config.trust_unverified_sdist_deps is True
        assert "wheel-only" in _inspect(path, "dist-policy")

    def test_table_vcs(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            '[project]\nname = "x"\nversion = "0"\n',
            '[vcs]\npolicy = "allow"\nallowed-schemes = ["git+https"]\n',
        )
        config = read_pyproject_config(path, discover_workspace=False)
        assert config.vcs.policy is VcsPolicy.ALLOW
        assert config.vcs.allowed_schemes == frozenset({"git+https"})
        assert "policy=allow" in _inspect(path, "vcs")

    def test_table_marker_environment(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            '[project]\nname = "x"\nversion = "0"\n',
            '[marker-environment]\nsys_platform = "linux"\n',
        )
        config = read_pyproject_config(path, discover_workspace=False)
        assert config.marker_environment == {"sys_platform": "linux"}
        assert "sys_platform=linux" in _inspect(path, "marker-environment")

    def test_list_constraints(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            '[project]\nname = "x"\nversion = "0"\n',
            'constraints = ["foo<2"]\n',
        )
        config = read_pyproject_config(path, discover_workspace=False)
        assert config.constraints == ("foo<2",)
        assert _inspect(path, "constraints") == "foo<2"

    def test_list_constraints_concat_across_files(self, tmp_path: Path) -> None:
        # Array concat: a pyproject list and a project-nab.toml list merge
        # additively, they do not conflict.
        path = self._write(
            tmp_path,
            '[project]\nname = "x"\nversion = "0"\n'
            '[tool.nab]\nconstraints = ["foo<2"]\n',
            'constraints = ["bar<3"]\n',
        )
        config = read_pyproject_config(path, discover_workspace=False)
        assert config.constraints == ("foo<2", "bar<3")
        assert _inspect(path, "constraints") == "foo<2, bar<3"

    def test_array_of_tables_indexes(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            '[project]\nname = "x"\nversion = "0"\n',
            "[[indexes]]\n"
            'name = "internal"\n'
            'url = "https://pkgs.example.com/simple/"\n',
        )
        config = read_pyproject_config(path, discover_workspace=False)
        assert [i.name for i in config.indexes] == ["internal"]
        assert "internal=https://pkgs.example.com/simple/" in _inspect(path, "indexes")

    def test_override_table_packages(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            '[project]\nname = "x"\nversion = "0"\n',
            '[packages.numpy]\nbuild-policy = "never"\n',
        )
        (override,) = read_pyproject_config(
            path, discover_workspace=False
        ).package_overrides
        assert override.name == "numpy"
        assert override.build_policy is BuildPolicy.NEVER
        assert "numpy" in _inspect(path, "packages")

    def test_override_table_index(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            '[project]\nname = "x"\nversion = "0"\n',
            "[[indexes]]\n"
            'name = "internal"\n'
            'url = "https://pkgs.example.com/simple/"\n'
            '[index.internal]\ndist-policy = "wheel-only"\n',
        )
        config = read_pyproject_config(path, discover_workspace=False)
        assert config.index_overrides["internal"].dist_policy is DistPolicy.WHEEL_ONLY
        assert "internal" in _inspect(path, "index")

    def test_cross_field_matrix_and_mode(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            '[project]\nname = "x"\nversion = "0"\n',
            'mode = "universal"\n'
            "[matrix]\n"
            'python = ">=3.11,<3.13"\n'
            'platforms = ["linux_x86_64"]\n',
        )
        config = read_pyproject_config(path, discover_workspace=False)
        assert config.mode is ResolveMode.UNIVERSAL
        assert config.matrix is not None
        assert config.matrix.platforms == ("linux_x86_64",)
        # Universal mode forces the build-policy floor to never even though
        # the project-nab.toml set no build-policy.
        assert config.build_policy is BuildPolicy.NEVER
        assert _inspect(path, "mode") == "universal"

    def test_uploaded_prior_to_duration_anchored(self, tmp_path: Path) -> None:
        # A P<n>D duration set in the project nab.toml anchors against the
        # lock anchor the resolve threads, not a fresh now().
        path = self._write(
            tmp_path,
            '[project]\nname = "x"\nversion = "0"\n',
            'uploaded-prior-to = "P4D"\n',
        )
        anchor = datetime(2024, 1, 5, tzinfo=timezone.utc)
        config = read_pyproject_config(path, discover_workspace=False, anchor=anchor)
        assert config.uploaded_prior_to == datetime(2024, 1, 1, tzinfo=timezone.utc)


class TestProjectNabTomlGateAndConflict:
    """The category gate and the cross-file conflict fire on the resolve path."""

    def test_user_key_in_pyproject_still_rejected(self, tmp_path: Path) -> None:
        # A USER-scope key in pyproject [tool.nab] is the category gate; it
        # must still error when a project nab.toml is present.
        path = tmp_path / "pyproject.toml"
        path.write_text(
            '[project]\nname = "x"\nversion = "0"\n[tool.nab]\noffline = true\n'
        )
        (tmp_path / "nab.toml").write_text('resolution = "lowest"\n')
        with pytest.raises(
            SourceConfigError,
            match="user-scope option and cannot be set in pyproject",
        ):
            read_pyproject_config(path, discover_workspace=False)

    def test_conflict_across_project_files_rejected(self, tmp_path: Path) -> None:
        # The same scalar key set to different values in pyproject and the
        # project nab.toml is a hard error on the resolve path.
        path = tmp_path / "pyproject.toml"
        path.write_text(
            '[project]\nname = "x"\nversion = "0"\n'
            '[tool.nab]\nbuild-policy = "build-local"\n'
        )
        (tmp_path / "nab.toml").write_text('build-policy = "never"\n')
        with pytest.raises(SourceConfigError, match="conflicting values"):
            read_pyproject_config(path, discover_workspace=False)

    def test_override_overlap_across_project_files_rejected(
        self, tmp_path: Path
    ) -> None:
        # The per-package same-field overlap composes with the cross-file
        # rule: an override in pyproject and an overlapping one in the project
        # nab.toml is the hard overlap error, not a silent last-win.
        path = tmp_path / "pyproject.toml"
        path.write_text(
            '[project]\nname = "x"\nversion = "0"\n'
            '[tool.nab.packages.numpy]\nbuild-policy = "never"\n'
        )
        (tmp_path / "nab.toml").write_text(
            '[packages.numpy]\nbuild-policy = "build-local"\n'
        )
        with pytest.raises(SourceConfigError, match="overlapping versions"):
            read_pyproject_config(path, discover_workspace=False)
