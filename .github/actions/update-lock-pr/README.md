# Publish dependency locks

Use this action after checking out a project and refreshing `pylock.dev.toml` and
`.github/requirements/pylock.build.toml`. It publishes changed locks to `ci/update-locks` and opens
or updates a draft pull request. Changes limited to refresh timestamps and command provenance are
ignored.

The job needs `contents: write` and `pull-requests: write`. The runner needs Git, the GitHub CLI and
Python 3.11 or newer. Check out the project with `persist-credentials: false`; the action uses its
token only for GitHub requests and the branch push.

`working-directory` selects a checkout relative to the workspace. `token` defaults to the job's
GitHub token. The `changed` output is `true` when an update was published, so a separate job with
`actions: write` can dispatch checks for `ci/update-locks`.

GitHub signs the update commit. Publication verifies its signature and refuses to overwrite a branch
that changed concurrently.
