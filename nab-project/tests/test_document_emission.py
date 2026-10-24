"""Keep direct index emission equivalent to native models and schema validation."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone, tzinfo
from typing import TYPE_CHECKING

import pytest
import tomli_w

from nab_project._lockfile import pylock
from nab_project._lockfile.coverage import CoverageError
from nab_project.lockfile import (
    ArchivePin,
    IndexPin,
    LocalPin,
    LockInput,
    LockValidationError,
    PinShape,
    Provenance,
    SdistArtifact,
    TargetLock,
    VcsPin,
    WheelArtifact,
)
from nab_provider._vendor.packaging.markers import Marker
from nab_provider._vendor.packaging.pylock import Package, PackageWheel, Pylock
from nab_provider._vendor.packaging.specifiers import (
    InvalidSpecifier,
    Specifier,
    SpecifierSet,
)
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget, environment_declaration
from nab_provider.value import replace

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path
    from types import FrameType
    from typing import SupportsIndex

    from typing_extensions import Self


_HASHES = (("sha512", "b" * 128), ("sha256", "a" * 64))


def index_pin(*, size: int = 0) -> IndexPin:
    wheel = WheelArtifact(
        "demo-1.0-py3-none-any.whl",
        "https://example.com/demo-1.0-py3-none-any.whl",
        _HASHES,
        size=size,
    )
    sdist = SdistArtifact(
        "demo-1.0.tar.gz", "https://example.com/demo-1.0.tar.gz", _HASHES, size=0
    )
    return IndexPin(
        "demo",
        "1.0",
        "https://example.invalid/simple/",
        sdist=sdist,
        wheels=(wheel,),
        requires_python=">=3.10",
    )


def lock_input(pin: PinShape) -> LockInput:
    """Put a supported pin in an actual declared target and provenance context."""
    target = ResolveTarget.for_declared(
        python_version="3.12", spec=PlatformSpec("linux_x86_64")
    )
    return LockInput(
        targets={target.label: TargetLock(target=target, pins={pin.name: pin})},
        environments=[Marker(environment_declaration(target, []))],
        requires_python=">=3.10",
        extras=("my_extra", "my-extra"),
        dependency_groups=("test",),
        default_groups=("test",),
        provenance=Provenance(
            "0.0.0",
            datetime(2026, 10, 23, tzinfo=timezone.utc),
            ("nab", "lock"),
            "pyproject.toml",
            "specific",
        ),
    )


def observe_render(value: LockInput, *, lock_dir: Path) -> tuple[str, list[str]]:
    """Observe real policy, serialization and validator calls without replacing them."""
    codes = {
        pylock.validate_marker_disjointness.__code__: "disjointness",
        pylock.validate_marker_coverage.__code__: "coverage",
        pylock._index_document.__code__: "document",
        Pylock.to_dict.__code__: "native-document",
        Pylock.from_dict.__func__.__code__: "schema",
    }
    calls: list[str] = []

    def observed(frame: FrameType, event: str, _value: object) -> None:
        if event == "call" and (label := codes.get(frame.f_code)) is not None:
            calls.append(label)

    previous = sys.getprofile()
    sys.setprofile(observed)
    try:
        text = pylock.render_lock(value, lock_dir=lock_dir)
    finally:
        sys.setprofile(previous)
    return text, calls


@pytest.mark.parametrize("artifacts", ["remote", "local"])
def test_index_document_matches_native_values_types_order_and_text(
    tmp_path: Path, artifacts: str
) -> None:
    pin = index_pin()
    if artifacts == "local":
        assert pin.sdist is not None
        wheel = pin.wheels[0]
        sdist = pin.sdist
        pin = pin.replace(
            wheels=(
                WheelArtifact(
                    wheel.filename,
                    wheel.url,
                    wheel.hashes,
                    size=wheel.size,
                    local_path=tmp_path / "files" / wheel.filename,
                ),
            ),
            sdist=SdistArtifact(
                sdist.filename,
                sdist.url,
                sdist.hashes,
                size=sdist.size,
                local_path=tmp_path / "files" / sdist.filename,
            ),
        )
    value = lock_input(pin)
    expected = pylock.build_pylock(value, lock_dir=tmp_path).to_dict()
    fields, packages = pylock._prepare_pylock(
        value, lock_dir=tmp_path, package_output=pylock._pin_to_document
    )
    document = pylock._index_document(fields, packages)

    assert document == expected
    assert list(document) == list(expected)

    native_packages = expected["packages"]
    direct_packages = document["packages"]
    assert isinstance(direct_packages, list)
    for actual, native in zip(direct_packages, native_packages, strict=True):
        assert list(actual) == list(native)
        assert isinstance(actual["wheels"], tuple)
        assert list(actual["wheels"][0]) == list(native["wheels"][0])
        assert list(actual["wheels"][0]["hashes"]) == ["sha256", "sha512"]
        assert actual["wheels"][0]["size"] == 0

    assert isinstance(document["extras"], tuple)
    assert isinstance(document["dependency-groups"], tuple)
    assert isinstance(document["environments"], list)

    text, calls = observe_render(value, lock_dir=tmp_path)

    assert text == tomli_w.dumps(expected)
    assert calls == ["disjointness", "coverage", "document", "schema"]


def test_public_builder_still_returns_native_unvalidated_models(tmp_path: Path) -> None:
    value = lock_input(index_pin(size=False))
    model = pylock.build_pylock(value, lock_dir=tmp_path)

    assert isinstance(model, Pylock)
    assert isinstance(model.packages[0], Package)
    assert model.packages[0].wheels is not None
    wheel = model.packages[0].wheels[0]

    assert isinstance(wheel, PackageWheel)
    assert wheel.size is False

    with pytest.raises(LockValidationError, match="bool"):
        pylock.render_lock(value, lock_dir=tmp_path)


@pytest.mark.parametrize("source", ["local", "vcs", "archive"])
def test_other_source_kinds_keep_the_native_serializer(
    tmp_path: Path, source: str
) -> None:
    pin: PinShape
    if source == "local":
        pin = LocalPin("demo", "1.0", str(tmp_path / "project"))
    elif source == "vcs":
        pin = VcsPin(
            name="demo",
            version="1.0",
            repo_url="https://example.invalid/demo.git",
            bare_repo_url="https://example.invalid/demo.git",
            commit_id="a" * 40,
        )
    else:
        pin = ArchivePin(
            name="demo",
            version="1.0",
            url="https://example.invalid/demo-1.0.tar.gz",
            hashes=_HASHES,
        )
    value = lock_input(pin)
    expected = pylock.build_pylock(value, lock_dir=tmp_path).to_dict()
    text, calls = observe_render(value, lock_dir=tmp_path)

    assert text == tomli_w.dumps(expected)
    assert calls == ["disjointness", "coverage", "native-document", "schema"]


def test_mixed_sources_keep_index_artifacts_on_the_native_path(tmp_path: Path) -> None:
    value = lock_input(index_pin())
    label, lock = next(iter(value.targets.items()))
    value = replace(
        value,
        targets={
            label: replace(
                lock,
                pins={
                    "demo": index_pin(),
                    "local": LocalPin("local", "1.0", str(tmp_path / "project")),
                },
            )
        },
    )
    expected = pylock.build_pylock(value, lock_dir=tmp_path).to_dict()
    text, calls = observe_render(value, lock_dir=tmp_path)

    assert text == tomli_w.dumps(expected)
    assert calls == ["disjointness", "coverage", "native-document", "schema"]


def test_index_output_refuses_a_non_index_pin(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="Index document"):
        pylock._pin_to_document(
            LocalPin("demo", "1.0", str(tmp_path)), lock_dir=tmp_path
        )


def test_artifact_checks_precede_coverage_failure(tmp_path: Path) -> None:
    value = replace(
        lock_input(index_pin(size=-1)),
        environments=[Marker('python_version == "3.11"')],
    )
    with pytest.raises(LockValidationError, match="size"):
        pylock.render_lock(value, lock_dir=tmp_path)


def test_coverage_failure_precedes_root_specifier_parsing(tmp_path: Path) -> None:
    value = replace(
        lock_input(index_pin()),
        environments=[Marker('python_version == "3.11"')],
        requires_python="not a specifier",
    )
    with pytest.raises(CoverageError):
        pylock.render_lock(value, lock_dir=tmp_path)


def test_root_specifier_parsing_precedes_schema_validation(tmp_path: Path) -> None:
    value = replace(
        lock_input(index_pin(size=False)), requires_python="not a specifier"
    )
    with pytest.raises(InvalidSpecifier):
        pylock.render_lock(value, lock_dir=tmp_path)


def test_package_specifier_stringification_follows_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    serialized: list[str] = []

    class ObservedSpecifier(SpecifierSet):
        def __str__(self) -> str:
            serialized.append("specifier")
            return super().__str__()

    monkeypatch.setattr(pylock, "SpecifierSet", ObservedSpecifier)
    value = replace(
        lock_input(index_pin()),
        environments=[Marker('python_version == "3.11"')],
        requires_python=None,
    )
    with pytest.raises(CoverageError):
        pylock.render_lock(value, lock_dir=tmp_path)

    assert serialized == []

    text, calls = observe_render(
        replace(value, environments=lock_input(index_pin()).environments),
        lock_dir=tmp_path,
    )

    assert text
    assert serialized == ["specifier"]
    assert calls == ["disjointness", "coverage", "document", "schema"]


def test_document_collections_do_not_alias_other_emissions(tmp_path: Path) -> None:
    value = lock_input(index_pin())
    root, packages = pylock._prepare_pylock(
        value, lock_dir=tmp_path, package_output=pylock._pin_to_document
    )
    document = pylock._index_document(root, packages)
    emitted = document["packages"]
    assert isinstance(emitted, list)
    emitted[0]["wheels"][0]["hashes"]["sha256"] = "c" * 64
    tool = document["tool"]
    assert isinstance(tool, dict)
    tool["nab"]["command-line"].append("changed")

    fresh = pylock.render_lock(value, lock_dir=tmp_path)

    assert "c" * 64 not in fresh
    assert "changed" not in fresh
    assert value.provenance is not None
    assert value.provenance.command_line == ("nab", "lock")
    assert value.targets[next(iter(value.targets))].pins["demo"] == index_pin()


class CopyText(str):
    """Expose the public serializer's scalar-copy behavior."""

    __slots__ = ("replacement",)

    replacement: str

    def __new__(cls, value: str, replacement: str) -> Self:
        result = super().__new__(cls, value)
        result.replacement = replacement
        return result

    def __deepcopy__(self, _memo: dict[int, object]) -> str:
        """Substitute the text only when native serialization copies it."""
        return self.replacement


