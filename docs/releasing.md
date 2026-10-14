# NetVault Release Guide

NetVault uses one version for the CLI, server, rendered web UI, static asset URLs,
Python modules, lockfiles, changelog, Git tag, and GitHub Release. CI rejects a
commit when tracked version sources differ or web guide commands/copy buttons
disagree with the CLI. Web versions come directly from the server package.

## Prepare a release

1. Update `project.version` in the root and server `pyproject.toml` files.
2. Update `__version__` in both package `__init__.py` files.
3. Add the matching release section at the top of `CHANGELOG.md`.
4. Run `uv lock` and `uv lock --project packages/netvault-server`.
5. Verify the result:

```bash
python scripts/check-version-consistency.py --tag vX.Y.Z
python scripts/sync-identity-contract.py --check
```

Commit the complete release with the exact subject:

```text
release: NetVault X.Y.Z
```

Push that commit to `main`. The Release workflow independently verifies all version
sources, lockfiles, lint, tests, coverage, and both package builds. It then creates an
annotated `vX.Y.Z` tag and a GitHub Release using the matching changelog section. The
CLI and server source distributions and wheels are attached to the release.

GitHub Actions publishes the packages. It does not deploy the web service.
Update CLI help, README, user guide and `/web/cli` examples together when behavior
changes. Keep the normal workflow compact and optional details collapsed. Existing
commands and compatible aliases remain unchanged.

## Deploy the matching web service directly on the VPS

Before reporting a release as fully upgraded, update the clean production checkout
at `/root/iiaide/netvault` to the same release commit. Inspect current state and
back up data using the existing backup procedure. Rebuild and restart only the
server, preserving `.env`, PostgreSQL and the PDF storage volume:

```bash
docker compose -f docker-compose.iiaide.yml build server
docker compose -f docker-compose.iiaide.yml up -d --no-deps --wait server
python3 scripts/verify-deployed-release.py https://iiaide.com/nv X.Y.Z
```

Verify `nv --version`, the OpenAPI version, the web tooltip/help version and CSS/JS
asset URLs all match. Check `/web/cli` and its copy buttons against current help.
A successful GitHub Release alone does not confirm the running web version.
Rendered HTML is not cached; asset URLs use the package version. When the next
version is bumped, deploy that server package as part of the same release.

Do not manually publish a release tag before this workflow. Rerunning the workflow is
safe only when an existing tag already points to the same release commit.

For identity/schema changes, run one isolated PostgreSQL contract check in addition
to the relevant test suite. The database must end in `_test`; the check creates
and removes only its own schemas. A production upgrade needs a database/source
backup and a complete stop of old writers before the locks-v2 service starts.
