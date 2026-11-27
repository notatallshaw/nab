# Output formats

What each `nab lock` format writes, where `--output` sends it, and how a universal resolve
represents its targets. See the [CLI](cli.md) for the command and
[Use a lock](../how-to/use-the-lock.md) for installation.

## `--format`

Choose the file type with `--format`:

- `--format pylock` (default) writes a [PEP 751] `pylock.toml`.
- `--format uv` writes `uv.lock` for `uv sync --frozen`. For a script, nab writes `SCRIPT.lock`
  beside the script, ready for `uv run --frozen --script SCRIPT`. For a package in an archive
  subdirectory, uv needs an HTTP or HTTPS archive URL.
- `--format pdm` writes `pdm.lock` for `pdm sync`. Pass the same groups and extras to nab that you
  want available to PDM.
- `--format poetry` writes `poetry.lock` for `poetry sync`. Configure custom package indexes in both
  nab and Poetry when Poetry needs to choose between distribution files. Poetry builds source
  archives from their root. For a package in an archive subdirectory, use PDM or publish an archive
  rooted at the package.
- `--format requirements` writes a pip-compatible `requirements.txt`, sorted by name. An index pin
  is `name==version` with recorded `--hash` lines; local, VCS, and archive pins render as URLs.
- `--format requirements-without-hashes` writes the same output without separate `--hash` lines. An
  index pin is bare `name==version`; an archive URL still carries its digest.

<a name="output"></a>

## `--output`

For project inputs, `--output` defaults to `pylock.toml` for `pylock` and `requirements.txt` for the
requirements formats. With `--build-requirements`, the defaults are `pylock.build.toml` and
`build-requirements.txt`. Pass `--output -` for stdout.

With `--script path/to/example.py`, the default lockfile is `path/to/pylock.example.toml`. For a
single target, the default requirements file is `path/to/example.py.requirements.txt`.

When a project or script resolves for multiple targets, both requirements formats print one block
per target to stdout by default. Use an output template to get a separate file for each target.

A requirements `--output` template writes one file per target. The variables are `{python_version}`,
`{platform_id}`, and `{selection}`, which names the conflict fork a target belongs to (`extra-cpu`,
`group-black22.group-isort5`, empty when the resolve did not fork). The status line labels each
target as a `tuple`:

```console
$ nab lock --format requirements-without-hashes --extras cpu gpu \
    --output 'req-{python_version}-{selection}.txt'
Wrote req-3.12-extra-cpu.txt (12 packages, tuple py312-linux_x86_64-extra-cpu)
Wrote req-3.12-extra-gpu.txt (14 packages, tuple py312-linux_x86_64-extra-gpu)
```

nab rejects a template when two targets would write to the same path. The error lists any template
variables that distinguish them. If the templates cannot distinguish the targets, use pylock output.

## Universal output

Universal mode (`[tool.nab].mode = "universal"`) is supported for each format:

- `pylock` produces one PEP 751 file. Pins shared by every target appear once; divergent pins use
  environment markers.

  A minor split at a micro boundary contributes one entry per slice. See [Lockfiles](lockfile.md)
  and [Universal resolution](../explanation/universal.md).

- `pdm` records the supported targets and attaches environment markers to packages that differ
  across them.

- When either requirements format prints multiple targets together, each target's pins follow a
  `# label` header. Use an output template to create separate requirements files for pip.

If any target fails, nab writes no lock or requirements output. The report goes to stderr and the
command exits `1`; see [Resolution failures](diagnostics.md).

Set `format` in `nab.toml` or `NAB_FORMAT` to choose a workflow default. `--format` overrides it.

[pep 751]: https://packaging.python.org/en/latest/specifications/pylock-toml/
