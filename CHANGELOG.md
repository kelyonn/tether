# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [0.4.0] - 2026-11-12

### Added

- Packaged as `tether-mcp` for PyPI.
- One-click Claude Desktop extension bundle (`dist/tether.mcpb`) built by `scripts/build_extension.py`.

### Changed

- Refreshed the plan and README.

## [0.3.0] - 2026-11-10

### Changed

- Tightened the idea-dedupe and `start_project` rules in the server instructions.
- Bundled the rule-eval scenarios inside the package.
- Rewrote the README pitch.

## [0.2.0] - 2026-11-10

### Added

- Rule-eval kit for checking how well models follow the Tether rules.
- NVIDIA / OpenAI-compatible provider for the rule eval, with retries on timeouts.
- Service install command.
- CI workflow running the test suite.

### Changed

- Journal sync, UI and server improvements.

## [0.1.0] - 2026-11-09

### Added

- Initial release: shared memory and project ledger (goal, status, decisions, tasks, handoffs) for every Claude surface.
- MCP server, CLI, local web UI, installer, git linking, journal sync and rendered project files.
