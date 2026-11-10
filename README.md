# Tether

**Ideate anywhere. Claude Code already knows.** Tether is a local memory and project ledger shared by every Claude surface. Talk through an idea in Claude on your phone or the web, and it is saved and sorted automatically. When you open Claude Code, it already has that idea, plus what is decided, what is done, what is next and who has the ball.

- **Memory:** ideas, research and specs saved from any chat, deduplicated and filed under a project when one fits.
- **Ledger:** each project's goal, status, decisions (that can be superseded), tasks tagged by the surface that should do them, and handoffs between surfaces.
- **Live view:** a local, editable page showing every project, what Code is doing now, the ideas inbox and the activity feed.
- **Two Macs:** an append-only journal in iCloud Drive keeps them in step.
- **Local and private:** one SQLite file, no cloud service, no AI of its own, and secrets are blocked from ever being stored.

Each Claude Code session starts with a short summary instead of the full history. On a synthetic 3-month project it is about 1.4k tokens, against about 12k for everything. Closed tasks and superseded decisions are not re-read, but stay searchable. Here is one:

```
# Shop · tether [shop]
Goal: Sell mugs online by December
Status: Product page designed; cart next (2h ago)
You are: code. Refs: t=task d=decision h=handoff.

## Handed to you (1)
- h3 from design (2h ago): Product page mockup
  Hero, gallery, reviews. Final copy in link.
  links: https://claude.ai/design/...
  tasks: t4

## Since code was last here
- 2h ago design: decided: Serif headings, mono prices
- 3h ago chat: added task: Annual pricing toggle

## Open tasks (5 open, 12 closed and hidden)
For code:
- t4 P1 Build product page
- t7 [blocked] Stripe webhook — waiting on account approval
For others:
- t9 (design) Empty cart state

## Decisions (6 active, newest first)
- d8 Shopify Storefront API, not custom backend — why: ship in weeks
```

## Install

```bash
uv tool install -e .        # from this folder; PyPI after launch
tether install              # registers with Claude Code (+ hook) and Claude Desktop
```

`tether install` merges only its own keys into your Claude config files and backs each file up first. Use `--dry-run` to preview the changes and `--uninstall` to remove them. Restart Claude Desktop afterwards. `tether doctor` checks everything.

## Check it works

```bash
tether doctor --deep    # starts the real server in a temp store and drives it end to end
```

It never touches your real data. Every line should say `ok`.

## Use

```bash
cd ~/code/shop
tether init --goal "Sell mugs online by December"   # binds this folder
claude                                                # the pack loads automatically
```

From there Claude does the bookkeeping. The server ships its rules:
- record decisions as they are settled
- add and tick tasks tagged with the surface that should do them
- hand work across surfaces
- update the status at milestones

In Claude Desktop or Cowork, say "load the shop project" once. Tether remembers it as the current project.

### Chat and Design (no MCP)

```bash
tether ctx --for chat --copy      # paste into claude.ai Chat or Design
# …work…  Claude ends with a TETHER UPDATE block; copy it, then:
tether apply --paste
```

Optionally, mirror every project as markdown into a folder those surfaces can read, such as Google Drive:

```bash
tether mirror --dir ~/Google\ Drive/My\ Drive/tether
```

### CLI

| | |
|---|---|
| `tether ls` | projects (`*` = this folder's / current) |
| `tether ctx [--for code\|chat\|design]` | the context pack |
| `tether tasks [--all] [--for code]` | tasks |
| `tether add "title" --for code -P 1` | new task |
| `tether start\|done\|block\|drop t12 ["note"]` | task status |
| `tether decide "text" --why "…" [--replaces d3]` | decision |
| `tether status "…"` · `tether goal "…"` · `tether brief spec.md` | project fields |
| `tether handoff design "title" --body "…" --link URL --task t4` | hand off |
| `tether inbox` · `tether accept h3` · `tether deliver h3` | handoffs |
| `tether search stripe` · `tether show d3` · `tether log` | history |
| `tether use shop` | current project for folder-less clients |
| `tether git install` | commits saying "fixes t12" / "closes t3, t4" mark those tasks done |
| `tether doctor [--deep]` | check the install; `--deep` runs an end-to-end self-test |

### MCP tools

`context`, `list_projects`, `start_project`, `update_project`, `record_decision`, `add_tasks`, `update_task`, `list_tasks`, `create_handoff`, `update_handoff`, `log_note`, `search`, `get`, plus the memory tools `remember`, `recall`, `recent_memories`, `attach_memory`.

No tool ever guesses a project: pass `project=<slug>` or run in a bound folder. A chat with no project uses `remember` and `recall`.

## Ideas, Code's tasks and the live view (v0.2)

```bash
tether remember "Reading app" --body "swipe to save quotes" --tags reading   # any project, or none
tether recall quotes            # search ideas + every project
tether inbox                    # handoffs + unfiled ideas (with "probably belongs to X")
tether attach m3 query          # file an idea under a project
tether promote t42              # turn one of Claude Code's own tasks into a project task
tether ui                       # editable live view at http://127.0.0.1:7878
tether watch                    # read-only terminal view
tether setup chat               # phone/web Chat -> Drive "Tether Inbox" -> Code
tether inbox import <folder>    # import ideas Chat saved as markdown files
tether config notify off        # macOS notifications
```

`tether install` also adds a PostToolUse hook that mirrors Claude Code's own task list
(`TaskCreate`/`TaskUpdate`/`TodoWrite`) into Tether as *session tasks*, in folders bound
with `tether init`. Anything that looks like an API key, token or password is refused on
every write path.

## Two Macs

```bash
tether sync setup        # on each Mac; uses iCloud Drive/Tether by default (--dir to change)
tether sync              # push + pull now, then show status (it also happens automatically)
tether sync status
tether sync off
```

Each Mac keeps its own database and appends its changes to its own journal file in the
shared folder; nothing writes the same file from two machines, so iCloud never sees a
conflict. Changes merge field by field (newest edit wins per field), so finishing a task
on one Mac and renaming it on the other both survive. Folder bindings stay per Mac: if
the project lives elsewhere on the second Mac, run `tether init` there and it binds the
synced project instead of creating a duplicate.

Limits: if iCloud "Optimize Mac Storage" evicts a journal, that Mac's changes wait until
iCloud downloads it again. Journals grow over time; compaction isn't built yet.

## How it works

- **Storage:** one SQLite file at `~/.tether/tether.db` (override with `TETHER_HOME`).
- **Surface tagging:** each client starts the server with `TETHER_SURFACE` set, so every write records who made it and every pack is tailored to whoever reads it.
- **More detail:** see [docs/PLAN.md](docs/PLAN.md) for the architecture and roadmap, and [docs/RESEARCH.md](docs/RESEARCH.md) for how Tether compares to existing tools.

## Develop

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest
```

MIT licensed.
