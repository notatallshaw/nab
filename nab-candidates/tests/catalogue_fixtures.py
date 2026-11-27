"""Small host catalogue of ordered integer releases and immutable prepared facts."""

from __future__ import annotations

from dataclasses import dataclass

from nab_resolver.ranges import Range


@dataclass(frozen=True, slots=True)
class Choice:
    """One immutable package/version/file-group identity."""

    package: str
    version: int
    withdrawn: bool = False

    @property
    def base(self) -> str:
        return self.package.partition("[")[0]

    @property
    def label(self) -> str:
        return str(self.version)


@dataclass(frozen=True, slots=True)
class Pin:
    package: str
    version: int


@dataclass(slots=True)
class Data:
    dependencies: dict[str, Range[int]]
    pins: tuple[Pin, ...] = ()


class Catalogue:
    """A fixed integer domain with exact-pin admission and cached dependency facts."""

    def __init__(self, choices: tuple[Choice, ...]) -> None:
        self.items = choices
        self.facts: dict[Choice, Data | None] = {}
        self.missing_listings: set[str] = set()
        self.unavailable: set[Choice] = set()
        self.unavailable_reason = "metadata is unavailable"
        self.metadata: dict[Choice, Data | None] = {}

    def prepare(self, candidate: Choice) -> Data | None:
        """Cache the declared metadata when the shared provider grants admission."""
        if candidate in self.unavailable:
            return None
        if candidate not in self.facts:
            self.facts[candidate] = self.metadata.get(candidate, Data({}))
        return self.facts[candidate]

    def choices(self, package: str) -> tuple[Choice, ...]:
        return tuple(
            candidate for candidate in self.items if candidate.package == package
        )

    def ordered(self, package: str, allowed: Range[int]) -> list[Choice]:
        return [
            candidate
            for candidate in self.choices(package)
            if candidate.version in allowed
        ]

    @staticmethod
    def candidate_package(candidate: Choice) -> str:
        return candidate.package

    @staticmethod
    def candidate_base(candidate: Choice) -> str:
        return candidate.base

    @staticmethod
    def candidate_label(candidate: Choice) -> str:
        return candidate.label

    @staticmethod
    def candidate_withdrawn(candidate: Choice) -> bool:
        return candidate.withdrawn

    @staticmethod
    def full_range() -> Range[int]:
        return Range[int].full()

    @staticmethod
    def intersect_ranges(left: Range[int], right: Range[int]) -> Range[int]:
        return left & right

    @staticmethod
    def range_contains(candidate: Choice, allowed: Range[int]) -> bool:
        return candidate.version in allowed

    @staticmethod
    def pin_allows(pin: Pin, candidate: Choice) -> bool:
        return pin.package == candidate.base and pin.version == candidate.version

    @staticmethod
    def pin_project(pin: Pin) -> str:
        return pin.package

    @staticmethod
    def base_name(package: str) -> str:
        return package.partition("[")[0]

    @staticmethod
    def base_candidate(candidate: Choice) -> Choice:
        return Choice(candidate.base, candidate.version, candidate.withdrawn)
