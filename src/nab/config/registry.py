"""The layered options, and the value types the ladder carries them in.

:data:`OPTIONS` is the keyed half of :data:`nab.optiontable.ALL`: the rows a
configuration source may set, in the order ``nab config list`` prints them.
An option's key, scope, hooks, rung 0, ``NAB_*`` name and CLI flag are all
written there.  Every row here carries a key, and on such a row ``name`` is
that key.

:mod:`nab.config.layers` walks the rows to read a source, gate it and merge
the result, and :mod:`nab.config.inspect` walks them again to print it.

The value types the ladder passes around live here, next to the rows they
describe: where a value came from (:class:`Origin`), what one source bound
(:class:`Layer`), what it was refused for (:class:`RejectedLayer`), what won
(:class:`EffectiveValue`) and where discovery looks (:class:`SourceRoots`).
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING, Any

from nab_project.value import ValueType

from ..optiondefs import Opt, Scope
from ..optiontable import ALL

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

__all__ = [
    "BY_KEY",
    "OPTIONS",
    "PRECEDENCE",
    "EffectiveValue",
    "Layer",
    "Opt",
    "Origin",
    "RejectedLayer",
    "Scope",
    "SourceKind",
    "SourceRoots",
    "build_cli_overrides",
    "pyproject_registry_keys",
    "scope_label",
]


class SourceKind(enum.Enum):
    """One discoverable configuration source, low precedence to high.

    The two project-level TOML sources (``PYPROJECT`` and
    ``PROJECT_TOML``) share a precedence rank; the rest are totally
    ordered by :data:`PRECEDENCE`.
    """

    DEFAULT = "default"
    SYSTEM_TOML = "system"
    USER_TOML = "user"
    PYPROJECT = "pyproject"
    PROJECT_TOML = "project"
    ENV = "env"
    CLI = "cli"


# Precedence rank, low -> high.  PYPROJECT and PROJECT_TOML share rank 3:
# they are the same (project) precedence level, and on a tie PROJECT_TOML
# (the project-dir nab.toml) sorts last (wins).
PRECEDENCE: dict[SourceKind, int] = {
    SourceKind.DEFAULT: 0,
    SourceKind.SYSTEM_TOML: 1,
    SourceKind.USER_TOML: 2,
    SourceKind.PYPROJECT: 3,
    SourceKind.PROJECT_TOML: 3,
    SourceKind.ENV: 4,
    SourceKind.CLI: 5,
}

# The registry: every declared row a configuration source may set.  A root
# flag or a command-local row carries no key, so the filter drops it.
OPTIONS: tuple[Opt, ...] = tuple(row for row in ALL if row.key)

BY_KEY: dict[str, Opt] = {row.name: row for row in OPTIONS}


def pyproject_registry_keys() -> frozenset[str]:
    """Registry keys a pyproject ``[tool.nab]`` table may legitimately carry.

    Only PROJECT-scope registry options are allowed in pyproject; the
    single-environment parser (:mod:`nab.config.model`) folds this set
    into its own known-keys so a registry key is not double-reported as
    an unknown ``[tool.nab]`` key.
    """
    return frozenset(
        spec.name for spec in OPTIONS if spec.allowed_in_toml(SourceKind.PYPROJECT)
    )


class Origin(ValueType):
    """Where a value came from: a source kind plus a display label."""

    __slots__ = __match_args__ = ("kind", "label")

    kind: SourceKind
    label: str

    def __init__(self, kind: SourceKind, label: str) -> None:
        """Record the source a value came from."""
        self.kind = kind
        self.label = label

    @property
    def scope(self) -> str:
        """The provenance scope name shown by ``nab config``.

        Mirrors the source kind's value for every kind except
        ``PYPROJECT``, which reports "project" (it sits at the project
        precedence level alongside the project-dir nab.toml).
        """
        return scope_label(self.kind)

    def outranks(self, other: Origin) -> bool:
        """Whether this origin sits at a strictly higher precedence level.

        A tie is not an outranking: ``PYPROJECT`` and ``PROJECT_TOML``
        share a rank, so neither overrides the other here.
        """
        return PRECEDENCE[self.kind] > PRECEDENCE[other.kind]


def scope_label(kind: SourceKind) -> str:
    """Return the scope name ``nab config`` reports for a source kind.

    Distinct from Scope (PROJECT/USER, the gate axis): provenance reports
    the source, so a project nab.toml reports "project", env reports
    "env", etc.  Every kind reports its own value except PYPROJECT, which
    shares the project precedence level and so reports "project".
    """
    return "project" if kind is SourceKind.PYPROJECT else kind.value


class Layer(ValueType):
    """A set of (key -> value) bindings discovered from one source."""

    __slots__ = __match_args__ = ("origin", "values")

    origin: Origin
    values: Mapping[str, Any]

    def __init__(self, origin: Origin, values: Mapping[str, Any]) -> None:
        """Record the bindings ``origin`` supplied."""
        self.origin = origin
        self.values = values


class RejectedLayer(ValueType):
    """A source refused by the registry: a key outside its scope, or unknown.

    Captured (not raised) by :func:`discover_layers` for the TOML sources and
    :func:`read_env_layer` for the ``NAB_*`` ones, only when the caller asks
    to collect rejections for ``nab config --include-rejected``.  The normal
    load path raises :class:`SourceConfigError` for TOML and warns for env.
    """

    __slots__ = __match_args__ = ("origin", "key", "reason")

    origin: Origin
    key: str
    reason: str

    def __init__(self, origin: Origin, key: str, reason: str) -> None:
        """Record why ``origin``'s ``key`` was refused."""
        self.origin = origin
        self.key = key
        self.reason = reason


