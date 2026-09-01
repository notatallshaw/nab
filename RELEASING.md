# Releasing

All six packages release together at one version (lockstep).

## Cut a release

1. From an up-to-date, clean `main`, run:

   ```
   hatch run release:make 0.0.3
   ```

   This creates a `release/0.0.3` branch with two commits (the `0.0.3` release,
   then a return to `0.0.4.dev0`), tags the release commit `v0.0.3`, pushes the
   branch and tag, and prints a link to open the PR.

2. Open the PR from that link and review the version bump.

3. Merge it with a merge commit, not a squash, so the `0.0.3` commit and the tag
   stay in history.

4. Draft a GitHub release for the `v0.0.3` tag, click "Generate release notes",
   and publish it. That triggers the release workflow, which builds and validates
   all six packages and publishes them to PyPI via trusted publishing. Approve
   the `pypi` deployment in the Actions tab when prompted.

## Notes

- `nab-markersets` has no PyPI project yet. Register a pending publisher for
  `notatallshaw/nab`, `release.yml` and the `pypi` environment right before
  cutting the release that first ships it; PyPI allows one pending publisher
  per repository, workflow and environment, so it cannot be set up early.
  Without it the publish job stops at that package, having already published
  the ones before it.
- Prereleases work: `hatch run release:make 0.0.3rc1`.
- The changelog is the generated GitHub release notes (a list of merged PRs).
- The workflow runs `tasks/release.py check` and `tasks/build_dists.py`, and
  installs only from hash-pinned locks: `pylock.release.toml` for the
  toolchain, plus `pylock.build.toml` for the backend, which the build needs
  because it runs with `--no-isolation`.
- After changing a dependency group, regenerate the locks with
  `tasks/refresh-locks.sh`.
- To abort before publishing, delete the branch and the tag.
