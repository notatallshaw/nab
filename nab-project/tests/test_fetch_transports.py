"""Exercise the project fetch coordinator with the index transports."""

from __future__ import annotations

import hashlib
import tarfile
from collections.abc import Callable
from pathlib import Path

import pytest
import respx

from nab_index.httpx2_async_transport import Httpx2AsyncTransport
from nab_index.httpx_async_transport import HttpxAsyncTransport
from nab_index.transport import AsyncHttpTransport
from nab_index.urllib3_async_transport import Urllib3AsyncTransport
from nab_project.fetch import FetchCoordinator


class TestFetchCoordinatorTransport:
    @respx.mock
    def test_explicit_transport(self) -> None:
        """Coordinator routes fetches through the injected transport."""
        route = respx.get("https://pypi.org/simple/pkg/").respond(json={"files": []})
        with FetchCoordinator(transport=HttpxAsyncTransport()) as coord:
            event = coord.request_listing("pkg")
            assert event.wait(timeout=5)
            assert coord.index.get_listing("pkg") is not None
        assert route.call_count == 1


class TestArtifactContentEncoding:
    @pytest.mark.parametrize(
        "make_transport",
        [
            pytest.param(Urllib3AsyncTransport, id="urllib3"),
            pytest.param(lambda: HttpxAsyncTransport(http2=False), id="httpx"),
            pytest.param(lambda: Httpx2AsyncTransport(http2=False), id="httpx2"),
        ],
    )
    def test_direct_archive_keeps_the_served_bytes(
        self,
        tmp_path: Path,
        make_transport: Callable[[], AsyncHttpTransport],
        gzip_artifact_server: tuple[str, list[str | None]],
    ) -> None:
        metadata = tmp_path / "PKG-INFO"
        metadata.write_text("Metadata-Version: 2.2\nName: demo\nVersion: 1.0\n")
        path = tmp_path / "demo-1.0.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            archive.add(metadata, arcname="demo-1.0/PKG-INFO")
        body = path.read_bytes()
        digest = hashlib.sha256(body).hexdigest()

        base_url, encodings = gzip_artifact_server
        with FetchCoordinator(transport=make_transport()) as coord:
            ready = coord.request_direct_archive("demo", digest, base_url + path.name)
            assert ready.wait(timeout=10)
            assert coord.index.get_sdist_archive("demo", digest) == body

        assert encodings == ["identity"]
