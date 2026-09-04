"""nab's configuration layer: what a run is configured with, and from where.

Six modules, each importing only the ones above it.

* :mod:`~nab.config.values` parses the value a key carries.
* :mod:`~nab.config.hooks` renders a merged value back, and parses the few
  rows that need parse state a ``(value, where)`` pair cannot carry.
* :mod:`~nab.config.registry` holds the rows the layer reads, written by
  ``tasks/gen_cli.py`` from the declaration in :mod:`nab.optiontable`.
* :mod:`~nab.config.subflags` assembles the flags that spell one key each
  of a configuration table.
* :mod:`~nab.config.ladder` finds the sources that bind those rows, merges
  them, and prints the result for ``nab config``.
* :mod:`~nab.config.model` reads ``[tool.nab]`` into the project config the
  commands hand to nab-project.

Import the module that holds what you need; this package imports none of them.
"""
