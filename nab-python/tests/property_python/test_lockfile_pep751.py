"""Property tests for :mod:`nab_python.lockfile`.

`PEP 751`_ defines the ``pylock.toml`` lockfile format that nab
emits.  This file walks the relevant sections of PEP 751 and adds a
property test for each invariant the writer must satisfy: spec
compliance via ``packaging.pylock`` round-trip, deterministic output,
and stable name ordering.

.. _PEP 751: https://peps.python.org/pep-0751/
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - 3.10 fallback
    import tomli as tomllib  # type: ignore[no-redef]

from nab_python._vendor.packaging.markers import Marker
from nab_python._vendor.packaging.pylock import Pylock
from nab_python.lockfile import (
    IndexPin,
    LocalPin,
    LockInput,
    SdistArtifact,
    VcsPin,
    WheelArtifact,
    build_pylock,
    write_lock,
)

from .strategies import (
    PROPERTY_SETTINGS,
    canonical_names,
    sha256s,
    versions,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

pytestmark = pytest.mark.property


def _wheel(name: str, version: str, sha: str) -> WheelArtifact:
    """Build a ``WheelArtifact`` with PEP 427-compliant filename."""
    norm = name.replace("-", "_")
    return WheelArtifact(
        filename=f"{norm}-{version}-py3-none-any.whl",
        url=f"https://example.com/{norm}-{version}-py3-none-any.whl",
        hashes=(("sha256", sha),),
        size=1024,
    )


def _wheel_named(filename: str, sha: str) -> WheelArtifact:
    """Build a ``WheelArtifact`` from a literal filename."""
    return WheelArtifact(
        filename=filename,
        url=f"https://example.com/{filename}",
        hashes=(("sha256", sha),),
        size=1024,
    )


def _sdist(name: str, version: str, sha: str) -> SdistArtifact:
    """Build an ``SdistArtifact`` with PEP 625-compliant filename."""
    norm = name.replace("-", "_")
    return SdistArtifact(
        filename=f"{norm}-{version}.tar.gz",
        url=f"https://example.com/{norm}-{version}.tar.gz",
        hashes=(("sha256", sha),),
        size=2048,
    )


@st.composite
def index_pins(draw: st.DrawFn) -> IndexPin:
    """Generate an ``IndexPin`` over the canonical-name pool."""
    name = draw(canonical_names)
    version = draw(versions)
    sha = draw(sha256s)
    return IndexPin(
        name=name,
        version=version,
        index="pypi",
        sdist=_sdist(name, version, sha),
        wheels=(_wheel(name, version, sha),),
    )


@st.composite
def local_pins(draw: st.DrawFn) -> LocalPin:
    """Generate a ``LocalPin`` for a directory reference."""
    name = draw(canonical_names)
    return LocalPin(
        name=name,
        version=draw(versions),
        path=f"/tmp/{name}",
    )


@st.composite
def vcs_pins(draw: st.DrawFn) -> VcsPin:
    """Generate a ``VcsPin`` for a git reference with a 40-hex commit id."""
    name = draw(canonical_names)
    sha_hex = draw(st.from_regex(r"[0-9a-f]{40}", fullmatch=True))
    return VcsPin(
        name=name,
        version=draw(versions),
        repo_url=f"https://github.com/example/{name}.git",
        bare_repo_url=f"https://github.com/example/{name}.git",
        commit_id=sha_hex,
    )


pin_strategies = st.one_of(index_pins(), local_pins(), vcs_pins())


@st.composite
def lock_inputs(draw: st.DrawFn) -> LockInput:
    """Generate a deduplicated ``LockInput`` with up to 8 pins."""
    n = draw(st.integers(min_value=0, max_value=8))
    pins = [draw(pin_strategies) for _ in range(n)]
    by_name: dict[str, IndexPin | LocalPin | VcsPin] = {}
    for pin in pins:
        if pin.name not in by_name:
            by_name[pin.name] = pin
    return LockInput(pins=by_name)


class TestQuoteSpecCompliantOutput:
    """PEP 751, § File Format:

    > "The format of the file is `TOML <https://toml.io/>`_."

    The writer must emit content that round-trips through both
    ``tomllib`` (TOML compliance) and the standalone
    ``packaging.pylock.Pylock.from_dict`` validator (PEP 751
    structural compliance).

    Reference: https://peps.python.org/pep-0751/#file-format
    """

    @given(lock_input=lock_inputs())
    @PROPERTY_SETTINGS
    def test_write_then_parse(self, lock_input: LockInput) -> None:
        """Output round-trips through ``tomllib`` + ``packaging.pylock``."""
        text = write_lock(lock_input)
        data = tomllib.loads(text)
        pylock = Pylock.from_dict(data)
        assert len(pylock.packages) == len(lock_input.pins)


class TestQuoteStablePackageOrdering:
    """PEP 751, § File Format:

    > "Tools SHOULD write their lock files in a consistent way to
    > minimize noise in diff output. Keys in tables, including the
    > top-level table, SHOULD be recorded in a consistent order
    > (if inspiration is desired, this PEP has tried to write down
    > keys in a logical order). As well, tools SHOULD sort arrays
    > in consistent order."

    nab's writer sorts the ``packages`` array by name.  A user
    re-running ``nab lock`` over the same inputs therefore sees a
    clean diff with no spurious reorderings.

    Reference: https://peps.python.org/pep-0751/#file-format
    """

    @given(lock_input=lock_inputs())
    @PROPERTY_SETTINGS
    def test_packages_sorted_by_name(self, lock_input: LockInput) -> None:
        """``packages`` entries are sorted by ``name``."""
        pylock = build_pylock(lock_input)
        names = [str(p.name) for p in pylock.packages]
        assert names == sorted(names)


class TestDeterministicOutput:
    """PEP 751 recommends consistent output to minimize diff noise.
    nab's stronger guarantee: identical inputs produce identical
    output, byte-for-byte.

    The writer must not depend on dict iteration order, ``id()``,
    or other non-deterministic sources.

    Reference: https://peps.python.org/pep-0751/#file-format
    """

    @given(lock_input=lock_inputs())
    @PROPERTY_SETTINGS
    def test_deterministic(self, lock_input: LockInput) -> None:
        """Calling ``write_lock`` twice on the same input is byte-equal."""
        first = write_lock(lock_input)
        second = write_lock(lock_input)
        assert first == second


def _multi_wheel(name: str, version: str) -> tuple[WheelArtifact, ...]:
    """Build several wheels for one pin, each with several hashes.

    The wheels are returned in a deliberately non-sorted filename order
    so a caller can prove the emitter sorts them rather than echoing
    insertion order.
    """
    norm = name.replace("-", "_")

    def _wheel_with_hashes(tag: str, primary: str) -> WheelArtifact:
        return WheelArtifact(
            filename=f"{norm}-{version}-{tag}.whl",
            url=f"https://example.com/{norm}-{version}-{tag}.whl",
            hashes=(
                ("sha512", primary * 128),
                ("sha256", primary * 64),
                ("sha384", primary * 96),
            ),
            size=1024,
        )

    return (
        _wheel_with_hashes("cp312-cp312-win_amd64", "f"),
        _wheel_with_hashes("cp310-cp310-manylinux_2_17_x86_64", "a"),
        _wheel_with_hashes("cp311-cp311-macosx_11_0_arm64", "c"),
    )


def _reorder(seq: Sequence[object], shift: int) -> list[object]:
    """Rotate ``seq`` by ``shift`` to get a different but complete order."""
    items = list(seq)
    if not items:
        return items
    shift %= len(items)
    return items[shift:] + items[:shift]


def _per_tuple_input(
    *,
    names: Sequence[str],
    labels: Sequence[str],
    versions_by_label: Mapping[str, str],
    markers_by_label: Mapping[str, str],
    wheel_shift: int,
) -> LockInput:
    """Assemble a per-tuple ``LockInput`` over the given orderings.

    ``labels`` fixes the tuple order, the ``per_tuple_pins`` dict is
    built in that order, and each pin's wheels are rotated by
    ``wheel_shift`` so two calls with different orderings exercise the
    insertion-order paths the emitter must neutralise.
    """
    per_tuple_pins: dict[str, dict[str, IndexPin]] = {}
    for label in labels:
        version = versions_by_label[label]
        per_name: dict[str, IndexPin] = {}
        for name in names:
            wheels = tuple(_reorder(_multi_wheel(name, version), wheel_shift))
            per_name[name] = IndexPin(
                name=name,
                version=version,
                index="pypi",
                wheels=wheels,  # type: ignore[arg-type]
            )
        per_tuple_pins[label] = per_name
    tuple_markers = {label: Marker(markers_by_label[label]) for label in labels}
    return LockInput(per_tuple_pins=per_tuple_pins, tuple_markers=tuple_markers)


class TestQuoteShuffledInputInvariant:
    """PEP 751, § File Format:

    > "Tools SHOULD write their lock files in a consistent way to
    > minimize noise in diff output. ... As well, tools SHOULD sort
    > arrays in consistent order."

    The byte-stability guarantee must survive a reordering of every
    insertion-ordered input: the package map, the per-tuple map, each
    pin's wheel list, each wheel's hash pairs, and the OR-marker
    fragments. Emitting a shuffled-but-equivalent input must produce
    identical bytes; otherwise a re-resolve that happened to populate
    its dicts in a different order would churn the lockfile.

    Reference: https://peps.python.org/pep-0751/#file-format
    """

    def test_shuffled_per_tuple_input_is_byte_equal(self) -> None:
        """Reordering every input axis yields byte-identical output."""
        names = ["alpha", "beta", "gamma"]
        labels = ["py310", "py311", "py312", "py313"]
        versions_by_label = {
            "py310": "1.0",
            "py311": "1.0",
            "py312": "2.0",
            "py313": "2.0",
        }
        markers_by_label = {
            "py310": 'python_version == "3.10"',
            "py311": 'python_version == "3.11"',
            "py312": 'python_version == "3.12"',
            "py313": 'python_version == "3.13"',
        }
        canonical = write_lock(
            _per_tuple_input(
                names=names,
                labels=labels,
                versions_by_label=versions_by_label,
                markers_by_label=markers_by_label,
                wheel_shift=0,
            )
        )
        shuffled = write_lock(
            _per_tuple_input(
                names=list(reversed(names)),
                labels=list(reversed(labels)),
                versions_by_label=versions_by_label,
                markers_by_label=markers_by_label,
                wheel_shift=1,
            )
        )
        assert shuffled == canonical


class TestQuoteMarkerCollisionOrdering:
    """PEP 751, § ``[[packages]]``:

    > "Packages MAY be listed multiple times with varying data, but
    > all packages to be installed MUST narrow down to a single entry
    > at install time."

    Two entries with the same name and version but different markers
    are a legal per-tuple shape (e.g. one index per environment). The
    name+version sort key alone cannot separate them, so the emitter
    must break the tie on the marker string. Both entries must survive
    and their relative order must not depend on insertion order.

    Reference: https://peps.python.org/pep-0751/#packages
    """

    def _collision_input(self, *, labels: Sequence[str]) -> LockInput:
        per_tuple_pins = {
            "linux": {
                "foo": IndexPin(
                    name="foo",
                    version="1.0",
                    index="https://linux.example/simple",
                    wheels=(_wheel("foo", "1.0", "a" * 64),),
                )
            },
            "darwin": {
                "foo": IndexPin(
                    name="foo",
                    version="1.0",
                    index="https://darwin.example/simple",
                    wheels=(_wheel("foo", "1.0", "b" * 64),),
                )
            },
        }
        ordered = {label: per_tuple_pins[label] for label in labels}
        tuple_markers = {
            "linux": Marker('sys_platform == "linux"'),
            "darwin": Marker('sys_platform == "darwin"'),
        }
        return LockInput(per_tuple_pins=ordered, tuple_markers=tuple_markers)

    def test_same_name_version_distinct_markers_stable(self) -> None:
        """A same-name same-version marker pair sorts stably by marker."""
        forward = write_lock(self._collision_input(labels=["linux", "darwin"]))
        reverse = write_lock(self._collision_input(labels=["darwin", "linux"]))
        assert forward == reverse
        data: Mapping[str, object] = tomllib.loads(forward)
        packages = data["packages"]
        assert isinstance(packages, list)
        assert len(packages) == 2
        markers = [pkg["marker"] for pkg in packages]
        assert markers == sorted(markers)


class TestQuoteWheelOrdering:
    """PEP 751, § ``[[packages.wheels]]``:

    > "tools SHOULD sort arrays in consistent order."

    A pin's wheels arrive in whatever order the index listed them.
    The emitter sorts them by filename so the ``packages.wheels``
    array is stable across runs and across indexes that list the same
    files in a different order.

    Reference: https://peps.python.org/pep-0751/#packageswheels
    """

    def test_wheels_emitted_in_filename_order(self) -> None:
        """Out-of-order wheels emit filename-ascending."""
        wheels = (
            _wheel_named("foo-1.0-cp312-cp312-win_amd64.whl", "f" * 64),
            _wheel_named("foo-1.0-cp310-cp310-manylinux_2_17_x86_64.whl", "a" * 64),
            _wheel_named("foo-1.0-cp311-cp311-macosx_11_0_arm64.whl", "c" * 64),
        )
        pin = IndexPin(name="foo", version="1.0", index="pypi", wheels=wheels)
        text = write_lock(LockInput(pins={"foo": pin}))
        data: Mapping[str, object] = tomllib.loads(text)
        packages = data["packages"]
        assert isinstance(packages, list)
        emitted = [w["name"] for w in packages[0]["wheels"]]
        assert emitted == sorted(emitted)
        assert emitted == [
            "foo-1.0-cp310-cp310-manylinux_2_17_x86_64.whl",
            "foo-1.0-cp311-cp311-macosx_11_0_arm64.whl",
            "foo-1.0-cp312-cp312-win_amd64.whl",
        ]


class TestQuoteUniversalPerTuplePackages:
    """PEP 751, § ``[[packages]]``:

    > "An array containing all packages that *may* be installed.
    > Packages MAY be listed multiple times with varying data, but
    > all packages to be installed MUST narrow down to a single
    > entry at install time."

    > "The environment marker which specify when the package should
    > be installed." (The ``marker`` field of a packages entry.)

    Universal locks contain per-tuple groups: a package may resolve
    to different versions on Python 3.10 vs 3.11, in which case the
    writer emits two ``[[packages]]`` entries with disjoint
    ``marker`` strings.

    Reference: https://peps.python.org/pep-0751/#packages
    """

    @given(
        names=st.lists(canonical_names, min_size=1, max_size=3, unique=True),
        version_a=versions,
        version_b=versions,
    )
    @PROPERTY_SETTINGS
    def test_diverging_pins_emit_per_group(
        self,
        names: list[str],
        version_a: str,
        version_b: str,
    ) -> None:
        """Diverging per-tuple pins emit one ``packages`` entry per group."""
        sha_a = "a" * 64
        sha_b = "b" * 64
        per_tuple_pins = {
            "py310": {
                name: IndexPin(
                    name=name,
                    version=version_a,
                    index="pypi",
                    wheels=(_wheel(name, version_a, sha_a),),
                )
                for name in names
            },
            "py311": {
                name: IndexPin(
                    name=name,
                    version=version_b,
                    index="pypi",
                    wheels=(_wheel(name, version_b, sha_b),),
                )
                for name in names
            },
        }
        tuple_markers = {
            "py310": Marker('python_version == "3.10"'),
            "py311": Marker('python_version == "3.11"'),
        }
        text = write_lock(
            LockInput(
                per_tuple_pins=per_tuple_pins,
                tuple_markers=tuple_markers,
            )
        )
        data: Mapping[str, object] = tomllib.loads(text)
        expected = (1 if version_a == version_b else 2) * len(names)
        assert len(data["packages"]) == expected  # type: ignore[arg-type]
