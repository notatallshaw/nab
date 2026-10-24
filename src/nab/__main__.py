"""Allow ``python -m nab ...`` to drive the CLI."""

__lazy_modules__ = ("nab._entry",)

from ._entry import console_entry

if __name__ == "__main__":
    console_entry()