class BlockProvenance(Provenance):
    """Provide supported opaque tool values through the public provenance API."""

    __slots__ = ("block",)

    def __init__(self, block: dict[str, object]) -> None:
        super().__init__(
            "0.0.0",
            datetime(2026, 10, 23, tzinfo=timezone.utc),
            (),
            "pyproject.toml",
            "specific",
        )
        self.block = block

    def to_block(self) -> dict[str, object]:
        return self.block


@pytest.mark.parametrize("field", ["index", "url", "hash", "provenance"])
def test_custom_scalar_copy_uses_native_serialization_once(
    tmp_path: Path, field: str
) -> None:
    pin = index_pin()
    if field == "index":
        pin = pin.replace(
            index=CopyText("https://original.invalid/", "https://copied.invalid/")
        )
    elif field in {"url", "hash"}:
        wheel = pin.wheels[0]
        pin = pin.replace(
            wheels=(
                WheelArtifact(
                    wheel.filename,
                    CopyText(wheel.url, "https://copied.invalid/demo.whl")
                    if field == "url"
                    else wheel.url,
                    (("sha256", CopyText("a" * 64, "c" * 64)),)
                    if field == "hash"
                    else wheel.hashes,
                    size=wheel.size,
                ),
            )
        )
    value = lock_input(pin)
    if field == "provenance":
        assert value.provenance is not None
        value.provenance.nab_version = CopyText("original", "copied")
    expected = tomli_w.dumps(pylock.build_pylock(value, lock_dir=tmp_path).to_dict())

    text, calls = observe_render(value, lock_dir=tmp_path)

    assert text == expected
    assert ("copied" if field != "hash" else "c" * 64) in text
    assert calls == [
        "disjointness",
        "coverage",
        "document",
        "native-document",
        "schema",
    ]


