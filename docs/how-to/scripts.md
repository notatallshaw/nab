# Lock a Python script

Use `--script` to read a local Python file's [PEP 723 metadata]. Nab reads its dependencies and
Python requirement without executing or editing the script.

```python
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx", "rich"]
# [tool.nab]
# dist-policy = "wheel-only"
# uploaded-prior-to = "P7D"
# ///

import httpx
from rich import print

print(httpx.get("https://example.com").status_code)
```

Save this as `fetch.py` and generate its lock:

```bash
nab lock --script fetch.py
```

This writes `pylock.fetch.toml` beside the script. The default target is the Python and platform
running Nab. Select another Python on that platform with `--python 3.11`, or declare a
[target matrix] in the script's `[tool.nab.matrix]` table.

Install the locked dependencies in a matching virtual environment using pip:

```bash
python -m pip install -r pylock.fetch.toml
python fetch.py
```

## Check and update the lock

Check that the saved solution still satisfies the script's declarations and policies:

```bash
nab lock --script fetch.py --locked
```

A missing lock or an incompatible dependency, policy or local-source change fails the check. A
change to the Python body alone does not change the dependency inputs. `--reproduce` resolves again
and compares the result instead; `--upgrade` moves a relative upload cutoff to the current time.

Use `--output` to choose another PEP 751 filename, or `--output -` to write the generated lock to
standard output. A script with a dotted stem such as `fetch.data.py` needs an explicit output such
as `--output pylock.fetch-data.toml`.

The existing [output formats] also apply to scripts:

```bash
nab lock --script fetch.py --format requirements --output requirements.txt
nab lock --script fetch.py --format requirements-without-hashes --output pins.txt
```

## Configure and download script dependencies

The script's `[tool.nab]` supports the same target, index, source and package policies as a project.
Relative `file:` index URLs and local-source paths are anchored at the script's directory. System
and user settings still apply; a `nab.toml` beside the script supplies directory-level
configuration.

An enclosing `pyproject.toml` does not contribute dependencies, groups, workspaces or targets to the
script. Script inputs have no project groups, optional project extras or build requirements. The
script's `requires-python` remains a compatibility bound when tool settings select the targets.

Inspect the script's configuration or download its selected artifacts:

```bash
nab config explain dist-policy --script fetch.py
nab download --script fetch.py --output wheels
```

Configuration reports show Nab settings. The standardized inline `requires-python` field also
constrains the selected targets.

`--script` accepts extensionless files and scripts with a shebang or Python encoding declaration. It
requires one complete metadata block. The standardized `dependencies` and `requires-python` fields
may be omitted; omitted dependencies mean an empty requirement list. Script URLs and standard input
are not accepted as local file inputs.

[output formats]: ../reference/formats.md
[pep 723 metadata]: https://packaging.python.org/en/latest/specifications/inline-script-metadata/
[target matrix]: ../reference/target-matrix.md
