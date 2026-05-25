"""TLS hardening shared by the urllib3 and niquests transports.

urllib3-future only emits an ``InsecureRequestWarning`` and then proceeds
when a connection could not be verified.  nab must never send an
unverified HTTPS request, so promote that warning to a hard error rather
than let it become a silent downgrade.
"""

from __future__ import annotations

import warnings

import urllib3.exceptions


def forbid_unverified_https() -> None:
    """Make an unverified HTTPS request raise instead of warn-and-proceed."""
    warnings.filterwarnings("error", category=urllib3.exceptions.InsecureRequestWarning)
