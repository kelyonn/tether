# Releasing

Releases are tag-driven. Pushing a tag `vX.Y.Z` runs `.github/workflows/release.yml`, which checks the tag against `pyproject.toml` and `src/tether/__init__.py`, runs the tests, builds the wheel, sdist and `dist/tether.mcpb`, publishes to PyPI via trusted publishing (no tokens), and creates a GitHub Release with the changelog notes.

## One-time setup (human)

1. **Push the repo** to GitHub as `kelyonn/tether` (if not already): `git remote add origin git@github.com:kelyonn/tether.git && git push -u origin main`.
2. **Create the GitHub environment.** Repo, Settings, Environments, New environment, name it exactly `pypi`. Optionally add yourself as a required reviewer so each publish needs a click.
3. **Register a pending trusted publisher on PyPI** (the project does not exist yet, so use the pending flow):
   - Sign in at https://pypi.org, then Account settings, Publishing, "Add a new pending publisher".
   - PyPI project name: `tether-mcp`
   - Owner: `kelyonn`
   - Repository name: `tether`
   - Workflow name: `release.yml`
   - Environment name: `pypi`
   - The first successful publish creates the project and turns the pending publisher into a normal one. Do the PyPI registration before the first tag; a pending publisher does not reserve the name forever, so don't wait long.
4. **Workflow permissions.** Repo, Settings, Actions, General: leave defaults. The workflow requests `contents: write` (GitHub Release) and `id-token: write` (PyPI OIDC) itself.
5. Optional: add a tag protection or ruleset for `v*` so only maintainers can tag.

No PyPI API token or repository secret is needed.

## Per release

1. Make sure `main` is green.
2. `python scripts/bump_version.py X.Y.Z` (updates `pyproject.toml`, `src/tether/__init__.py`, stubs a CHANGELOG section).
3. Edit the new section in `CHANGELOG.md`; the release notes are taken from it verbatim.
4. `python -m pytest -q`, then commit: `git commit -am "Release X.Y.Z"`.
5. `git push origin main`, wait for CI.
6. `git tag vX.Y.Z && git push origin vX.Y.Z`.
7. Watch the Actions run. If you configured a reviewer on the `pypi` environment, approve the publish.
8. Verify: https://pypi.org/project/tether-mcp/, `uvx tether-mcp --help`, and the GitHub Release has the wheel, sdist and `tether.mcpb`.

## If something fails

- Version mismatch: delete the tag (`git tag -d vX.Y.Z && git push origin :refs/tags/vX.Y.Z`), fix, re-tag.
- PyPI never accepts the same version twice. If publish succeeded but a later step failed, fix forward with a new patch version, or re-run only the failed job.
- `invalid-publisher` from PyPI: the workflow name, environment or repo owner on the PyPI side doesn't exactly match.
