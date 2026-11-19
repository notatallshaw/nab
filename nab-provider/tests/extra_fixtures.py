"""Shared target and wheel metadata for provider extra tests."""

from nab_provider.target import ResolveTarget
from nab_provider.testing import make_metadata

PYTHON_312 = ResolveTarget.for_host_python("3.12.0")
SECURITY_EXTRA_METADATA = make_metadata(
    "foo",
    "1.0",
    "bar>=1.0",
    'cryptography>=2.0; extra == "security"',
    provides_extra=("security",),
)
