# Output formats

What each `nab lock` format writes, where `--output` sends it, and how a
universal resolve represents its targets. See the [CLI](cli.md) for the
command and [Use a lock](../how-to/use-the-lock.md) for installation.

## `--format`

Three formats:

* `--format pylock` (default) writes a [PEP 751] `pylock.toml`.
* `--format requirements` writes a pip-compatible `requirements.txt`, sorted
  by name. An index pin is `name==version` with recorded `--hash` lines;
  local, VCS, and archive pins render as URLs.
* `--format requirements-without-hashes` writes the same output without
  separate `--hash` lines. An index pin is bare `name==version`; an archive
  URL still carries its digest.

## `--output`

`--output` defaults to `pylock.toml` for `pylock` and `requirements.txt`
for the requirements formats. With `--build-requirements`, the defaults are
`pylock.build.toml` and `build-requirements.txt`. Pass `--output -` for stdout.

A universal requirements resolve has no default file because one pip
requirements file cannot represent every target. It prints to stdout unless
`--output` names a template.

A requirements `--output` template writes one file per target. The
variables are `{python_version}`, `{platform_id}`, and `{selection}`,
which names the conflict fork a target belongs to (`extra-cpu`,
`group-black22.group-isort5`, empty when the resolve did not fork). The
status line labels each target as a `tuple`:

```console
$ nab lock --format requirements-without-hashes --extras cpu gpu \
    --output 'req-{python_version}-{selection}.txt'
Wrote req-3.12-extra-cpu.txt (12 packages, tuple py312-linux_x86_64-extra-cpu)
Wrote req-3.12-extra-gpu.txt (14 packages, tuple py312-linux_x86_64-extra-gpu)
```

A template that maps two targets onto one path is rejected. The error names
variables that can separate them. If no variable names the differing axis,
such as libc or implementation, use pylock output instead.

## Universal output

Universal mode (`[tool.nab].mode = "universal"`) is supported for
all three formats:

* `pylock` produces one PEP 751 file. Pins shared by every target appear once;
  divergent pins use environment markers.

  A minor split at a micro boundary contributes one entry per slice. See
  [Lockfiles](lockfile.md) and [Universal resolution](../explanation/universal.md).
* `requirements` and `requirements-without-hashes` emit a `# label` block per
  target, followed by that target's pins. pip cannot use the combined output
  as one hash-checked requirements file; use an output template to create one
  file per target.

A failed target renders as `# {label}: FAILED`, followed by the commented,
indented error. The command exits `1`.

[PEP 751]: https://peps.python.org/pep-0751/
