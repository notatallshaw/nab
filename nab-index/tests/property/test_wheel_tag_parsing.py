"""Compare tags in wheel filenames and check that each admitted wheel carries tags."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from packaging import utils as upstream_utils
from packaging.utils import InvalidWheelFilename
from packaging.version import InvalidVersion

from nab_index.client import _parse_wheel_filename
from nab_provider.tags import wheel_tag_set

from .settings import PROPERTY_SETTINGS

pytestmark = pytest.mark.property

tag_part = st.text(alphabet="abcdefgh0123456789_", min_size=1, max_size=8)
compressed = st.lists(tag_part, min_size=1, max_size=3, unique=True).map(".".join)

# Packaging 26.3 requires interpreter identifiers. Use inputs accepted by
# both the vendored parser and the earlier released parser in this comparison.
compressed_interpreters = st.lists(
    tag_part.filter(str.isidentifier), min_size=1, max_size=3, unique=True
).map(".".join)


name_strategy = st.text(alphabet="abcxyz0123456789_", min_size=1, max_size=8)
version_strategy = st.sampled_from(
    ["1.0", "2.1.3", "0.9a1", "1!2.0", "3.0.post1", "1.0+local"]
)
build_strategy = st.none() | st.integers(0, 99).map(str)


@st.composite
def wheel_filenames(draw: st.DrawFn, interpreters: st.SearchStrategy[str]) -> str:
    """Generate a wheel filename with optional build tag and random tag parts.

    ``interpreters`` supplies the first tag field, letting a caller pick
    spec-valid ones or the wider alphabet.
    """
    name = draw(name_strategy)
    version = draw(version_strategy)
    build = draw(build_strategy)
    parts = [name, version]
    if build is not None:
        parts.append(build)
    parts += [draw(interpreters), draw(compressed), draw(compressed)]
    return "-".join(parts) + ".whl"


class TestWheelTagSetMatchesUpstream:
    """``wheel_tag_set`` parses the tag suffix out of wheel filenames;
    on every filename upstream ``parse_wheel_filename`` accepts, the
    extracted tag set must agree.  Filenames upstream rejects are out
    of scope: nab is intentionally permissive there.
    """

    @given(filename=wheel_filenames(compressed_interpreters))
    @PROPERTY_SETTINGS
    def test_wheel_tag_set_matches_upstream_parse_wheel_filename(
        self, filename: str
    ) -> None:
        """``wheel_tag_set`` agrees with upstream on spec-valid filenames."""
        try:
            _, _, _, expected = upstream_utils.parse_wheel_filename(filename)
        except (InvalidWheelFilename, InvalidVersion):
            return  # upstream rejects; nab is intentionally permissive
        got = wheel_tag_set(filename)
        assert got is not None
        assert {(t.interpreter, t.abi, t.platform) for t in got} == {
            (t.interpreter, t.abi, t.platform) for t in expected
        }


class TestAdmittedWheelsCarryTags:
    """A wheel nab_index admits must be one ``nab_provider.tags`` can rank.

    nab_index applies its own copy of the tag rules and nab_provider runs the
    vendored ``parse_tag``, so re-vendoring a stricter one is how the two come
    apart. The interpreter field draws the wide alphabet to reach that.
    """

    @given(filename=wheel_filenames(compressed))
    @PROPERTY_SETTINGS
    def test_readable_wheel_has_tags(self, filename: str) -> None:
        if _parse_wheel_filename(filename) is None:
            return
        assert wheel_tag_set(filename) is not None
