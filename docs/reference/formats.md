# Output formats

What each `nab lock` format writes, where `--output` sends it, and how a
universal resolve renders across the matrix.

`nab lock` itself is on the [CLI](cli.md) page. A matrix expands into
one resolve target, called a tuple below, per
`(python, platform, implementation)` point it names; see
[Universal resolution](../explanation/universal.md).

## `--format`

Three formats:

* `--format pylock` (default) writes a [PEP 751] `pylock.toml`.
* `--format requirements` writes a pip-compatible
  `requirements.txt`, sorted by name. An index pin is
  `name==version` with one `--hash=<algo>:<digest>` line per
  recorded digest; a local, VCS or archive pin renders as a URL
  line.
* `--format requirements-without-hashes` writes the same output
  with the `--hash` lines dropped, so an index pin is a bare
  `name==version`. Local, VCS and archive pins are unchanged, so
  an archive pin still carries its digest in the URL fragment.

## `--output`

`--output` defaults to `pylock.toml` for `pylock` and
`requirements.txt` for the two requirements formats, or to
`pylock.build.toml` and `build-requirements.txt` under
`--build-requirements`. Pass
`--output -` to write to stdout instead. A matrix has no default
requirements file: no one file can carry every tuple's pins (see
below), so the requirements formats print to stdout unless `--output`
names a template.

A requirements `--output` template writes one file per tuple. The
variables are `{python_version}`, `{platform_id}`, and `{selection}`,
which names the conflict fork a tuple belongs to (`extra-cpu`,
`group-black22.group-isort5`, empty when the resolve did not fork):

```console
$ nab lock --format requirements-without-hashes --extras cpu gpu \
    --output 'req-{python_version}-{selection}.txt'
Wrote req-3.12-extra-cpu.txt (12 packages, tuple py312-linux_x86_64-extra-cpu)
Wrote req-3.12-extra-gpu.txt (14 packages, tuple py312-linux_x86_64-extra-gpu)
```

A template that maps two tuples onto one path is rejected, naming the
variables that would give every tuple its own file. When the tuples
split on an axis no variable names, such as the libc or the
implementation, no template separates them and the message points at
pylock output instead.

## Universal output

Universal mode (`[tool.nab].mode = "universal"`) is supported for
all three formats:

* `pylock` produces one PEP 751 file with per-tuple `Package`
  entries gated by markers (`python_version`, `sys_platform`,
  `platform_machine`, plus `implementation_name` when the matrix
  declares a non-CPython implementation or more than one). Versions
  agreed across every tuple appear once without a marker; divergent
  versions appear once per `(version, source)` group with the
  matching tuples disjoined.
* `requirements` and `requirements-without-hashes` emit a
  sequence of `# label` comment blocks, one per
  `(python, platform, implementation)` tuple, followed by that
  tuple's pins. Pip's hash-checking mode cannot install a single
  requirements.txt across multiple tuples, so the per-tuple block
  format is for inspection or for tools that consume one block at
  a time.

Failed tuples render as `# {label}: FAILED` followed by the error,
every line of it commented and indented, and exit `1`.

[PEP 751]: https://peps.python.org/pep-0751/
