"""Install the native lockfile readers used by the integration tests."""

from __future__ import annotations

import os
import subprocess
import sys
import venv
from pathlib import Path

LOCKS = Path(__file__).resolve().parents[1] / ".github/requirements/native-locks"


def install(destination: Path) -> None:
    """Install each tool from its plan's lock into a separate environment."""
    for tool in ("pdm", "poetry", "uv"):
        environment = destination / tool
        venv.EnvBuilder(symlinks=os.name != "nt").create(environment)

        # The runner's pip can read pylock even when the new environment has no pip.
        lockfile = LOCKS / f"pylock.{tool}.toml"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "--python",
                str(environment),
                "install",
                "-r",
                str(lockfile),
            ],
            check=True,
        )


if __name__ == "__main__":
    install(Path(sys.argv[1]))
