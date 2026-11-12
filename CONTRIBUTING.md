# Contributing

## Dev setup

```sh
python -m venv .venv && source .venv/bin/activate
python -m pip install -e ".[dev]"
```

Python 3.10 or newer.

## Tests

```sh
python -m pytest -q
python -m tether doctor --deep   # real server over stdio
```

Tests isolate `HOME` and `TETHER_HOME`, so they never touch your real `~/.tether`. CI runs the same on Python 3.10 to 3.13.

## Releasing

Short version (details in [docs/RELEASING.md](docs/RELEASING.md)):

1. `python scripts/bump_version.py X.Y.Z` and fill in the new `CHANGELOG.md` section.
2. Commit, push to `main`, wait for CI.
3. `git tag vX.Y.Z && git push origin vX.Y.Z`. The release workflow tests, builds, publishes to PyPI and creates the GitHub Release.
