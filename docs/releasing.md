# NetVault Release Guide

NetVault uses one version for the CLI, server, Python modules, lockfiles, changelog,
Git tag, and GitHub Release. CI rejects a commit when any tracked version source differs.

## Prepare a release

1. Update `project.version` in the root and server `pyproject.toml` files.
2. Update `__version__` in both package `__init__.py` files.
3. Add the matching release section at the top of `CHANGELOG.md`.
4. Run `uv lock` and `uv lock --project packages/netvault-server`.
5. Verify the result:

```bash
python scripts/check-version-consistency.py --tag vX.Y.Z
```

Commit the complete release with the exact subject:

```text
release: NetVault X.Y.Z
```

Push that commit to `main`. The Release workflow independently verifies all version
sources, lockfiles, lint, tests, coverage, and both package builds. It then creates an
annotated `vX.Y.Z` tag and a GitHub Release using the matching changelog section. The
CLI and server source distributions and wheels are attached to the release.

Do not manually publish a release tag before this workflow. Rerunning the workflow is
safe only when an existing tag already points to the same release commit.
