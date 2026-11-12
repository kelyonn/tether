# Tether: the full plan

*Working name, kept until the open-source launch. Last updated 2026-11-09. Decisions are recorded in section 10.*

---

## 1. What Tether is

**One memory and project ledger shared by every Claude surface.** You ideate anywhere: on your phone, on the web, or in Code. The idea gets saved and sorted, and when you open Claude Code it already knows. On top of that, a live view shows what every surface, Code included, is doing right now.

| Layer | Holds | Example |
|---|---|---|
| **Memory** | Ideas, research and specs, saved automatically from any chat and sorted. A project is optional. | "Reading app: swipe to save quotes, offline-first, maybe Kindle import" |
| **Ledger** | Each project's goal, status, decisions, tasks and handoffs | "query: d3 use gpt-4o-mini · t1 [doing] add pytest tests" |
| **Live view** | What's happening right now, editable | Code is on "fix SQL guard", 3 new ideas in the inbox, a handoff waiting |

**Not:** a replacement for Claude's built-in memory, a Jira clone, a cloud service, or an archive of whole chat transcripts.

## 2. Where you work and how each surface connects

You ideate on **phone Chat, web Chat and in Claude Code**. You don't use the Desktop app, and you don't use Code or Cowork on mobile.

| Surface | Reads Tether | Writes to Tether |
|---|---|---|
| **Claude Code (Mac)** | Automatically, through the session-start hook | MCP tools, plus Code's own task list synced through a hook |
| **Phone and web Chat** | Not directly | **Drive inbox.** Chat saves each idea as a file in a `Tether Inbox` Google Drive folder through your Drive connector. Code imports it. |
| Claude Desktop / Cowork | MCP tools | MCP tools (works, not your main path) |
| Claude Design | Paste | Paste (cut candidate) |
| You | Live view (browser and terminal), CLI | Live view (editable), CLI |

**Nothing has to stay running.** Code starts Tether itself when it needs it, and Drive is already in the cloud.

## 3. The flows

1. **Phone → Code (the main one)**
   - You brainstorm about anything in phone Chat.
   - Chat saves the idea to the Drive inbox **automatically**, with a depth that fits the idea. If the idea already exists, Chat updates it.
   - Next time you open Claude Code, Tether reports "2 new ideas in your inbox". Code imports them through the Drive connector, and Tether sorts them.
   - You say "start building the reading app idea". Code creates the project and **attaches** the idea as context.
2. **Code → everything.** Code's own tasks sync live, and its decisions and status show up everywhere. You get a macOS notification when Code finishes a task or a handoff arrives.
3. **Watch and steer.** The live view shows all projects, Code's live tasks, the ideas inbox and the activity feed. You can tick tasks, attach ideas and accept handoffs right there.

## 4. Built so far

**Done, 85 tests passing**
- SQLite store with migrations, budgeted context pack, MCP server (17 tools) with built-in rules
- Claude Code session-start hook and task-sync hook; paste-back for Chat and Design; Markdown mirror; git link ("fixes t12")
- Memory layer: remember/recall/attach, dedupe, project suggestions, no project guessing
- Drive-inbox import + `setup chat`; secrets blocking; notifications; `tether ui` / `watch`; promote
- Two Macs: journal sync, field-level merge, per-Mac bindings, automatic and manual compaction
- Rule-eval kit with Anthropic and NVIDIA providers. Full run on 2026-11-12 (Mistral Large 4 via NVIDIA, 1 run): 11/11 scenarios, 100% on every rule, against the 80% target. Not yet run on a Claude model.
- Desktop extension (`scripts/build_extension.py` -> `dist/tether.mcpb`)
- Packaging as `tether-mcp` 0.4.0 (wheel and sdist build), CI on macOS and Linux

**Still to do (needs you):** real iCloud test (single Mac available, so sync is covered by tests only), real-world check of the Drive inbox from phone Chat, 2-3 weeks of dogfooding, then PyPI/GitHub publish.

**(Old note) Half-built before v0.2:**
- Memories table: save, update, attach, archive, and recall across everything
- Code session tasks: upsert from `TaskCreate`/`TaskUpdate`/`TodoWrite`
- Schema v2 migration

## 5. v0.2: what gets built next

