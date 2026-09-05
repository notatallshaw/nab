"""Resolve version constraints independently for each candidate source."""

from __future__ import annotations

from enum import Enum
from types import MemberDescriptorType
from typing import TYPE_CHECKING, Any, ClassVar

from nab_provider._vendor.packaging.ranges import VersionRange

from ._compat import override

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from nab_provider._vendor.packaging.version import Version

__all__ = ["CandidateKey", "CandidateRange"]

_CANDIDATE_VERSIONS = VersionRange.full(admit_arbitrary=False)


class _ImmutableSlots:
    """Keep fields used in candidate and range hashes unchanged."""

    __slots__ = ("__weakref__",)
    _immutable_fields: ClassVar[tuple[str, ...]]

    @override
    def __setattr__(self, name: str, value: object) -> None:
        if "_immutable_fields" in type(self).__dict__ or name in self._immutable_fields:
            message = f"cannot assign to field {name!r}"
            raise AttributeError(message)
        object.__setattr__(self, name, value)

    @override
    def __delattr__(self, name: str) -> None:
        if "_immutable_fields" in type(self).__dict__ or name in self._immutable_fields:
            message = f"cannot delete field {name!r}"
            raise AttributeError(message)
        object.__delattr__(self, name)

    def __getstate__(
        self,
    ) -> dict[str, Any] | tuple[dict[str, Any], dict[str, Any]]:
        """Collect fields and subclass state without copying contained values."""
        dictionary = getattr(self, "__dict__", {}).copy()
        slots: dict[str, Any] = {}
        seen = set()
        for cls in type(self).__mro__:
            for name, descriptor in cls.__dict__.items():
                if not isinstance(descriptor, MemberDescriptorType) or name in seen:
                    continue
                try:
                    value = descriptor.__get__(self)
                except AttributeError:
                    continue
                seen.add(name)
                if name in self._immutable_fields:
                    dictionary[name] = value
                else:
                    slots[name] = value
        return (dictionary, slots) if slots else dictionary

    def __setstate__(
        self, state: dict[str, Any] | tuple[dict[str, Any], dict[str, Any]]
    ) -> None:
        """Restore fields and subclass state without running their constructors."""
        dictionary, slots = state if isinstance(state, tuple) else (state, {})
        extra = dictionary.copy()
        for name in self._immutable_fields:
            if name in extra and name not in slots:
                descriptor = _attribute_slot(type(self), name)
                if descriptor is not None:
                    descriptor.__set__(self, extra.pop(name))
        if extra:
            self.__dict__.update(extra)
        for name, value in slots.items():
            setattr(self, name, value)


def _attribute_slot(cls: type, name: str) -> MemberDescriptorType | None:
    """Find the visible slot without invoking a subclass property."""
    descriptor = next(
        base.__dict__[name] for base in cls.__mro__ if name in base.__dict__
    )
    return descriptor if isinstance(descriptor, MemberDescriptorType) else None


def _slot_writer(cls: type, name: str) -> Callable[[object, object], None]:
    """Bind a slot setter for initialization without writable attributes."""
    slot: MemberDescriptorType = cls.__dict__[name]
    return slot.__set__


class CandidateKey(_ImmutableSlots):
    """Identify a distribution by its version and installer source."""

    __slots__ = __match_args__ = ("version", "source")
    _immutable_fields: ClassVar[tuple[str, ...]] = __slots__

    version: Version
    source: str

    def __init__(self, version: Version, source: str) -> None:
        """Retain the version and its host-defined source."""
        if type(self) is CandidateKey:
            _set_key_version(self, version)
            _set_key_source(self, source)
        else:
            object.__setattr__(self, "version", version)
            object.__setattr__(self, "source", source)

    @override
    def __eq__(self, other: object) -> bool:
        """Compare version and source within the exact class."""
        if not isinstance(other, CandidateKey) or other.__class__ is not self.__class__:
            return NotImplemented
        return (self.version, self.source) == (other.version, other.source)

    def __lt__(self, other: object) -> bool:
        """Compare versions before source identifiers."""
        if not isinstance(other, CandidateKey) or other.__class__ is not self.__class__:
            return NotImplemented
        return (self.version, self.source) < (other.version, other.source)

    def __le__(self, other: object) -> bool:
        """Compare versions before source identifiers."""
        if not isinstance(other, CandidateKey) or other.__class__ is not self.__class__:
            return NotImplemented
        return (self.version, self.source) <= (other.version, other.source)

    def __gt__(self, other: object) -> bool:
        """Compare versions before source identifiers."""
        if not isinstance(other, CandidateKey) or other.__class__ is not self.__class__:
            return NotImplemented
        return (self.version, self.source) > (other.version, other.source)

    def __ge__(self, other: object) -> bool:
        """Compare versions before source identifiers."""
        if not isinstance(other, CandidateKey) or other.__class__ is not self.__class__:
            return NotImplemented
        return (self.version, self.source) >= (other.version, other.source)

    @override
    def __hash__(self) -> int:
        """Hash version and source together."""
        return hash((self.version, self.source))

    @override
    def __repr__(self) -> str:
        """Render the class name and its identity fields."""
        return (
            f"{type(self).__qualname__}(version={self.version!r}, "
            f"source={self.source!r})"
        )

    @override
    def __str__(self) -> str:
        """Render the PEP 440 version for host diagnostics."""
        return str(self.version)


_set_key_version = _slot_writer(CandidateKey, "version")
_set_key_source = _slot_writer(CandidateKey, "source")


class _RangeRelation(Enum):
    EMPTY = (True, True)
    SUBSET = (True, False)
    DISJOINT = (False, True)
    OVERLAPPING = (False, False)

    @property
    def is_subset(self) -> bool:
        """Whether every admitted candidate is also in the other constraint."""
        return self.value[0]

    @property
    def is_disjoint(self) -> bool:
        """Whether the constraints admit no shared candidate."""
        return self.value[1]