def test_marker_subclass_in_tool_values_keeps_native_copy(tmp_path: Path) -> None:
    class CopiedMarker(Marker):
        def __deepcopy__(self, _memo: dict[int, object]) -> str:
            return "copied-marker"

    value = replace(
        lock_input(index_pin()),
        provenance=BlockProvenance(
            {"marker": CopiedMarker('python_version == "3.12"')}
        ),
    )
    expected = tomli_w.dumps(pylock.build_pylock(value, lock_dir=tmp_path).to_dict())
    text, calls = observe_render(value, lock_dir=tmp_path)

    assert text == expected
    assert "copied-marker" in text
    assert calls == [
        "disjointness",
        "coverage",
        "document",
        "native-document",
        "schema",
    ]


def test_custom_copy_failure_preserves_policy_and_native_error_order(
    tmp_path: Path,
) -> None:
    class CopyFailure(str):
        __slots__ = ()

        def __deepcopy__(self, _memo: dict[int, object]) -> str:
            raise RuntimeError("index copy failed")

    pin = index_pin().replace(index=CopyFailure("https://example.invalid/"))
    value = replace(lock_input(pin), environments=[Marker('python_version == "3.11"')])
    with pytest.raises(CoverageError):
        pylock.render_lock(value, lock_dir=tmp_path)

    # Native traversal reaches the package index before the provenance cycle.
    cycle: list[object] = []
    cycle.append(cycle)

    value = replace(
        value,
        environments=lock_input(index_pin()).environments,
        provenance=BlockProvenance({"cycle": cycle}),
    )
    with pytest.raises(RuntimeError, match="index copy failed"):
        pylock.build_pylock(value, lock_dir=tmp_path).to_dict()
    with pytest.raises(RuntimeError, match="index copy failed"):
        pylock.render_lock(value, lock_dir=tmp_path)