### 5a. Memory layer and smart sorting
- **MCP tools:**
  - `remember`: save or update an idea. If it matches an existing one, Claude passes `update_of`.
  - `recall`: searches ideas and every project.
  - `recent_memories`: lists recent ideas.
  - `attach_memory`: moves an idea into a project.
- **Smart sorting.** Tether runs no AI of its own and has no API key. Claude does the judgement when saving, and Tether does the bookkeeping.
  - **Claude classifies** each save with a kind (idea, research, spec, decision-log, bug, todo-list), tags, a one-line summary and a guess at the project if one fits.
  - **Tether deduplicates.** Before saving, it compares the title and words with existing ideas. A strong match is merged into the existing one, and a weak match comes back as "did you mean m4?".
  - **Tether suggests projects.** Ideas that look like an existing project (by name, goal and words) get flagged "probably belongs to query" in that project's pack and in the live view. Attaching is still your call.
  - **Ranking.** The inbox is ordered by recency and how often an idea has been updated. Ideas untouched for 60 days or more sink to an "older" section and are never deleted.
- **Smart depth.** A quick thought gets a short note, and a long design session gets a full write-up: options weighed, decisions, open questions, next steps. Every idea also gets a one-line summary for lists.
- **Auto-save rules (server instructions plus claude.ai preferences):**
  - Save when an idea, plan or decision takes shape, or when you ask.
  - Update the matching idea rather than creating a new one.
  - Skip small talk and one-off questions.

### 5b. Drive inbox (phone/web Chat → Code)
- **One-time setup:**
  - `tether setup chat` prints a paragraph to paste into claude.ai Settings → Profile → preferences.
  - Create a `Tether Inbox` folder in Drive.
- **The paragraph tells Chat:**
  - Save ideas automatically as markdown files into `Tether Inbox`.
  - Use a fixed header (title, kind, tags, summary, maybe-project, update-of).
  - Search the folder first and update an existing file rather than creating duplicates.
- **Import:**
  - The session-start pack says "N new ideas in Drive inbox". Server instructions tell Code to read them with the Drive connector, call `remember` for each, then move each file to `Tether Inbox/imported`.
  - `tether inbox import <folder>` does the same for a folder exported by hand.
- **Must verify first:** that phone Chat has the Drive connector turned on, and that terminal Claude Code has it too, not just the desktop app.

### 5c. No more project guessing
- MCP tools never use a global "current project".
- With more than one project, Claude passes `project=<slug>`, taken from the pack header.
- `context` with no project lists the projects and recent ideas instead of guessing.
- `tether apply` with no project header refuses unless run inside a bound folder.

### 5d. Claude Code's own tasks
- A PostToolUse hook on `TaskCreate|TaskUpdate|TodoWrite` syncs them as **session tasks**, only in folders bound with `tether init`.
- They stay out of the project task list and the pack's activity section, so there's no noise.
- The next Code session sees the 5 most recent unfinished ones.
- `tether promote t42` turns one into a project task.

### 5e. Live view: browser and terminal, editable
- **`tether ui`** opens `http://127.0.0.1:7878`. It shows:
  - every project, and what Code is doing now
  - a task board (todo / doing / done)
  - Code's live session tasks
  - the ideas inbox with suggested projects
  - open handoffs
  - recent decisions
  - the live activity feed
- **Editable from the page:** tick, start or drop tasks, add tasks, attach, edit or archive ideas, accept or deliver handoffs, record decisions, set status.
- **Security:**
  - It listens on localhost only.
  - Every write needs a random token created at launch, so other websites can't send writes to it.
  - It sends nothing over the network.
- **`tether watch`** is a terminal dashboard that redraws on change. It is read-only, and the browser is where you edit.

### 5f. Block secrets
- Every write is checked against patterns for common keys and tokens. That covers API keys (`sk-…`, `ghp_…`, AWS, Google, Slack, Stripe), JWTs, private-key blocks, and `password=` / `token=` assignments.
- A match is refused, with a message telling Claude to save the text without the secret. This applies to the CLI, MCP, hooks and the live view.

### 5g. Done notifications
- A macOS notification when Code finishes a session task, when a handoff to Code arrives, and when new ideas land in the inbox.
- Off with `tether config notify off`. It uses the built-in `osascript`, so there's nothing to install.

