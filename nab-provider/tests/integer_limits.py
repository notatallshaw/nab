"""Decimal strings at and beyond the interpreter's integer conversion limit."""

import pytest

from nab_markersets._compat import get_int_max_str_digits

_DIGIT_LIMIT = get_int_max_str_digits()
requires_int_limit = pytest.mark.skipif(
    _DIGIT_LIMIT == 0, reason="requires an enabled integer conversion limit"
)
AT_DIGIT_LIMIT = "1" * (_DIGIT_LIMIT or 4300)
OVER_DIGIT_LIMIT = AT_DIGIT_LIMIT + "1"