def test_custom_tool_container_uses_native_reconstruction(tmp_path: Path) -> None:
    class AppendingList(list[str]):
        def __init__(self, values: Iterable[str]) -> None:
            super().__init__(values)
            self.append("copied")

    value = replace(
        lock_input(index_pin()),
        provenance=BlockProvenance({"values": AppendingList(["original"])}),
    )
    expected = tomli_w.dumps(pylock.build_pylock(value, lock_dir=tmp_path).to_dict())
    text, calls = observe_render(value, lock_dir=tmp_path)

    assert text == expected
    assert text.count("copied") == 2
    assert calls == [
        "disjointness",
        "coverage",
        "document",
        "native-document",
        "schema",
    ]


@pytest.mark.parametrize("container", ["list", "dict"])
def test_tool_cycle_keeps_native_serialization_failure(
    tmp_path: Path, container: str
) -> None:
    cycle: list[object] | dict[str, object]
    if container == "list":
        cycle = []
        cycle.append(cycle)
    else:
        cycle = {}
        cycle["self"] = cycle
    value = replace(
        lock_input(index_pin()), provenance=BlockProvenance({"cycle": cycle})
    )
    with pytest.raises(RecursionError):
        pylock.build_pylock(value, lock_dir=tmp_path).to_dict()
    with pytest.raises(RecursionError):
        pylock.render_lock(value, lock_dir=tmp_path)


def test_custom_metaclass_cannot_admit_a_scalar(tmp_path: Path) -> None:
    class PretendMarker(type):
        def __eq__(cls, other: object) -> bool:
            return other is Marker

        def __hash__(cls) -> int:
            return type.__hash__(cls)

    class CopiedScalar(str, metaclass=PretendMarker):
        __slots__ = ()

        def __deepcopy__(self, _memo: dict[int, object]) -> str:
            return "https://copied.invalid/"

    value = lock_input(
        index_pin().replace(index=CopiedScalar("https://original.invalid/"))
    )
    expected = tomli_w.dumps(pylock.build_pylock(value, lock_dir=tmp_path).to_dict())
    text, calls = observe_render(value, lock_dir=tmp_path)

    assert text == expected
    assert "copied" in text
    assert calls == [
        "disjointness",
        "coverage",
        "document",
        "native-document",
        "schema",
    ]


def test_cycle_admission_preserves_earlier_tool_copy_failure(tmp_path: Path) -> None:
    class CopyFailure(str):
        __slots__ = ()

        def __deepcopy__(self, _memo: dict[int, object]) -> str:
            raise RuntimeError("tool copy failed")

    cycle: list[object] = []
    cycle.append(cycle)
    value = replace(
        lock_input(index_pin()),
        provenance=BlockProvenance({"first": CopyFailure("original"), "cycle": cycle}),
    )
    with pytest.raises(RuntimeError, match="tool copy failed"):
        pylock.build_pylock(value, lock_dir=tmp_path).to_dict()
    with pytest.raises(RuntimeError, match="tool copy failed"):
        pylock.render_lock(value, lock_dir=tmp_path)


