"""PEP 508 marker algebra: a marker as the set of environments it selects.

The supported API is the module paths below. They will not move without a major
version bump. Everything else in the package is internal and may be renamed or
relocated in any release.

    nab_markersets.errors       IntractableMarkerSet, UnserializableMarkerSet
    nab_markersets.markersets   DecisionStore, MarkerSet, variable_names

The package root binds no names, so importing ``nab_markersets`` pulls in no
submodules and a caller loads only what it imports.
"""
