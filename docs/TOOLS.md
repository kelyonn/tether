# Tether MCP tools

Generated from the server's tool definitions. Do not edit by hand: run `python scripts/gen_tool_docs.py`.

17 tools. Read-only tools never change anything; write tools never delete.

## `add_tasks`

*writes*

Add one or more open tasks. Tag each with the surface that should do it.

| Parameter | Type | Default |
|---|---|---|
| `tasks` | list of NewTask | required |
| `project` | string | none |

`NewTask` fields:

| Field | Type | Notes |
|---|---|---|
| `title` | string | required. Short imperative title, e.g. 'Add rate limiting to /login' |
| `for_surface` | `code` \| `desktop` \| `cowork` \| `chat` \| `design` \| `any` | Who should do it |
| `priority` | integer | 1 high, 2 normal, 3 low |
| `detail` | string | Optional acceptance criteria or context |

## `attach_memory`

*writes*

File an idea under a project, so the project's context includes it.

| Parameter | Type | Default |
|---|---|---|
| `memory` | string | required |
| `project` | string | required |

## `context`

*read-only*

Load the project's working context: goal, status, handoffs waiting for you,
changes since your last visit, open tasks and active decisions. Call once at the
start of a session. Closed tasks and superseded decisions are left out on purpose;
use `search` for history. With no project given (and none bound to the folder) it
lists projects and recent unfiled ideas instead of guessing.

| Parameter | Type | Default |
|---|---|---|
| `project` | string | none |
| `surface` | `code` \| `desktop` \| `cowork` \| `chat` \| `design` \| `any` | none |
| `budget_tokens` | integer | `1500` |

## `create_handoff`

*writes*

Hand work to another surface. The body must stand alone: the receiver can't
see this conversation. Include links (mockup URLs, file paths, PRs) and the task
refs it covers. It shows up at the top of the receiver's context.

| Parameter | Type | Default |
|---|---|---|
| `to_surface` | `code` \| `desktop` \| `cowork` \| `chat` \| `design` \| `any` | required |
| `title` | string | required |
| `body` | string | required |
| `links` | list of string | none |
| `tasks` | list of string | none |
| `project` | string | none |

## `get`

*read-only*

Full detail for one item: t12, d3, h2 or m4 (idea).

| Parameter | Type | Default |
|---|---|---|
| `ref` | string | required |

## `list_projects`

*read-only*

List known projects with open task counts. Use when unsure which project the
user means.

## `list_tasks`

*read-only*

List tasks, open only by default. Filter by the surface they are for.

| Parameter | Type | Default |
|---|---|---|
| `for_surface` | `code` \| `desktop` \| `cowork` \| `chat` \| `design` \| `any` | none |
| `include_closed` | boolean | `False` |
| `project` | string | none |

## `log_note`

*writes*

Add a free-form progress note to the activity log (e.g. 'deployed to staging').

| Parameter | Type | Default |
|---|---|---|
| `text` | string | required |
| `project` | string | none |

## `recall`

*read-only*

Search every saved idea and every project (goals, decisions, tasks, handoffs).
Use it before `remember`, and whenever the user refers to something discussed
elsewhere. Works with no project.

| Parameter | Type | Default |
|---|---|---|
| `query` | string | required |

## `recent_memories`

*read-only*

List recent saved ideas, newest first. Unfiled ones are the inbox.

| Parameter | Type | Default |
|---|---|---|
| `project` | string | none |
| `unfiled_only` | boolean | `False` |
| `limit` | integer | `10` |

## `record_decision`

*writes*

Record a settled decision future sessions must respect. One line; put the
reason in `why`. Pass `replaces` (e.g. 'd3') when it overturns an earlier one.

| Parameter | Type | Default |
|---|---|---|
| `decision` | string | required |
| `why` | string | empty |
| `replaces` | string | none |
| `project` | string | none |

## `remember`

*writes*

Save an idea, plan, research or spec so any other Claude surface can find it
later. Works with no project. If `recall` found an idea on the same topic you must
pass its id as `update_of` (m4) instead of saving a new one;
`append` adds the body below the old one, otherwise it replaces it. Near-duplicates
are merged automatically; looser matches come back as "did you mean".

| Parameter | Type | Default |
|---|---|---|
| `title` | string | required |
| `body` | string | empty |
| `summary` | string | empty |
| `kind` | `idea` \| `research` \| `spec` \| `decision-log` \| `bug` \| `todo-list` \| `note` | `idea` |
| `tags` | list of string | none |
| `project` | string | none |
| `update_of` | string | none |
| `append` | boolean | `True` |

## `search`

*read-only*

Search all decisions, tasks (including closed), handoffs and notes. Use this
for history instead of asking the user to re-explain.

| Parameter | Type | Default |
|---|---|---|
| `query` | string | required |
| `project` | string | none |

## `start_project`

*writes*

Create a project, ONLY when the user explicitly asks for one. Never to file an
idea or because a topic sounds project-like (use `remember` for that). `path` binds it to a folder so Claude Code sessions there pick
it automatically (defaults to the current folder when running in Claude Code).

| Parameter | Type | Default |
|---|---|---|
| `name` | string | required |
| `goal` | string | empty |
| `path` | string | none |

## `update_handoff`

*writes*

Mark a handoff accepted when you pick it up, done when delivered.

| Parameter | Type | Default |
|---|---|---|
| `handoff` | string | required |
| `status` | `accepted` \| `done` \| `dropped` | required |
| `note` | string | empty |

## `update_project`

*writes*

Set the one-line current status (where things stand now), the goal, or the
longer brief (the spec every surface should know).

| Parameter | Type | Default |
|---|---|---|
| `status` | string | none |
| `goal` | string | none |
| `brief` | string | none |
| `project` | string | none |

## `update_task`

*writes*

Change a task: start it (doing), finish it (done, with a note of what changed),
block it (with the reason), drop it, or edit its title/owner/priority.

| Parameter | Type | Default |
|---|---|---|
| `task` | string | required |
| `status` | `todo` \| `doing` \| `blocked` \| `done` \| `dropped` | none |
| `note` | string | none |
| `title` | string | none |
| `for_surface` | `code` \| `desktop` \| `cowork` \| `chat` \| `design` \| `any` | none |
| `priority` | integer | none |