def test_plain_tool_scalars_keep_direct_emission(tmp_path: Path) -> None:
    value = replace(
        lock_input(index_pin()),
        provenance=BlockProvenance({"flag": True, "count": 0, "ratio": 0.5}),
    )
    expected = tomli_w.dumps(pylock.build_pylock(value, lock_dir=tmp_path).to_dict())
    text, calls = observe_render(value, lock_dir=tmp_path)

    assert text == expected
    assert calls == ["disjointness", "coverage", "document", "schema"]


def test_custom_tool_mapping_uses_native_reconstruction(tmp_path: Path) -> None:
    class AppendingDict(dict[str, str]):
        def __init__(self, items: Iterable[tuple[str, str]]) -> None:
            super().__init__(items)
            self["copies"] = self.get("copies", "") + "x"

    value = replace(
        lock_input(index_pin()),
        provenance=BlockProvenance({"values": AppendingDict((("original", "value"),))}),
    )
    expected = tomli_w.dumps(pylock.build_pylock(value, lock_dir=tmp_path).to_dict())
    text, calls = observe_render(value, lock_dir=tmp_path)

    assert text == expected
    assert 'copies = "xx"' in text
    assert calls == [
        "disjointness",
        "coverage",
        "document",
        "native-document",
        "schema",
    ]


def test_custom_datetime_timezone_keeps_native_copy_failure(tmp_path: Path) -> None:
    class CopyZone(tzinfo):
        def utcoffset(self, _value: datetime | None) -> timedelta:
            return timedelta(0)

        def dst(self, _value: datetime | None) -> None:
            return None

        def tzname(self, _value: datetime | None) -> str:
            return "UTC"

        def __deepcopy__(self, _memo: dict[int, object]) -> tzinfo:
            raise RuntimeError("timezone copy failed")

    value = replace(
        lock_input(index_pin()),
        provenance=BlockProvenance(
            {"created": datetime(2026, 10, 23, tzinfo=CopyZone())}
        ),
    )
    with pytest.raises(RuntimeError, match="timezone copy failed"):
        pylock.build_pylock(value, lock_dir=tmp_path).to_dict()
    with pytest.raises(RuntimeError, match="timezone copy failed"):
        pylock.render_lock(value, lock_dir=tmp_path)


def test_naive_tool_datetime_keeps_direct_emission(tmp_path: Path) -> None:
    value = replace(
        lock_input(index_pin()),
        provenance=BlockProvenance(
            {
                "created": datetime(2026, 10, 23, tzinfo=timezone.utc).replace(
                    tzinfo=None
                )
            }
        ),
    )
    expected = tomli_w.dumps(pylock.build_pylock(value, lock_dir=tmp_path).to_dict())
    text, calls = observe_render(value, lock_dir=tmp_path)

    assert text == expected
    assert calls == ["disjointness", "coverage", "document", "schema"]


def test_public_specifier_iterable_in_tool_keeps_native_copy_failure(
    tmp_path: Path,
) -> None:
    class CopySpecifier(Specifier):
        def __deepcopy__(self, _memo: dict[int, object]) -> Specifier:
            raise RuntimeError("specifier copy failed")

    value = replace(
        lock_input(index_pin()),
        provenance=BlockProvenance(
            {"requires-python": SpecifierSet([CopySpecifier(">=3")])}
        ),
    )
    with pytest.raises(RuntimeError, match="specifier copy failed"):
        pylock.build_pylock(value, lock_dir=tmp_path).to_dict()
    with pytest.raises(RuntimeError, match="specifier copy failed"):
        pylock.render_lock(value, lock_dir=tmp_path)


class CopyRange(str):
    """Retain custom text through public specifier parsing operations."""

    __slots__ = ()

    def split(self, sep: str | None = None, maxsplit: int = -1) -> list[str]:
        return [self]

    def strip(self, chars: str | None = None) -> Self:
        return self

    def __getitem__(self, key: SupportsIndex | slice) -> str:
        """Retain the custom scalar in parsed operator and version values."""
        return type(self)(super().__getitem__(key))

    def __deepcopy__(self, _memo: dict[int, object]) -> str:
        """Change the parsed version only when the native model copies it."""
        return "4" if self == "3" else str(self)


