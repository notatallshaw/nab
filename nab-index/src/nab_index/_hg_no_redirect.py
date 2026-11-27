"""Mercurial extension loaded only by nab's HTTPS pull subprocess."""

from __future__ import annotations

from importlib import import_module
from typing import NoReturn
from urllib.error import URLError
from urllib.request import BaseHandler, HTTPRedirectHandler

cmdtable: dict[bytes, object] = {}


class _RefuseRedirect(BaseHandler):
    # Handle redirects before urllib's standard redirect handler sees them.
    handler_order = HTTPRedirectHandler.handler_order - 1

    def http_error_302(self, *_args: object) -> NoReturn:
        """Stop before opening the redirected URL."""
        msg = "Mercurial HTTPS redirects are disabled"
        raise URLError(msg)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def _redirect_handler(_ui: object, _password_manager: object) -> BaseHandler:
    """Supply an opener handler for redirect responses."""
    return _RefuseRedirect()


def extsetup(_ui: object) -> None:
    """Register the pull command only after installing the redirect policy."""
    url = import_module("mercurial.url")
    commands = import_module("mercurial.commands")
    url.handlerfuncs.append(_redirect_handler)
    cmdtable[b"nab-pull"] = commands.table[b"pull"]
