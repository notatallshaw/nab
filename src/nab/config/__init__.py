"""nab's configuration layer: what a run is configured with, and from where.

Six modules, each importing only the ones above it.  :mod:`nab.optiondefs`
sits between the second and the third: it declares every option and names the
hooks each one parses and renders with, and the registry is the half of that
declaration a configuration source may set.

* :mod:`~nab.config.values` parses the value a key carries.
* :mod:`~nab.config.hooks` binds those parsers to a row, and renders back.
* :mod:`~nab.config.registry` holds those rows, and the value types the ladder
  carries them in.
* :mod:`~nab.config.layers` finds the sources, gates them and merges them.
* :mod:`~nab.config.inspect` prints the result for ``nab config``.
* :mod:`~nab.config.model` reads ``[tool.nab]`` into the project config the
  commands hand to nab-project.

Import the module that holds what you need; this package imports none of them.
"""
