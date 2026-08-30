"""Every nab option, declared once.

Seven root rows, thirty-six behind a command's parameters, and thirteen
configuration keys with no flag.

A row is a call in a table body.  The class says how the parser reads it and
the type parameter says what its value is, so an option's kind, vtype,
choice set, nullability and printed label are read off the declaration
rather than written into it.  :mod:`nab.optionrows` is the vocabulary and
:mod:`nab.optionlower` turns each row into the :class:`nab.optiondefs.Opt`
the generator and the tests read.

The ten classes a row is written with, and how each is read:

- ``Count``: how many times the flag was written.
- ``Switch``: stores a constant.
- ``Eager``: acted on before the rest of the line is parsed.
- ``Tri``: a flag with a negation, absent until one of the two is written.
- ``Value``: reads one token.
- ``Many``: repeatable, and one occurrence contributes one value.
- ``Star``: takes every token up to the next flag.
- ``Operand``: a positional word.
- ``Verb``: a required positional word out of a fixed set.
- ``Key``: a configuration key with no command line at all.

A table's class keywords are its rows' defaults, and a row writes ``on=`` or
``docs=`` only where it differs.  Class order and body order are help order,
and that is what a table's name cannot always follow: ``path``, ``action``
and ``output`` are each declared by two commands while a table binds a name
once, and ``include-rejected`` prints last.  Those four sit in a table whose
command set they do not share, and each table's docstring names its own.

Adding an option is a row here, a parameter on the command function that
takes it, and ``python tasks/gen_bijection.py --write``.  A row with a
configuration key needs three more: an ``OptionSpec`` in
``nab_project.config_sources``, an entry in ``nab._run._cli_overrides``, and
the page its ``docs=`` names.  The censuses in ``tests/test_cli_table.py``,
``tests/test_cli_docs.py`` and ``tests/test_config_cmd.py`` are written out
rather than derived, so a new row moves those too.
"""

from __future__ import annotations

import types
from pathlib import Path
from typing import Any, Literal, NewType

from nab_provider.policy import (
    BuildPolicy,
    DecisionOrder,
    DistPolicy,
    ResolutionStrategy,
    ResolveMode,
)
from nab_provider.records import DEFAULT_INDEX_NAME, DEFAULT_INDEX_URL, IndexConfig
from nab_provider.vcs_admission import VcsConfig