## 6. Two Macs (v0.3, the hardest piece)

SQLite can't safely live in iCloud or Drive, because two machines writing to the same database file corrupts it.

**Plan:**
- Each Mac keeps its own database and writes every change to an **append-only journal** file in a shared folder (iCloud Drive, which you have).
- Each Mac imports the other's journal on start and on each read.
- Records get globally unique ids, and the last write wins per field.

**Built.** See `src/tether/sync.py`. Change capture uses SQLite triggers, so every write path is covered. Merge is field-level with a hybrid logical clock. Projects created on both Macs before syncing merge by slug. Compaction is built (automatic past 8 MB, or `tether sync compact`).

## 7. Roadmap

| Phase | What | Size |
|---|---|---|
| **v0.2** | Sections 5a–5g and tests | 3–4 days |
| **Verify** | Drive connector on phone and in terminal Code. Run `tether install` on your Mac. | 1 hour, needs you |
| **Dogfood** | Daily use on `query` plus new ideas for 2–3 weeks, with friction logged | 2–3 weeks |
| **Rule eval** | Measure whether Claude auto-saves, sorts, records decisions and ticks tasks. Target at least 80%. | 2–3 days |
| **v0.3** | Two-Mac sync. Cut whatever dogfooding shows is useless. One-click Desktop extension. | 1–2 weeks |
| **v0.4: launch** | Final name, PyPI, GitHub, demo, CI | 1 week |
| **Later, if needed** | Cloud endpoint so Chat writes directly, replacing the Drive inbox | 1–2 weeks |

## 8. Cut after dogfooding (your call: remove the useless ones later)

Markdown mirror · `log_note` tool · project `brief` field (moves into attached ideas) · paste-back block. They stay until real use shows they're unused, then go.

## 9. Risks and honest costs of the decisions

| Decision or risk | The cost | Mitigation |
|---|---|---|
| **Automatic saving** | Clutter, and Chat writing Drive files mid-conversation | Smart dedupe and merge, a "skip small talk" rule, an inbox you can archive from in one click |
| **The Drive inbox depends on Chat following preferences** | It may forget to save or use the wrong format | The import accepts loose formats. Watch the save rate in dogfooding. Upgrade to the cloud endpoint if it's poor. |
| **Editable live view** | More code, plus a local web server that writes data | Localhost only, a per-launch token, and it's the same store functions the CLI uses |
| **Two Macs** | The hardest feature, with real risk of sync bugs | Journal-based, built after single-Mac use is solid |
| **Sorting quality depends on Claude** | Mis-tags and missed merges | Tether's own similarity check as a second layer. Easy fixes in the live view. |
| Anthropic ships cross-surface memory including Code | Overlap | Stay structured: tasks, decisions, handoffs, live view |
| Too many tools for the model | Worse tool choice | Cut `log_note`, keep at most 14 tools |

## 10. Decisions log

| # | Question | Decision |
|---|---|---|
| 1 | Where do you ideate | Phone Chat, web Chat, Claude Code |
| 2 | How phone and web reach Tether | **Drive inbox** (cloud endpoint later if needed) |
| 3 | Saving | **Automatic**, with smart sorting |
| 4 | Memory depth | **Smart:** depth fits the content, always with a one-line summary |
| 5 | Same idea again | **Update the existing one** |
| 6 | Idea becomes a project | **Just attach it** |
| 7 | Code task sync scope | Folders bound with `tether init` ("whatever works") |
| 8 | Live view | **Both** browser and terminal. The browser is **editable**. |
| 9 | Extras | **Block secrets, two Macs, done notifications.** Other AI tools later. |
| 10 | Cuts | Mirror, `log_note`, `brief`, paste-back. Remove after dogfooding. |
| 11 | Name | **Final.** Product is Tether; the PyPI package is `tether-mcp` |
| 12 | Product shape | One product |

**Still open, decide during dogfooding:**
- Do memories ever auto-archive (currently never)?
- Does the live view show all projects or focus one by default (currently all)?
- MIT vs AGPL at launch (currently MIT).
- Whether other AI tools (Cursor, Codex) are needed before launch.