class EffectiveValue(ValueType):
    """One option's winning value plus its full shadowed stack."""

    __slots__ = __match_args__ = ("spec", "value", "origin", "stack", "rejected")

    spec: Opt
    value: Any
    origin: Origin
    # Every binding for this key in precedence order (low -> high),
    # the last of which is the winner.
    stack: tuple[tuple[Origin, Any], ...]
    rejected: tuple[RejectedLayer, ...]

    def __init__(
        self,
        spec: Opt,
        value: Any,
        origin: Origin,
        stack: tuple[tuple[Origin, Any], ...],
        rejected: tuple[RejectedLayer, ...] = (),
    ) -> None:
        """Record the value ``origin`` bound for ``spec``."""
        self.spec = spec
        self.value = value
        self.origin = origin
        self.stack = stack
        self.rejected = rejected


class SourceRoots(ValueType):
    """Injectable search roots so config discovery is hermetic in tests.

    ``system_toml`` and ``user_toml`` point at the system/user
    ``nab.toml`` files.  ``project_dir`` is the directory holding the
    pyproject; the project ``nab.toml`` is looked up beside it.
    ``pyproject`` names the pyproject file itself when the user pointed at
    a non-default name; left ``None`` it defaults to
    ``project_dir / "pyproject.toml"`` so the registry reads the same file
    the rest of nab does.  Any field may be ``None`` to skip that source.
    There is no walk-up: the project source is the directory of the
    pyproject only.
    """

    __slots__ = __match_args__ = (
        "system_toml",
        "user_toml",
        "project_dir",
        "pyproject",
    )

    system_toml: Path | None
    user_toml: Path | None
    project_dir: Path | None
    pyproject: Path | None

    def __init__(
        self,
        system_toml: Path | None = None,
        user_toml: Path | None = None,
        project_dir: Path | None = None,
        pyproject: Path | None = None,
    ) -> None:
        """Record the roots config discovery may read."""
        self.system_toml = system_toml
        self.user_toml = user_toml
        self.project_dir = project_dir
        self.pyproject = pyproject


def build_cli_overrides(locals_by_param: Mapping[str, Any]) -> dict[str, Any]:
    """Map ``{cli_param: value}`` to a registry-keyed override dict.

    Iterates :data:`OPTIONS`, reads each row's ``cli_param`` out of
    ``locals_by_param``, and keeps only the keys the user actually set.
    An unset scalar flag is ``None`` and an unset repeatable flag is an
    empty tuple (the append-action default); both are omitted so they do not
    shadow the file ladder.  A file-only row (``cli_param`` is ``None``)
    has no CLI flag, so it is skipped entirely.  Both the run subcommands
    and ``nab config`` build their override dict through this single
    helper, keyed off the registry rather than a per-option if-chain.
    """
    out: dict[str, Any] = {}
    for spec in OPTIONS:
        if spec.cli_param is None:
            continue
        value = locals_by_param[spec.cli_param]
        if value is None or (isinstance(value, tuple) and not value):
            continue
        out[spec.name] = value
    return out
