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
    from collections.abc import Mapping

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