from .flagtypes import (
    BuildPolicyFlag,
    DecisionOrderFlag,
    DistPolicyFlag,
    HttpBackend,
    LockFormat,
    ModeFlag,
    ResolutionFlag,
)
from .optiondefs import GLOBAL, Opt
from .optionlower import table_rows
from .optionrows import (
    Count,
    Eager,
    Key,
    Layer,
    Many,
    Operand,
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
        key=Layer(rdefault=ResolutionStrategy.HIGHEST),
        help="which version of each package to prefer",
    )

    decision_order = Value[DecisionOrderFlag | None](
        mirrors=DecisionOrder,
        key=Layer(rdefault=DecisionOrder.ARRIVAL),
        help="whether an arrived listing may steer the decision order",
    )

    mode = Value[ModeFlag | None](
        mirrors=ResolveMode,
        key=Layer(rdefault=ResolveMode.SPECIFIC),
        help="resolve for this environment or across a matrix",
        docs="explanation/universal.md",
    )

    constraints = Many[Requirement](
        key=Layer(
            rdefault=(),
            sample="attrs<24",
        ),
        help="bound a package's versions without pulling it into the resolve",
    )

    default_groups = Many[Group](
        key=Layer(
            rdefault=(),
            sample="dev",
        ),
        help="a dependency group every resolve selects",
        docs="reference/selection.md",
    )

    base_group = Value[Group | None](
        key=Layer(
            rdefault=None,
            sample="runtime",
        ),
        help="the group name the project's own dependencies lock under",
        docs="reference/selection.md",
    )

    build_group = Value[Group | None](
        key=Layer(
            rdefault=None,
            sample="build",
        ),
        help="the group name [build-system].requires locks under",
        docs="reference/selection.md",
    )

    requires_python = Value[Specifier | None](
        key=Layer(
            rdefault=None,
            sample=">=3.11",
        ),
        help="the Python range the project supports, as a specifier",
    )

    uploaded_prior_to = Value[str | None](
        key=Layer(
            rdefault=None,
            sample="P7D",
            label="datetime|PnD",
        ),
        help="ignore distributions uploaded after this point",
    )

    dist_policy = Value[DistPolicyFlag | None](
        mirrors=DistPolicy,
        key=Layer(rdefault=(DistPolicy.WHEEL_OR_SDIST, False)),
        help="which distribution kinds the resolve may pin",
        docs="reference/build-policy.md",
    )

    build_policy = Value[BuildPolicyFlag | None](
        mirrors=BuildPolicy,
        key=Layer(rdefault=BuildPolicy.BUILD_LOCAL),
        help="whether nab may build an sdist, and which ones",
        docs="reference/build-policy.md",
    )

    build_requires_depth = Value[int | None](
        key=Layer(
            rdefault=0,
            sample="1",
        ),
        help="how many build environments nab may open beneath the first",
        docs="reference/build-policy.md",
    )

    environment = Key(
        Layer(
            rdefault=_EMPTY_MAPPING,
            label="table(python,platform[,knobs],implementation)",
        ),
        help="the target whose markers and wheel tags the resolve uses",
    )

    marker_environment = Key(
        Layer(
            rdefault=_EMPTY_MAPPING,
            label="table(marker-var=str)",
        ),
        deprecated=True,
        help="PEP 508 marker variables, set one at a time",
    )

    vcs = Key(
        Layer(
            rdefault=VcsConfig(),
            label="table(vcs-policy)",
        ),
        help="whether a requirement may name a VCS URL, and which ones",
        docs="how-to/vcs.md",
    )

    workspace = Key(
        Layer(
            rdefault=None,
            label="table(members)",
        ),
        help="the member paths a workspace root declares",
        docs="how-to/workspaces.md",
    )

    indexes = Key(
        Layer(
            rdefault=(IndexConfig(DEFAULT_INDEX_NAME, DEFAULT_INDEX_URL),),
            label="array-of-tables(name,url,serialization)",
        ),
        help="the package indexes, consulted in the order declared",
        docs="how-to/multi-index.md",
    )

    local_sources = Key(
        Layer(
            rdefault=(),
            label="array-of-tables(name,path)",
        ),
        help="a directory that becomes the only candidate for a named package",
        docs="how-to/local-sources.md",
    )

    vcs_sources = Key(
        Layer(
            rdefault=(),
            label="array-of-tables(name,url)",
        ),
        help="a repository that becomes the only candidate for a named package",
        docs="how-to/vcs.md",
    )

    archive_sources = Key(
        Layer(
            rdefault=(),
            label="array-of-tables(name,url)",
        ),
        help="a hashed .tar.gz URL a named package is pinned to",
    )

    packages = Key(
        Layer(
            rdefault=(),
            label="table(package-override)",
        ),
        help="policy and metadata overrides keyed by package name",
    )

    package_rules = Key(
        Layer(
            rdefault=(),
            label="array-of-tables(match,policy)",
        ),
        help="policy and metadata overrides selected by a list of requirements",
    )

    index = Key(
        Layer(
            rdefault=_EMPTY_MAPPING,
            label="table(index-override)",
        ),
        help="policy overrides keyed by index name",
        docs="how-to/multi-index.md",
    )

    conflicts = Key(
        Layer(
            rdefault=(),
            label="array-of-tables(members,policy)",
        ),
        help="sets of groups and extras that cannot be selected together",
        docs="explanation/conflicts.md",
    )

    matrix = Key(
        Layer(
            rdefault=None,
            label="table(python,platforms)",
        ),
        help="the Python and platform axes a universal resolve covers",
        docs="explanation/universal.md",
    )


class UserKeys(Table, on=_LAYERED, scope=Scope.USER, docs="reference/cli.md"):
    """The four settings a user sets once, each also a ``NAB_*`` variable."""

    offline = Tri(
        key=Layer(rdefault=False),
        env=True,
        help="never hit the network; resolve from the cache alone",
    )

    cache_dir = Value[Path | None](
        key=Layer(
            rdefault=None,
            sample="nab-cache",
        ),
        env=True,
        help="the on-disk cache root",
        docs="reference/cache.md",
        on=_EVERY,
    )

    http_backend = Value[HttpBackend | None](
        key=Layer(
            rdefault="urllib3",
            # The one written label: the flag offers the alias's order and
            # ``nab config explain`` prints them alphabetically.
            label="enum(httpx|urllib3)",
        ),
        env=True,
        help="the transport index and artefact fetches go through",
    )

    max_concurrency = Value[int | None](
        key=Layer(
            rdefault=8,
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
        help="where to write the lock; - writes it to stdout",
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
        help="the lockfile format to emit",
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
        help="lock [build-system].requires instead of the dependencies",
        docs="reference/selection.md",
        on=("lock",),
    )

    # The one bool flag that is not negatable: its name already spells the
    # negation, so --no-no-emit-workspace is not a spelling the table offers.
    no_emit_workspace = Switch(
        default=False,
        help="leave the workspace member pins out of the lockfile",
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
    UserKeys,
    ConfigWords,
    CacheWords,
    RunFlags,
)

ALL: tuple[Opt, ...] = table_rows(*TABLES)
