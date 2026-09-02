"""Declare the rows that drive CLI parsing, configuration, and option docs.

A table binds each name once, and body order sets help order. Shared rows
therefore sit in narrow tables whose command sets may differ from nearby rows.
"""

from __future__ import annotations

import types
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, NewType

from nab_project.conflicts import ConflictSet
from nab_project.workspace import WorkspaceConfig
from nab_provider.policy import (
    ArchiveSource,
    BuildPolicy,
    DecisionOrder,
    DistPolicy,
    LocalSource,
    ResolutionStrategy,
    ResolveMode,
    VcsSource,
)
from nab_provider.records import DEFAULT_INDEX_NAME, DEFAULT_INDEX_URL, IndexConfig
from nab_provider.vcs_admission import VcsConfig

from .config import hooks, values
from .config.values import MatrixConfig
from .flagtypes import (
    BuildPolicyFlag,
    DecisionOrderFlag,
    DistPolicyFlag,
    HttpBackend,
    ImplementationFlag,
    LockFormat,
    MatrixOrderFlag,
    ModeFlag,
    ResolutionFlag,
)
from .optiondefs import GLOBAL, Opt
from .optionlower import table_rows
from .optionrows import (
    Count,
    Eager,
    Item,
    Items,
    Key,
    Layer,
    Many,
    Operand,
    Pairs,
    Scope,
    Star,
    Switch,
    Table,
    Tri,
    Value,
    Verb,
)
from .output import ColorChoice

Group = NewType("Group", str)
Requirement = NewType("Requirement", str)
Specifier = NewType("Specifier", str)

_EMPTY_MAPPING: types.MappingProxyType[str, Any] = types.MappingProxyType({})

_LAYERED = ("lock", "download", "config")
_EVERY = ("lock", "download", "config", "cache")
_RUN = ("lock", "download")


class Root(Table, on=GLOBAL, docs="reference/cli.md"):
    """The rows read on either side of the command name."""

    verbose = Count(
        short="v",
        help="raise verbosity; -v adds INFO records, -vv adds DEBUG",
    )

    quiet = Count(
        short="q",
        help="lower verbosity; -q drops the summary and notes, -qq keeps errors alone",
    )

    color = Value[ColorChoice](
        help="when to colour nab's output",
    )

    no_color = Switch(
        default=False,
        help="shorthand for --color never",
    )

    no_progress = Switch(
        default=False,
        help="suppress the live progress line",
    )

    version = Eager(
        short="V",
        help="print the version and exit",
    )

    help = Eager(
        short="h",
        help="print this help and exit",
    )


