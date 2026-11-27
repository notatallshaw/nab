# nab-candidates

Resolve Python package files supplied by a host. The host orders the files and prepares their
metadata. The candidate search checks their dependency ranges, exact file requirements and the pins
that permit a yanked release.

Nab uses this library when its index-backed provider needs file-level admission. An installer with
its own finder can use the same search without installing nab's index client or project layer.
