"""Small archive and local Simple API builders for real CLI resolves."""

from __future__ import annotations

import hashlib
import io
import zipfile
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


def make_wheel_archive(name: str, version: str, metadata: str, tag: str) -> bytes:
    """Build a wheel whose metadata can be read through the local index."""
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        directory = f"{name}-{version}.dist-info"
        archive.writestr(f"{directory}/METADATA", metadata + "\n")
        archive.writestr(
            f"{directory}/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: false\n" + f"Tag: {tag}\n",
        )
        archive.writestr(f"{directory}/RECORD", "")
    return content.getvalue()


def publish_artifacts(index: Path, name: str, artifacts: Mapping[str, bytes]) -> None:
    """Write a hashed Simple API page and the artifacts it names."""
    directory = index / name
    directory.mkdir(parents=True, exist_ok=True)
    links = []
    for filename, payload in artifacts.items():
        (directory / filename).write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        links.append(f'<a href="{filename}#sha256={digest}">{filename}</a>\n')
    (directory / "index.html").write_text("".join(links), encoding="utf-8")