class ProjectKeys(
    Table, on=_LAYERED, scope=Scope.PROJECT, docs="reference/configuration.md"
):
    """The project's own settings: twelve carry a flag, thirteen are file-only."""

    resolution = Value[ResolutionFlag | None](
        mirrors=ResolutionStrategy,
        key=Layer[ResolutionStrategy](
            rdefault=ResolutionStrategy.HIGHEST,
            parse=values.parse_resolution,
            render=hooks.render_enum_value,
        ),
        help="which version of each package to prefer",
    )

    decision_order = Value[DecisionOrderFlag | None](
        mirrors=DecisionOrder,
        key=Layer[DecisionOrder](
            rdefault=DecisionOrder.ARRIVAL,
            parse=values.parse_decision_order,
            render=hooks.render_enum_value,
        ),
        help="whether an arrived listing may steer the decision order",
    )

    mode = Value[ModeFlag | None](
        mirrors=ResolveMode,
        key=Layer[ResolveMode](
            rdefault=ResolveMode.SPECIFIC,
            parse=values.parse_mode,
            render=hooks.render_enum_value,
        ),
        help="resolve for this environment or across a matrix",
        docs="explanation/universal.md",
    )

    constraints = Many[Requirement](
        key=Layer[tuple[str, ...]](
            rdefault=(),
            parse=values.parse_constraints,
            render=hooks.render_string_tuple,
            sample="attrs<24",
        ),
        help="bound a package's versions without pulling it into the resolve",
    )

    default_groups = Many[Group](
        key=Layer[tuple[str, ...]](
            rdefault=(),
            parse=values.parse_default_groups,
            render=hooks.render_string_tuple,
            sample="dev",
        ),
        help="a dependency group every resolve selects",
        docs="reference/selection.md",
    )

    base_group = Value[Group | None](
        key=Layer[str | None](
            rdefault=None,
            parse=values.parse_base_group,
            render=hooks.render_optional_text,
            sample="runtime",
        ),
        help="the group name the project's own dependencies lock under",
        docs="reference/selection.md",
    )

    build_group = Value[Group | None](
        key=Layer[str | None](
            rdefault=None,
            parse=values.parse_build_group,
            render=hooks.render_optional_text,
            sample="build",
        ),
        help="the group name [build-system].requires locks under",
        docs="reference/selection.md",
    )

    requires_python = Value[Specifier | None](
        key=Layer[str | None](
            rdefault=None,
            parse=values.parse_requires_python,
            render=hooks.render_optional_text,
            sample=">=3.11",
        ),
        help="the Python range the project supports, as a specifier",
    )

    uploaded_prior_to = Value[str | None](
        key=Layer[datetime | None](
            rdefault=None,
            parse=hooks.parse_uploaded_prior_to,
            render=hooks.render_uploaded_prior_to,
            sample="P7D",
            label="datetime|PnD",
        ),
        help="ignore distributions uploaded after this point",
    )

    dist_policy = Value[DistPolicyFlag | None](
        mirrors=DistPolicy,
        key=Layer[tuple[DistPolicy, bool]](
            rdefault=(DistPolicy.WHEEL_OR_SDIST, False),
            parse=values.parse_dist_policy,
            render=hooks.render_dist_policy,
        ),
        help="which distribution kinds the resolve may pin",
    )

    build_policy = Value[BuildPolicyFlag | None](
        mirrors=BuildPolicy,
        key=Layer[BuildPolicy](
            rdefault=BuildPolicy.BUILD_LOCAL,
            parse=values.parse_build_policy,
            render=hooks.render_enum_value,
        ),
        help="whether nab may build an sdist, and which ones",
        docs="reference/build-policy.md",
    )

    build_requires_depth = Value[int | None](
        key=Layer[int](
            rdefault=0,
            parse=values.parse_build_requires_depth,
            render=hooks.render_text,
            sample="1",
        ),
        help="how many build environments nab may open beneath the first",
        docs="reference/build-policy.md",
    )

    environment = Key(
        Layer[Mapping[str, Any]](
            rdefault=_EMPTY_MAPPING,
            parse=values.parse_environment,
            render=hooks.render_environment,
            label="table(python,platform[,knobs],implementation)",
        ),
        help="the target whose markers and wheel tags the resolve uses",
    )

    marker_environment = Key(
        Layer[Mapping[str, str]](
            rdefault=_EMPTY_MAPPING,
            parse=values.parse_marker_environment,
            render=hooks.render_marker_environment,
            label="table(marker-var=str)",
        ),
        deprecated=True,
        help="PEP 508 marker variables, set one at a time",
    )

    vcs = Key(
        Layer[VcsConfig](
            rdefault=VcsConfig(),
            parse=values.parse_vcs,
            render=hooks.render_vcs,
            label="table(vcs-policy)",
        ),
        help="whether a requirement may name a VCS URL, and which ones",
        docs="how-to/vcs.md",
    )

    workspace = Key(
        Layer[WorkspaceConfig | None](
            rdefault=None,
            parse=values.parse_workspace,
            render=hooks.render_workspace,
            label="table(members)",
        ),
        help="the member paths a workspace root declares",
        docs="how-to/workspaces.md",
    )

    indexes = Key(
        Layer[tuple[IndexConfig, ...]](
            rdefault=(IndexConfig(DEFAULT_INDEX_NAME, DEFAULT_INDEX_URL),),
            parse=values.parse_indexes,
            render=hooks.render_index_list,
            label="array-of-tables(name,url,serialization)",
        ),
        help="the package indexes, consulted in the order declared",
        docs="how-to/multi-index.md",
    )

    local_sources = Key(
        Layer[tuple[LocalSource, ...]](
            rdefault=(),
            parse=hooks.parse_local_sources,
            render=hooks.render_local_sources,
            label="array-of-tables(name,path)",
        ),
        help="a directory that becomes the only candidate for a named package",
        docs="how-to/local-sources.md",
    )

    vcs_sources = Key(
        Layer[tuple[VcsSource, ...]](
            rdefault=(),
            parse=values.parse_vcs_sources,
            render=hooks.render_vcs_sources,
            label="array-of-tables(name,url)",
        ),
        help="a repository that becomes the only candidate for a named package",
        docs="how-to/vcs.md",
    )

    archive_sources = Key(
        Layer[tuple[ArchiveSource, ...]](
            rdefault=(),
            parse=values.parse_archive_sources,
            render=hooks.render_archive_sources,
            label="array-of-tables(name,url)",
        ),
        help="a hashed .tar.gz URL a named package is pinned to",
    )

    packages = Key(
        Layer[tuple[Any, ...]](
            rdefault=(),
            parse=hooks.parse_packages,
            render=hooks.render_package_overrides,
            label="table(package-override)",
        ),
        help="policy and metadata overrides keyed by package name",
    )

    package_rules = Key(
        Layer[tuple[Any, ...]](
            rdefault=(),
            parse=hooks.parse_package_rules,
            render=hooks.render_package_overrides,
            label="array-of-tables(match,policy)",
        ),
        help="policy and metadata overrides selected by a list of requirements",
    )

    index = Key(
        Layer[Mapping[str, Any]](
            rdefault=_EMPTY_MAPPING,
            parse=hooks.parse_index_overrides,
            render=hooks.render_index_overrides,
            label="table(index-override)",
        ),
        help="policy overrides keyed by index name",
        docs="how-to/multi-index.md",
    )

    conflicts = Key(
        Layer[tuple[ConflictSet, ...]](
            rdefault=(),
            parse=values.parse_conflicts,
            render=hooks.render_conflicts,
            label="array-of-tables(members,policy)",
        ),
        help="sets of groups and extras that cannot be selected together",
        docs="explanation/conflicts.md",
    )

    matrix = Key(
        Layer[MatrixConfig | None](
            rdefault=None,
            parse=hooks.parse_matrix,
            render=hooks.render_matrix,
            label="table(python,platforms)",
        ),
        help="the Python and platform axes a universal resolve covers",
        docs="explanation/universal.md",
    )


