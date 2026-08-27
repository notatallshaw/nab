"""Check what the docs say triggers a build of a local source.

A checkout goes to a PEP 517 backend when ``extract_static_metadata``
returns ``None`` for it.  Two pages enumerate the ``[project].dynamic``
entries that empty the static read, so the expected set is derived from
the reader rather than repeated here.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest

from nab_project.build_backend import extract_static_metadata

_DOCS = Path(__file__).resolve().parents[2] / "docs"

# Every [project] field a backend may compute, and so every name the
# static read could refuse.  PEP 621 forbids listing name in dynamic.
_PROJECT_FIELDS = frozenset(
    {
        "version",
        "description",
        "readme",
        "requires-python",
        "license",
        "license-files",
        "authors",
        "maintainers",
        "keywords",
        "classifiers",
        "urls",
        "scripts",
        "gui-scripts",
        "entry-points",
        "dependencies",
        "optional-dependencies",
    }
)


def _how_to_section() -> str:
    """The local-source how-to's "Dynamic metadata" body."""
    text = (_DOCS / "how-to" / "local-sources.md").read_text(encoding="utf-8")
    _, marker, rest = text.partition("\n## Dynamic metadata\n")
    if not marker:
        raise AssertionError("no dynamic-metadata section in local-sources.md")
    return rest.partition("\n## ")[0]


def _build_policy_bullet() -> str:
    """The build-policy reference's local-checkout bullet under ``never``."""
    text = (_DOCS / "reference" / "build-policy.md").read_text(encoding="utf-8")
    _, marker, rest = text.partition("\n* Local checkouts declared via")
    if not marker:
        raise AssertionError("no local-checkout bullet in build-policy.md")
    return rest.partition("\n* ")[0]


_SECTIONS: dict[str, Callable[[], str]] = {
    "how-to/local-sources.md": _how_to_section,
    "reference/build-policy.md": _build_policy_bullet,
}


def _checkout(path: Path, dynamic_field: str) -> Path:
    """A checkout whose ``[project]`` is static apart from ``dynamic_field``.

    PEP 621 forbids giving a field a static value while listing it in
    ``dynamic``, so the static ``version`` drops out when ``version`` is
    the dynamic field.
    """
    path.mkdir(parents=True)

    fields = ['name = "my-fork"']
    if dynamic_field != "version":
        fields.append('version = "1.0"')
    fields.append(f'dynamic = ["{dynamic_field}"]')

    body = "[project]\n" + "\n".join(fields) + "\n"
    (path / "pyproject.toml").write_text(body, encoding="utf-8")
    return path


def _fields_forcing_a_build(tmp_path: Path) -> frozenset[str]:
    """The fields that empty the static read by appearing in ``dynamic``."""
    return frozenset(
        field
        for field in _PROJECT_FIELDS
        if extract_static_metadata(_checkout(tmp_path / field, field)) is None
    )


def _fields_named(section: str) -> frozenset[str]:
    """The ``[project]`` field names ``section`` quotes in backticks."""
    return _PROJECT_FIELDS & set(re.findall(r"`([a-z-]+)`", section))


@pytest.mark.parametrize("page", sorted(_SECTIONS))
def test_page_names_every_dynamic_field_that_forces_a_build(
    page: str, tmp_path: Path
) -> None:
    """The page's list is exactly the set the static reader refuses."""
    assert _fields_named(_SECTIONS[page]()) == _fields_forcing_a_build(tmp_path)


@pytest.mark.parametrize("page", sorted(_SECTIONS))
def test_page_names_a_missing_project_table(page: str, tmp_path: Path) -> None:
    """The other trigger, a checkout with no ``[project]``, is named too."""
    (tmp_path / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["setuptools"]\n', encoding="utf-8"
    )
    assert extract_static_metadata(tmp_path) is None

    section = _SECTIONS[page]()
    assert "missing" in section
    assert "`[project]`" in section
