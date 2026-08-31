"""Allow ``python -m nab ...`` to drive the CLI."""

from ._entry import console_entry

if __name__ == "__main__":
    console_entry()
