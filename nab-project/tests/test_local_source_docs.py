"""Static metadata admission for every PEP 621 dynamic field."""

from __future__ import annotations

from pathlib import Path

from nab_project.build_backend import extract_static_metadata

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


def test_dynamic_fields_requiring_a_backend(tmp_path: Path) -> None:
    """Only dynamic resolution metadata prevents a static read."""
    assert _fields_forcing_a_build(tmp_path) == {
        "version",
        "requires-python",
        "dependencies",
        "optional-dependencies",
    }


def test_missing_project_table_requires_a_backend(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["setuptools"]\n', encoding="utf-8"
    )
    assert extract_static_metadata(tmp_path) is None
