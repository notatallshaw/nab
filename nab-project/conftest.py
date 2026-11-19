"""HTTP fixtures for the project build and fetch tests.

The distribution root keeps these fixtures distinct from the CLI suite's
``tests.conftest`` module during joint test collection.
"""

# ruff: noqa: INP001 - Pytest discovers this module at the distribution root.
from __future__ import annotations

from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path


@contextmanager
def _serve_directory(
    root: Path, *, content_encoding: str | None = None
) -> Generator[tuple[str, list[str | None]], None, None]:
    """Yield the base URL and record the content encodings requested by clients."""
    root.mkdir(exist_ok=True)
    encodings: list[str | None] = []

    class Handler(SimpleHTTPRequestHandler):
        def end_headers(self) -> None:
            encodings.append(self.headers.get("Accept-Encoding"))
            if content_encoding is not None:
                self.send_header("Content-Encoding", content_encoding)
            super().end_headers()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

    handler = partial(Handler, directory=str(root))
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = Thread(
            target=partial(server.serve_forever, poll_interval=0.01), daemon=True
        )
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/", encodings
        finally:
            server.shutdown()
            thread.join()


@pytest.fixture
def build_index_url(tmp_path: Path) -> Generator[str, None, None]:
    """Serve the build test's local index directory."""
    with _serve_directory(tmp_path / "index") as (url, _):
        yield url


@pytest.fixture
def gzip_artifact_server(
    tmp_path: Path,
) -> Generator[tuple[str, list[str | None]], None, None]:
    """Serve the test archive with a gzip content coding."""
    with _serve_directory(tmp_path, content_encoding="gzip") as server:
        yield server
