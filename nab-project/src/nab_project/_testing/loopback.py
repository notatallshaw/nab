"""Run local HTTP servers for transport and build integration tests."""

from __future__ import annotations

from contextlib import contextmanager
from functools import partial
from threading import Thread
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from collections.abc import Generator
    from http.server import ThreadingHTTPServer


_Server = TypeVar("_Server", bound="ThreadingHTTPServer")


@contextmanager
def running_server(server: _Server) -> Generator[_Server, None, None]:
    """Serve requests in a background thread until the test leaves the context."""
    with server:
        # A short poll keeps shutdown from delaying every test by half a second.
        thread = Thread(
            target=partial(server.serve_forever, poll_interval=0.01), daemon=True
        )
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join()
