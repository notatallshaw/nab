"""Check the README's VCS policy section against the admission code.

The README is the ``nab`` distribution's PyPI description, so it states the
default posture to readers who never open the documentation site.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import tomli

from nab_project.config_sources import OPTIONS
from nab_provider.vcs_admission import UnsupportedVcsError, VcsConfig, admit_vcs_url

README = Path(__file__).resolve().parents[1] / "README.md"

PINNED_URL = f"git+https://github.com/myorg/pkg.git@{'0' * 40}"


def _vcs_section() -> str:
    """The README body under ``## VCS policy``, up to the next heading.

    A heading only counts outside a fenced block, so a ``#`` comment in a
    toml example does not cut the section short.
    """
    body = README.read_text(encoding="utf-8").partition("\n## VCS policy\n")[2]
    assert body, "README.md has no VCS policy section"

    lines: list[str] = []
    fenced = False
    for line in body.splitlines():
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and line.startswith("#"):
            break
        lines.append(line)

    return "\n".join(lines)


def _documented_default() -> VcsConfig:
    """The section's toml block, parsed by the registry's own ``vcs`` parser."""
    block = re.search(r"```toml\n(.*?)\n```", _vcs_section(), re.DOTALL)
    assert block, "the VCS policy section has no toml block"

    spec = next(option for option in OPTIONS if option.key == "vcs")
    return spec.parse(tomli.loads(block[1])["tool"]["nab"]["vcs"], where="README.md")


def test_documented_default_is_the_shipped_default() -> None:
    """The block the section leads with matches ``VcsConfig``, key for key."""
    assert _documented_default() == VcsConfig()


def test_documented_default_refuses_a_pinned_url() -> None:
    """A commit-pinned URL is refused under the block, as the section says."""
    with pytest.raises(UnsupportedVcsError, match=r'vcs\.policy is "block"'):
        admit_vcs_url(PINNED_URL, _documented_default())
