"""Project lockfile writers for native package managers."""

from ._lockfile.pdm import write_pdm_lock

__all__ = ["write_pdm_lock"]