class CandidateRange(_ImmutableSlots):
    """An immutable default version range with finite source-specific replacements."""

    __slots__ = __match_args__ = ("default", "overrides", "_hash")
    _immutable_fields: ClassVar[tuple[str, ...]] = __slots__

    default: VersionRange
    overrides: tuple[tuple[str, VersionRange], ...]
    _hash: int

    def __init__(
        self,
        default: VersionRange,
        overrides: Mapping[str, VersionRange] | Iterable[tuple[str, VersionRange]] = (),
    ) -> None:
        """Snapshot source bounds with the default prerelease configuration."""
        default = default & _CANDIDATE_VERSIONS
        sources = {
            source: value & _CANDIDATE_VERSIONS
            for source, value in dict(overrides).items()
        }
        normalized = tuple(
            sorted(
                (source, value) for source, value in sources.items() if value != default
            )
        )
        if type(self) is CandidateRange:
            _set_range_default(self, default)
            _set_range_overrides(self, normalized)
            _set_range_hash(self, hash((default, normalized)))
        else:
            object.__setattr__(self, "default", default)
            object.__setattr__(self, "overrides", normalized)
            object.__setattr__(self, "_hash", hash((default, normalized)))

    @classmethod
    def empty(cls) -> CandidateRange:
        """Return a constraint admitting no candidate."""
        return cls(VersionRange.empty())

    @classmethod
    def full(cls) -> CandidateRange:
        """Return every PEP 440 version on every source."""
        return cls(_CANDIDATE_VERSIONS)

    @classmethod
    def singleton(cls, candidate: CandidateKey) -> CandidateRange:
        """Return a constraint admitting this candidate identity alone."""
        return cls.for_source(
            candidate.source, VersionRange.singleton(candidate.version)
        )

    @classmethod
    def for_source(
        cls, source: str, versions: VersionRange | None = None
    ) -> CandidateRange:
        """Restrict the given source and exclude every other source."""
        if versions is None:
            versions = _CANDIDATE_VERSIONS
        return cls(VersionRange.empty(), [(source, versions)])

    def for_versions(self, source: str) -> VersionRange:
        """Return the version constraint applying to one source."""
        for key, value in self.overrides:
            if key == source:
                return value
        return self.default

    @property
    def is_empty(self) -> bool:
        """Whether no version is admitted on any source."""
        return self.default.is_empty and all(
            versions.is_empty for _, versions in self.overrides
        )

    def __contains__(self, candidate: CandidateKey) -> bool:
        """Check the version bounds on the candidate's source."""
        return candidate.version in self.for_versions(candidate.source)

    def _combine(
        self,
        other: CandidateRange,
        operation: Callable[[VersionRange, VersionRange], VersionRange],
    ) -> CandidateRange:
        """Apply one range operation to every affected source coordinate."""
        default = operation(self.default, other.default)
        sources = {source for source, _ in self.overrides + other.overrides}
        return CandidateRange(
            default,
            (
                (
                    source,
                    operation(self.for_versions(source), other.for_versions(source)),
                )
                for source in sources
            ),
        )

    def __and__(self, other: object) -> CandidateRange:
        """Intersect each source coordinate."""
        if not isinstance(other, CandidateRange):
            return NotImplemented
        return self._combine(other, VersionRange.__and__)

    def __or__(self, other: object) -> CandidateRange:
        """Union each source coordinate."""
        if not isinstance(other, CandidateRange):
            return NotImplemented
        return self._combine(other, VersionRange.__or__)

    def __sub__(self, other: object) -> CandidateRange:
        """Remove the other constraint on each source coordinate."""
        if not isinstance(other, CandidateRange):
            return NotImplemented
        return self._combine(other, VersionRange.__sub__)

    def __invert__(self) -> CandidateRange:
        """Complement the bounds independently on each source."""
        return CandidateRange(
            ~self.default, ((source, ~value) for source, value in self.overrides)
        )

    def is_subset(self, other: CandidateRange) -> bool:
        """Whether every admitted candidate is also in the other constraint."""
        return (self - other).is_empty

    def is_superset(self, other: CandidateRange) -> bool:
        """Whether every candidate in the other constraint is admitted here."""
        return other.is_subset(self)

    def is_disjoint(self, other: CandidateRange) -> bool:
        """Whether the constraints admit no shared candidate."""
        return (self & other).is_empty

    def relation(self, other: CandidateRange) -> _RangeRelation:
        """Return the subset and disjoint range flags the resolver reads."""
        return _RangeRelation((self.is_subset(other), self.is_disjoint(other)))

    @override
    def __eq__(self, other: object) -> bool:
        """Compare normalized source coordinates and admission policy."""
        if not isinstance(other, CandidateRange):
            return NotImplemented
        return self.default == other.default and self.overrides == other.overrides

    @override
    def __hash__(self) -> int:
        """Return the hash fixed when the constraint was constructed."""
        return self._hash

    @override
    def __str__(self) -> str:
        """Render the version bounds and exceptional source coordinates."""
        return f"{self.default!r}; sources={self.overrides!r}"

    @override
    def __repr__(self) -> str:
        """Render the normalized fields and cached hash."""
        return (
            f"{type(self).__qualname__}(default={self.default!r}, "
            f"overrides={self.overrides!r}, _hash={self._hash!r})"
        )


_set_range_default = _slot_writer(CandidateRange, "default")
_set_range_overrides = _slot_writer(CandidateRange, "overrides")
_set_range_hash = _slot_writer(CandidateRange, "_hash")
