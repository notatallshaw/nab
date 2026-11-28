# nab-candidates

Use `nab-candidates` when your installer already finds package files and reads their metadata. You
supply the available files in preference order and the dependencies each file declares. The library
selects one candidate per required package so that their dependencies fit together.

It also checks when a yanked file can be used. For example, if the only wheel for `lib==2.0` has
been yanked, a requirement of `lib>=2` alone cannot authorize that wheel. An exact pin such as
`lib==2.0` from the user or a selected dependency can. If the search selects that wheel, the result
tells you where the pin came from.

Nab uses this search when locking a project that may need a yanked file. See the
[guide to resolving package files](https://nab.readthedocs.io/en/latest/how-to/embed-the-resolver.html#resolve-python-package-files)
to use it with your own installer.
