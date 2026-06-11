"""Shared Hypothesis strategies for the vendored-packaging guard tests.

These generate the PEP 440 shapes the differential suites exercise:
epochs, pre/post/dev segments, local versions, zero padding, long
releases, wildcards, ``~=``, and ``===``, plus probe lists that mix
valid and invalid version strings.
"""

from __future__ import annotations

from hypothesis import strategies as st

EPOCHS = st.sampled_from(["", "1!", "3!"])
PRES = st.sampled_from(["", "a1", "b2", "rc1", "a0", ".rc1", "-c3"])
POSTS = st.sampled_from(["", ".post0", ".post1", ".post2", "-1", ".rev2"])
DEVS = st.sampled_from(["", ".dev0", ".dev1", ".dev3"])
LOCALS = st.sampled_from(["", "+abc", "+abc.1", "+0.zz", "+ubuntu-1"])

PROBE_EXTRAS = st.sampled_from(
    ["0", "0.dev0", "0a0.dev0", "not-a-version", "1.0.0", "1!0", "9999"]
)


@st.composite
def releases(draw: st.DrawFn, max_segments: int = 4) -> str:
    """Generate a release segment, occasionally zero-padded.

    A padded segment like ``01`` parses and normalizes to ``1``, so the
    canonical-form properties can treat padding as an identity.
    """
    segments = draw(st.lists(st.integers(0, 12), min_size=1, max_size=max_segments))
    parts = [str(value) for value in segments]
    if draw(st.booleans()) and draw(st.booleans()):
        index = draw(st.integers(0, len(parts) - 1))
        parts[index] = "0" + parts[index]
    return ".".join(parts)


@st.composite
def version_strings(draw: st.DrawFn) -> str:
    """Generate a full PEP 440 version string, local segment included."""
    return (
        draw(EPOCHS)
        + draw(releases())
        + draw(PRES)
        + draw(POSTS)
        + draw(DEVS)
        + draw(LOCALS)
    )


@st.composite
def version_strings_no_local(draw: st.DrawFn) -> str:
    """Generate a PEP 440 version string without a local segment."""
    return draw(EPOCHS) + draw(releases()) + draw(PRES) + draw(POSTS) + draw(DEVS)


@st.composite
def specifier_strings(draw: st.DrawFn) -> str:
    """Generate a single PEP 440 specifier across every operator kind."""
    kind = draw(
        st.sampled_from(
            ["cmp", "cmp", "cmp", "eq", "eq", "wildcard", "compatible", "arbitrary"]
        )
    )
    if kind == "cmp":
        op = draw(st.sampled_from([">=", "<=", ">", "<"]))
        return op + draw(version_strings_no_local())
    if kind == "eq":
        op = draw(st.sampled_from(["==", "!="]))
        return op + draw(version_strings())
    if kind == "wildcard":
        op = draw(st.sampled_from(["==", "!="]))
        return op + draw(EPOCHS) + draw(releases()) + ".*"
    if kind == "compatible":
        release = draw(releases(max_segments=3))
        if "." not in release:
            release += ".0"
        return "~=" + draw(EPOCHS) + release + draw(PRES) + draw(POSTS) + draw(DEVS)
    literal = draw(
        st.one_of(version_strings(), st.sampled_from(["wat", "1.0.foo", "WAT"]))
    )
    return "===" + literal


@st.composite
def specifier_set_strings(draw: st.DrawFn, max_specs: int = 3) -> str:
    """Generate a comma-joined specifier set string."""
    specs = draw(st.lists(specifier_strings(), min_size=1, max_size=max_specs))
    return ",".join(specs)


@st.composite
def probe_lists(draw: st.DrawFn, min_size: int = 4, max_size: int = 10) -> list[str]:
    """Generate probe version strings, mixing valid and invalid shapes."""
    return draw(
        st.lists(
            st.one_of(version_strings(), PROBE_EXTRAS),
            min_size=min_size,
            max_size=max_size,
        )
    )