class MatrixKeys(
    Table,
    on=_LAYERED,
    scope=Scope.PROJECT,
    under="matrix",
    needs=("python", "platforms"),
    docs="explanation/universal.md",
):
    """``[tool.nab.matrix]``'s five keys, as the flags that set them.

    These rows carry no configuration key of their own: ``under`` names the
    one they spell, and :mod:`nab.config.subflags` assembles what they read
    into the value ``matrix`` takes.  A key the command line leaves out keeps
    the file's; ``needs`` is the pair a command line has to give when no file
    declares the table.
    """

    python = Value[Specifier | None](
        help="the Python range a universal resolve covers, as a specifier",
    )

    platforms = Items[str](
        opened_by="id",
        help="the platforms to model: an id, then any KEY=VALUE tag knobs",
    )

    implementations = Star[str](
        help="the interpreter implementations to model",
    )

    python_order = Value[MatrixOrderFlag | None](
        help="the direction the python axis aligns across targets",
    )

    python_patches = Pairs[str](
        help="pin a Python minor to one patch release, as MINOR=FULL",
    )


class EnvironmentKeys(
    Table,
    on=_LAYERED,
    scope=Scope.PROJECT,
    under="environment",
    docs="reference/configuration.md",
):
    """``[tool.nab.environment]``'s three axes, as the flags that set them.

    No ``needs``: an axis the command line leaves out keeps the file's, and
    an axis no source sets is the host's.
    """

    python = Value[str | None](
        help="the Python version the resolve targets, as a version not a specifier",
    )

    platform = Item[str](
        opened_by="id",
        help="the machine to model: an id, then any KEY=VALUE tag knobs",
    )

    implementation = Value[ImplementationFlag | None](
        help="the interpreter implementation the resolve targets",
    )


class UserKeys(Table, on=_LAYERED, scope=Scope.USER, docs="reference/cli.md"):
    """The four settings a user sets once, each also a ``NAB_*`` variable."""

    offline = Tri(
        key=Layer[bool](
            rdefault=False, parse=values.parse_bool, render=hooks.render_bool
        ),
        env=True,
        help="never hit the network; resolve from the cache alone",
    )

    cache_dir = Value[Path | None](
        key=Layer[Path | None](
            rdefault=None,
            parse=values.parse_path,
            render=hooks.render_cache_dir,
            sample="nab-cache",
        ),
        env=True,
        help="the on-disk cache root",
        docs="reference/cache.md",
        on=_EVERY,
    )

    http_backend = Value[HttpBackend | None](
        key=Layer[str](
            rdefault="urllib3",
            parse=values.parse_http_backend,
            render=hooks.render_text,
            # The one written label: the flag offers the alias's order and
            # ``nab config explain`` prints them alphabetically.
            label="enum(httpx|urllib3)",
        ),
        env=True,
        help="the transport index and artefact fetches go through",
    )

    max_concurrency = Value[int | None](
        key=Layer[int](
            rdefault=8,
            parse=values.parse_max_concurrency,
            render=hooks.render_text,
            sample="4",
        ),
        env=True,
        help="how many fetches may be in flight at once",
        on=("download", "config"),
    )