@pytest.mark.parametrize("field", ["root", "package"])
@pytest.mark.parametrize("copy_error", [False, True])
def test_semantic_specifier_text_keeps_native_copy_behavior(
    tmp_path: Path, field: str, copy_error: bool
) -> None:
    class CopyFailureRange(CopyRange):
        __slots__ = ()

        def __deepcopy__(self, _memo: dict[int, object]) -> str:
            raise RuntimeError("range copy failed")

    supplied = CopyFailureRange(">=3") if copy_error else CopyRange(">=3")
    pin = index_pin()
    if field == "package":
        pin = pin.replace(requires_python=supplied)
    value = lock_input(pin)
    if field == "root":
        value = replace(value, requires_python=supplied)
    if copy_error:
        with pytest.raises(RuntimeError, match="range copy failed"):
            pylock.build_pylock(value, lock_dir=tmp_path).to_dict()
        with pytest.raises(RuntimeError, match="range copy failed"):
            pylock.render_lock(value, lock_dir=tmp_path)
        with pytest.raises(CoverageError):
            pylock.render_lock(
                replace(value, environments=[Marker('python_version == "3.11"')]),
                lock_dir=tmp_path,
            )
        return

    expected = pylock.build_pylock(value, lock_dir=tmp_path).to_dict()
    if field == "root":
        assert expected["requires-python"] == ">=4"
    else:
        assert expected["packages"][0]["requires-python"] == ">=4"
    text, calls = observe_render(value, lock_dir=tmp_path)

    assert text == tomli_w.dumps(expected)
    assert calls == [
        "disjointness",
        "coverage",
        "document",
        "native-document",
        "schema",
    ]


@pytest.mark.parametrize("field", ["root", "package"])
def test_runtime_specifier_object_keeps_native_member_copy(
    tmp_path: Path, field: str
) -> None:
    class CopySpecifier(Specifier):
        def __deepcopy__(self, _memo: dict[int, object]) -> Specifier:
            return Specifier(">=4")

    # These public replace methods accept the non-string value at runtime.
    supplied = SpecifierSet([CopySpecifier(">=3")])
    pin = index_pin()
    if field == "package":
        pin = pin.replace(requires_python=supplied)
    value = lock_input(pin)
    if field == "root":
        value = replace(value, requires_python=supplied)
    expected = pylock.build_pylock(value, lock_dir=tmp_path).to_dict()
    if field == "root":
        assert expected["requires-python"] == ">=4"
    else:
        assert expected["packages"][0]["requires-python"] == ">=4"
    text, calls = observe_render(value, lock_dir=tmp_path)

    assert text == tomli_w.dumps(expected)
    assert calls == [
        "disjointness",
        "coverage",
        "document",
        "native-document",
        "schema",
    ]


class CopyOperatorRange(CopyRange):
    """Keep only the operator as custom scalar state."""

    __slots__ = ()

    def __getitem__(self, key: SupportsIndex | slice) -> str:
        """Return ordinary version text and a custom operator."""
        text = str.__getitem__(self, key)
        return type(self)(text) if text == ">=" else text

    def __deepcopy__(self, _memo: dict[int, object]) -> str:
        """Change the operator only when native copying reaches it."""
        return ">"


class CopyVersionRange(CopyRange):
    """Keep only the version as custom scalar state."""

    __slots__ = ()

    def __getitem__(self, key: SupportsIndex | slice) -> str:
        """Return an ordinary operator and custom version text."""
        text = str.__getitem__(self, key)
        return type(self)(text) if text == "3" else text


@pytest.mark.parametrize("field", ["root", "package"])
@pytest.mark.parametrize(
    ("range_type", "expected_range"),
    [(CopyOperatorRange, ">3"), (CopyVersionRange, ">=4")],
)
def test_semantic_specifier_component_copy_changes_output(
    tmp_path: Path,
    field: str,
    range_type: type[CopyRange],
    expected_range: str,
) -> None:
    supplied = range_type(">=3")
    pin = index_pin()
    if field == "package":
        pin = pin.replace(requires_python=supplied)
    value = lock_input(pin)
    if field == "root":
        value = replace(value, requires_python=supplied)
    expected = pylock.build_pylock(value, lock_dir=tmp_path).to_dict()
    if field == "root":
        assert expected["requires-python"] == expected_range
    else:
        assert expected["packages"][0]["requires-python"] == expected_range
    text, calls = observe_render(value, lock_dir=tmp_path)

    assert text == tomli_w.dumps(expected)
    assert calls == [
        "disjointness",
        "coverage",
        "document",
        "native-document",
        "schema",
    ]
