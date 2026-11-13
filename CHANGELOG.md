# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Auto-archive: memories and projects unused for 30 days are archived (checked when the store opens, at most every 12 hours, or by hand with `tether sweep [--dry-run]`). Asking for one again (recall, `get`, opening its project or folder) restores it, and it is archived again after 7 days of no use. Archiving by hand is never undone automatically. Tune with `archive_after_days` and `rearchive_after_days` in the config; `archive_after_days: 0` turns it off.
- Live view focus: the browser view shows every active project by default; "Focus on this" on a project narrows the sidebar and overview to it (remembered in the browser), and "Focused · show all" undoes it. `tether watch -p <slug>` does the same in the terminal.
- Schema v5: a local, unsynced `usage` table.
- Ranked full-text search (SQLite FTS5, schema v6) for `recall` and `search`, with the old substring match as a top-up and as the fallback on SQLite builds without FTS5.
- `tether doctor` reports database, reference, archive-flag, search-index and sync-journal health; `doctor --fix` rebuilds the index and this Mac's journal.
- `tether export` / `tether import`: a private JSON backup and a restore into an empty store.
- `tether archived` and `tether restore`; an Archive page in the live view with Restore buttons.
- Live view keyboard shortcuts (`/`, `c`, `g` then `o`/`i`/`a`, `f`, `?`).
- `tether install` ends with an end-to-end check and numbered next steps (`--no-check` skips it).
- Plain-language errors for a busy, damaged or read-only ledger and for permission problems (`TETHER_DEBUG=1` shows the traceback).
- `docs/TOOLS.md`, the MCP tool reference generated from the code, with a test that keeps it current.
- `SECURITY.md`, Dependabot, a lockfile, and signed build provenance on release artifacts.

### Changed

- Automatic GitHub sync: the live view (including the always-on login service) runs `tether github` itself, 30 seconds after it starts and then every 3 hours, so about eight times a day, with no cron job. Change it with `github_sync_hours` in the config (0 turns it off). A failed run is retried within 15 minutes. The sidebar shows "GitHub synced 12m ago" (or why it failed), and clicking it syncs now.
- Edit activity from Claude Code: a new `post-edit` hook (installed by `tether install`) notes when Code edits files in a bound project. The first edit of a session is logged at once and later ones are batched at most every 5 minutes, naming files only (never contents). The project shows "Code working" for 10 minutes after an edit.
- `tether github`: reads GitHub through the `gh` CLI and keeps every bound project accurate. Status comes from the latest commit on GitHub plus whether your clone is behind, ahead or missing the branch; the brief records the repo, visibility and open issue and PR counts. Tasks are managed for unpushed commits, a branch that exists only on your Mac, clones 3 or more commits behind, and open issues and pull requests (all of them in repos you can write to, only your own elsewhere). It is safe to re-run, closes what GitHub resolved, keeps tasks you dropped dropped, and never writes to GitHub or your repositories.
- New logo, "one thread": a single continuous loop with an amber knot where it crosses. It is in the live view, the browser tab icon, the README banner and the Desktop extension (`extension/icon.png`, rendered from `docs/readme/logo.svg` by `scripts/render_logo.py`).
- The live view overview is tighter: projects with open work are cards that show what is in progress or next, the status, open and P1 counts, handoffs and last activity, ranked by what needs attention; quiet projects fold into a compact list. The sidebar does the same. Each project page gets a facts strip (open, in progress, P1, blocked, done, handoffs, decisions, ideas, last active).
- The store is now a package of focused modules; imports and behavior are unchanged.
- CI gains lint (ruff), type (mypy) and coverage gates, a lockfile check and a browser-test job.
- A clipped last line in this Mac's sync journal is healed before the next write instead of swallowing it.
- The live view reads a refused write's body before answering, so a 403 is no longer a reset connection.

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