class ConfigWords(Table, on=("config",), docs="reference/cli.md"):
    """``nab config``'s two words, and the project path lock and download read."""

    path = Operand[Path](
        default=Path("pyproject.toml"),
        help="the project file to resolve",
        on=_RUN,
    )

    action = Verb[Literal["list", "get", "explain"]](
        help="which configuration report to print",
    )

    key = Operand[str](
        default="",
        help="the option get and explain report on",
    )


class CacheWords(Table, on=("cache",), docs="reference/cache.md"):
    """``nab cache``'s word, plus ``nab config``'s path and ``nab lock``'s output.

    Those two are second declarations of names ``ConfigWords`` and
    ``RunFlags`` have already bound, and a table binds each name once.
    """

    action = Verb[Literal["dir", "verify", "clear"]](
        help="what to do with the cache",
    )

    path = Value[Path](
        default=Path("pyproject.toml"),
        help="the project file the configuration is read for",
        docs="reference/cli.md",
        on=("config",),
    )

    output = Value[Path | None](
        help=(
            "output path or -; defaults to pylock.toml or requirements.txt; "
            "universal requirements paths may use {python_version}, "
            "{platform_id}, and {selection}, but must render uniquely for every "
            "target; otherwise use pylock"
        ),
        docs="reference/formats.md",
        on=("lock",),
    )


class RunFlags(Table, on=_RUN, docs="reference/cli.md"):
    """The flags a resolve takes, where its result is written, and one more.

    ``include-rejected`` belongs to ``nab config`` and sits here because
    body order is help order and it prints after everything above it.
    """

    output = Value[Path](
        default=Path("wheels"),
        help="the directory the artefacts are written to",
        on=("download",),
    )

    format = Value[LockFormat](
        default="pylock",
        help=(
            "emit pylock, requirements with index-pin hash lines, or requirements "
            "without them"
        ),
        docs="reference/formats.md",
        on=("lock",),
    )

    cache = Switch(
        default=True,
        negatable=True,
        help="read and write the on-disk cache",
        docs="reference/cache.md",
    )

    python = Value[str | None](
        help="resolve for this Python version instead of the running one",
    )

    groups = Star[str](
        help="the dependency groups to select",
        docs="reference/selection.md",
    )

    all_groups = Switch(
        default=False,
        negatable=True,
        help="select every dependency group",
        docs="reference/selection.md",
    )

    extras = Star[str](
        help="the extras to select",
        docs="reference/selection.md",
    )

    all_extras = Switch(
        default=False,
        negatable=True,
        help="select every extra",
        docs="reference/selection.md",
    )

    workspace_discovery = Switch(
        default=True,
        negatable=True,
        help="resolve against the workspace members found in the tree",
        docs="how-to/workspaces.md",
    )

    build_requirements = Switch(
        default=False,
        negatable=True,
        help=(
            "lock [build-system].requires; defaults to pylock.build.toml or "
            "build-requirements.txt"
        ),
        docs="reference/selection.md",
        on=("lock",),
    )

    # This bool is not negatable because its name already states the
    # negation, so the table does not offer --no-no-emit-workspace.
    no_emit_workspace = Switch(
        default=False,
        help=(
            "omit workspace pins but keep members in resolution; install them "
            "outside a hashed-requirements run"
        ),
        docs="how-to/workspaces.md",
        on=("lock",),
    )

    upgrade = Switch(
        default=False,
        negatable=True,
        help="re-anchor a relative upload window to now",
        on=("lock",),
    )

    locked = Switch(
        default=False,
        negatable=True,
        help="check the committed lock is current and write nothing",
        on=("lock",),
    )

    include_rejected = Switch(
        default=False,
        negatable=True,
        help="report the sources a config error would have refused",
        on=("config",),
    )


TABLES = (
    Root,
    ProjectKeys,
    MatrixKeys,
    EnvironmentKeys,
    UserKeys,
    ConfigWords,
    CacheWords,
    RunFlags,
)

ALL: tuple[Opt, ...] = table_rows(*TABLES)
