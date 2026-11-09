# Prior art and market check

*Researched 2026-10-20. Re-check before the open-source launch, because this space moves monthly.*

## Short answer

You're not the only one with the idea, but nobody has built this exact product.

- **Memory across tools** is a crowded category. Directories list 300+ "memory" MCP servers.
- **Task tracking for coding agents** exists, but it is Claude Code–centric.
- **Anthropic has closed part of the gap itself.** Chat and Cowork now share one memory, and Design can hand off to Code.
- **The gap:** no tool combines structured project state with a cross-surface handoff inbox. That state is decisions that can be superseded, tasks tagged by the surface that should do them, and handoffs between surfaces. It should load automatically in Claude Code and have a write-back path for surfaces that can't call MCP.

Tether should **not** pitch itself as "memory". It is a project ledger.

## What Anthropic ships

| Capability | Status | Implication for Tether |
|---|---|---|
| Shared memory between Chat and Cowork | Shipped 2026-08-25. Memory is now one pool, editable by the user, on by default for Free/Pro/Max. ([TechCrunch](https://techcrunch.com/2026/08/25/claude-cowork-finally-remembers-what-you-told-the-app-in-chat/), [9to5Mac](https://9to5mac.com/2026/08/25/anthropic-update-unifies-memory-feature-across-claude-cowork-and-chat/)) | General "remember me" memory is covered. Don't compete on it. |
| Claude Code memory | Separate: `CLAUDE.md` plus local auto-memory. A vendor blog says Anthropic told The Register it stays separate ([memoryplugin](https://blog.memoryplugin.com/claude-code-chat-memory/)). This is secondhand and unverified. | Code is the island Tether connects. It is also where the next native feature could land. |
| Design → Code handoff | Native. Design exports a handoff bundle (component spec, tokens, layout, assets) to Claude Code or Claude Code Web. Expanded in June 2026 ([claudefa.st](https://claudefa.st/blog/guide/mechanics/claude-design-handoff), [aiweekly](https://aiweekly.co/alerts/claude-design-gains-github-import-and-claude-code-handoff)). | Don't rebuild the bundle. Record it as a Tether handoff (title plus link) so it sits next to the tasks and decisions it belongs to. |
| Claude Design + MCP | No evidence Design can call MCP or read local files. Its documented inputs are prompts, uploads (DOCX/PPTX/XLSX/images), a linked codebase and web capture. | Design stays a paste/upload surface. The mirror `.md` file or `tether ctx --for design` is the bridge. |
| claude.ai custom connectors (remote MCP) | Supported on web and Desktop. Remote servers run from Anthropic's cloud, so they can't reach local disk ([sunpeak](https://sunpeak.ai/blogs/what-are-claude-connectors/), [Scalar](https://scalar.com/learn/mcp/connect-mcp-server-to-claude)). | Phase 3 (a remote Tether endpoint) is what makes Chat a first-class writer. |

User pain that Anthropic hasn't solved:
- A report that the "Open Claude Code" handoff from claude.ai carries no conversation context ([claudeissues mirror](https://claudeissues.com/issue/56903-open-claude-code-handoff-from-claude-ai-doesnt-carry-conversation-context), closed May 2026).
- A report that Cowork couldn't find a chat's context ([claudeissues mirror](https://claudeissues.com/issue/69589-bug-claude-cowork-not-able-to-access-the-context-of-a-particular-chat-from-claud)).

## Closest open-source projects

| Project | What it does | How Tether differs |
|---|---|---|
| [mcp_shared_memory_server](https://github.com/shirisha456/mcp_shared_memory_server) | Shared, versioned project memory for Desktop/Cursor/Code on Postgres + pgvector, with supersede semantics. Claims a 4.2x token reduction. 0 stars. | Closest in spirit for decisions. No tasks, no handoffs, needs Docker and Postgres. Tether is a single SQLite file with zero services. |
| [OpenMemory MCP (mem0)](https://mem0.ai/blog/introducing-openmemory-mcp) | Local-first memory shared across Cursor, Claude Desktop and Windsurf. A few tools (add/search/list/delete). | Stores unstructured fragments optimized for retrieval. No notion of done or decided. |
| [Cortex](https://github.com/cdeust/Cortex) | Cross-client memory MCP on SQLite with hybrid retrieval and Claude Code hooks. | Memory and retrieval research. No task or handoff model. |
| [mcp-memory-service](https://wavect.io/blog/mcp-memory-service-claude-code-cursor/) | One SQLite store shared by Code, Cursor and OpenCode, mainly for retrieving project decisions. | Same storage idea. No surface-aware tasks or handoffs. |
| [claude-brain](https://github.com/mikeadolan/claude-brain) | SQLite + MCP + hooks memory for Claude Code. | Claude Code only. |
| [Task Master](https://www.npmjs.com/package/task-master-ai) | PRD → task breakdown → next-task workflow for coding agents, over MCP. Popular. | Code-only task planner with no decisions, handoffs or other surfaces. It could complement Tether rather than compete. |
| Directories | [mcpservers.org lists 313 memory servers](https://mcpservers.org/category/memory). | The "memory" label is saturated. Stay out of it. |

## Implications for the plan

1. **Positioning:** "Project state that every Claude surface shares: what's decided, what's done, what's next, and who has the ball." Avoid "memory".
2. **Differentiators to protect:**
   - Superseding decisions
   - Tasks tagged by surface
   - The handoff inbox at the top of the context
   - "Since you were last here"
   - Auto-load through a Claude Code hook, so it works even when the model forgets
   - A paste-back block for MCP-less surfaces
3. **Lower the Anthropic risk:** Anthropic is unifying memory surface by surface, so the generic part will get absorbed. Two things make Tether hard to make redundant:
   - Its structure: tasks and decisions with lifecycles.
   - Working with any MCP client (Cursor, Codex, Gemini CLI) when it goes open source.
4. **Name check:** `tether` is free on PyPI. But **Tether is a very large crypto brand (USDT)**, and there is already a "Tether" MCP server ([tether-mcp](https://pypi.org/project/tether-mcp/), a health-data app). Expect confusion and search collisions. Decide before the public launch (see PLAN.md).
5. **Design bridge:** link to Design's native bundle rather than replicating it.
