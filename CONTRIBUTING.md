# Contributing

## Dev setup

```sh
python -m venv .venv && source .venv/bin/activate
python -m pip install -e ".[dev]"
```

Python 3.10 or newer.

## Tests

```sh
ruff check src tests scripts     # lint
mypy                             # types
python -m pytest -q --cov        # tests; fails under the coverage floor
python -m pytest tests/e2e       # the live view in a browser: pip install -e ".[e2e]" first
python -m tether doctor --deep   # real server over stdio
python scripts/gen_tool_docs.py  # regenerate docs/TOOLS.md after changing an MCP tool
```

The store lives in `src/tether/store/`, one module per area (schema, core, projects, records, memories, archive, search). New behavior needs a test; sync changes need a case in `tests/test_sync_faults.py` or the fuzz test.

Tests isolate `HOME` and `TETHER_HOME`, so they never touch your real `~/.tether`. CI runs the same on Python 3.10 to 3.13.

## Releasing

Short version (details in [docs/RELEASING.md](docs/RELEASING.md)):

1. `python scripts/bump_version.py X.Y.Z` and fill in the new `CHANGELOG.md` section.
2. Commit, push to `main`, wait for CI.
3. `git tag vX.Y.Z && git push origin vX.Y.Z`. The release workflow tests, builds, publishes to PyPI and creates the GitHub Release.
