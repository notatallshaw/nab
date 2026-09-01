"""nab's configuration layer: what a run is configured with, and from where.

Four modules, each importing only the ones above it.  :mod:`nab.optiondefs`
sits between the second and the third: it declares every option and names the
parse and render each one uses, and the ladder's ``OPTIONS`` is the half of
that declaration a configuration source may set.

* :mod:`~nab.config.values` parses the value a key carries.
* :mod:`~nab.config.hooks` renders a merged value back, and parses the few
  rows that need parse state a ``(value, where)`` pair cannot carry.
* :mod:`~nab.config.ladder` holds the rows, finds the sources that bind
  them, merges them, and prints the result for ``nab config``.
* :mod:`~nab.config.model` reads ``[tool.nab]`` into the project config the
  commands hand to nab-project.

Import the module that holds what you need; this package imports none of them.
"""
