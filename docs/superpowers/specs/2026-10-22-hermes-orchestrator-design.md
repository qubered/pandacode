# Hermes orchestrator: MCP endpoint and event webhooks

Status: approved direction (approach A), sections 1–2 approved in chat; sections 3–5
are the defaults below, open to review while implementation runs.
Branch: `hermes-orchestrator`, based on `remote-core`.

## Goal

Hermes Agent, running on another machine on the tailnet, orchestrates coding work
through PandaCode. It can find, create, message, steer, stop and read projects and
tasks, and it can answer approvals on the tasks it supervises. PandaCode wakes Hermes
with signed webhooks when something needs it. The user keeps working in the PandaCode
UI on the same tasks, and can take any task over.

Success looks like this:
1. From Hermes (or Telegram through Hermes), the user asks for a change.
2. Hermes picks the project, launches a task, and answers routine approvals.
3. Hermes is woken when the task finishes, reviews the result and reports back.
4. Throughout, the user can watch, message, approve or take over in the UI, and
   Hermes hears about it.

## Architecture

A new module, `src-tauri/src/orchestrator/`, is compiled in both desktop and headless builds:

| File | Owner (unit) | Purpose |
|---|---|---|
| `mod.rs` | foundation | Config, startup, shared types (`Event`, `Actor`) |
| `mcp.rs` | A | Long-lived HTTP MCP endpoint: auth, JSON-RPC, dispatch to `tools` |
| `tools.rs` | B | Tool catalog and handlers; native queue drain for supervised tasks |
| `hooks.rs` | C | Outbound webhooks: event payloads, signing, delivery with retries |

Handlers call the same internal functions as the UI commands. They do not go through
IPC to the core itself. So everything Hermes does reaches the UI through the existing
`agent-event`, `sessions-changed` and `queue-changed` events.

## Configuration

`orchestrator.json` in the data dir holds settings that are not secret:

```json
{ "enabled": true, "name": "hermes", "bind": "100.87.96.22", "port": 7879,
  "maxMode": "auto",
  "webhook": { "url": "http://hermes-host:8644/webhooks/pandacode" } }
```

- **Token:** the MCP bearer token lives in the secret store at
  `app.pandacode.orchestrator/token`. It is generated on first enable (32 random bytes,
  hex).
- **Webhook secret:** stored at `app.pandacode.orchestrator/webhook-secret`. The user
  pastes it from `hermes webhook subscribe`.
- **Reading settings:** `orchestrator_settings` returns the settings, the MCP URL, and
  whether a token and a webhook secret are set. The token is returned only by an explicit
  `orchestrator_reveal_token`.
- **Saving settings:** `orchestrator_save_settings` saves them and restarts the endpoint.
- **Headless:** `pandacode-core --mcp-port N` enables the endpoint with that port on the
  `--bind` address and overrides the file. `pandacode-core --print-mcp-token` prints
  the token (generating it if needed) and exits.
- **Mode values** are the session `mode` strings: `plan` < `ask` < `auto` < `full`.
  The ceiling is `maxMode`, default `auto` (Accept edits). A mode above the ceiling is
  clamped down to it. In `auto`, the agent CLI still decides which shell commands are safe
  to run unasked. Claude Code ran `sleep … && date > file` without a prompt in testing.
  `ask` prompts for every command.

## Data model (schema 26)

- `sessions.supervisor TEXT NULL`: the orchestrator name (`"hermes"`), or NULL when the
  user supervises. It appears in `Session` as `supervisor: Option<String>`.
- `messages.sender TEXT NULL`: who wrote a user message. NULL means the user. It appears in
  `Message` as `sender`.
- `queued_messages.sender TEXT NULL`: carried to `messages.sender` on delivery.
- New `Store` methods:
  - `set_supervisor(sid, Option<&str>)`
  - `set_message_sender(message_id, &str)`
  - `latest_user_message_id(sid)`
  - `queue_set_sender(entry_id, &str)`
  - `list_tasks_filtered(...)` (unit B)

## Section 1: tools (MCP, prefix `pandacode_`)

All tools take and return camelCase JSON. Results are compact JSON with a one-line summary
in the text content. Message bodies are capped at 4 KB each, with `truncated: true`.

| Tool | Arguments | Result |
|---|---|---|
| `list_projects` | none | `[{id,name,path,branch,taskCounts:{running,waiting,total}}]` |
| `add_project` | `path` | project. Refuses missing paths and non-directories |
| `list_tasks` | `projectId?, status?, supervisor?, limit?=30` | `[{id,projectId,title,status,supervisor,provider,model,updatedAt}]`. Excludes drafts, archived tasks and architect workers |
| `search_tasks` | `query, projectId?, limit?=20` | Hits with excerpts. All projects when `projectId` is omitted |
| `read_task` | `taskId, sinceMessageId?, limit?=20` | `{task, pending:{kind:"approval"\|"question",requestId,text,data}?, messages:[{id,role,sender,content,truncated,createdAt}], lastChanges?}` |
| `create_task` | `projectId, prompt, provider?, model?, mode?, worktreeBranch?` | `{taskId, mode, clamped}`. Creates, sets supervisor, starts |
| `send_message` | `taskId, text, delivery?="queue"` (`"queue"\|"steer"`) | `{delivered:"started"\|"queued"\|"steered"}` |
| `respond` | `taskId, requestId, approve?, answer?` | `{ok}`. Only while `supervisor == name` |
| `release_task` | `taskId` | Supervisor set to the user. Emits `task.released` |
| `stop_task` | `taskId` | `{ok}` |
| `wait_for_task` | `taskId, timeoutSeconds?=60` (max 120) | `{reason:"done"\|"needs_response"\|"timeout", task}` |
| `list_models` | none | `[{provider,installed,models:[{id,label}]}]` |

**Defaults for `create_task`:**
- Provider and model come from the app's default.
- The mode is clamped to the ceiling.
- `worktreeBranch` creates the task in a new worktree, as `create_worktree` does.

**Delivery rules:**
- `send_message` on an idle task starts a turn with that text.
- On a busy task it queues by default. With `"steer"` it uses `steer_task`.
- Messages Hermes sends, and queue entries it adds, are tagged `sender = name`.

**Native queue drain:** at the end of a run on a task with a supervisor, the core
delivers the next queued entry itself, as bot tasks do.
- This is `tools::drain_next(app, sid)`, called from the run-finished hook.
- It respects the existing rule: a failure, stop or restart holds the queue until Resume.
- The frontend skips `dispatchQueue` for tasks with a `supervisor`.

## Section 2: supervision and approvals

- **Who supervises:**
  - Tasks Hermes creates start with `supervisor = name`.
  - Hermes cannot claim user tasks. The user hands a task over with "Let Hermes
    supervise".
  - "Take over" (or Hermes's `release_task`) returns a task to the user.
  - The UI command is `set_task_supervisor(sessionId, supervisor: string|null)`.
- **Approvals and questions:**
  - Both appear in the UI as today. On supervised tasks they carry a note.
  - The first valid answer wins (the request ID is single use).
  - The user can always answer. Hermes can answer only while it supervises.
  - Nothing is auto-approved on a timeout.
- **Messages:** the user may message or steer any task. That does not change the supervisor.
  The orchestrator may message only tasks whose mode is within `maxMode`. Otherwise a
  message to the user's Full access task would run without the approvals the ceiling keeps.
- **Mode ceiling:** `maxMode`, default `auto`.

## Section 3: webhooks (Hermes "Generic V2")

POST JSON to `webhook.url` with these headers:
- `Content-Type: application/json`
- `X-Webhook-Timestamp: <unix seconds>`
- `X-Webhook-Signature-V2: <hex HMAC-SHA256(secret, "<timestamp>.<raw body>")>`
- `X-Request-ID: <event id (uuid)>`

The body:

```json
{ "event_type": "task.finished", "id": "<uuid>", "occurredAt": 1792627000000,
  "task": { "id", "projectId", "projectName", "title", "status", "supervisor", "provider", "model" },
  "summary": "one line for the Hermes prompt template",
  "data": { } }
```

**Events.** Only tasks with a supervisor send webhooks:

| `event_type` | When | `data` |
|---|---|---|
| `task.finished` | A run ends: done, failed, stopped or limited | `{outcome, finalReply (≤2 KB), changedFiles}` |
| `task.needs_response` | An approval or question arrives | `{kind, requestId, text}` |
| `task.human_message` | The user sends or steers via the UI | `{text (≤2 KB), delivery}` |
| `task.human_responded` | The user answers an approval or question | `{requestId, approved?, answer?}` |
| `task.taken_over` | The user sets the supervisor to themselves | none |
| `task.handed_over` | The user hands a task to Hermes | `{lastReply (≤2 KB)}` |
| `task.released` | Hermes releases a task | none |

- **Where events come from.** Human events fire only from the Tauri/WS command wrappers
  (`send_message`, `queue_add`, `steer_task`, `respond_approval`, `respond_user_input`,
  `set_task_supervisor`), never from orchestrator tool handlers. That is how "the user
  acted" is told apart from "Hermes acted".
- **Delivery:**
  - Events go through an in-memory FIFO with one sender task.
  - A failed POST is retried after 2 s, 10 s, 60 s, 5 min and 30 min, then dropped and
    logged.
  - A 2xx counts as delivered.
  - A 4xx other than 429 is not retried.
  - The `X-Request-ID` makes retries idempotent on Hermes's side.
  - The queue is capped at 1000 events; the oldest are dropped first.
  - Losing it on restart is acceptable, because `read_task` and `list_tasks` let Hermes
    catch up.
- **No webhook configured:** events are discarded, and `wait_for_task` still works.

Suggested Hermes route (documented in `docs/hermes.md`):

```yaml
platforms:
  webhook:
    enabled: true
    extra:
      routes:
        pandacode:
          secret: "<webhook-secret>"
          prompt: "PandaCode {event_type}: {summary}\nTask {task.id} in {task.projectName}. Use the pandacode MCP tools to read the task and act."
mcp_servers:
  pandacode:
    url: "http://100.87.96.22:7879/mcp"
    headers: { Authorization: "Bearer <token>" }
```

## Section 4: security

- **Binding:** the endpoint binds only the configured address. The default is
  `127.0.0.1`; on the server it is the tailnet IP.
- **Auth:** every request needs `Authorization: Bearer <token>`, compared in constant time.
  A missing or wrong token gets a 401 with no detail.
- **Browsers:** a request carrying an `Origin` header is refused with a 403. MCP clients
  are not browsers, so a page in a browser cannot drive the endpoint.
- **Request limits:** bodies are capped at 1 MB. Reads time out after 10 s. At most 32
  concurrent connections are served.
- **Webhooks:** the signature covers the timestamp, which gives Hermes replay protection.
- **Logs:** tokens and secrets are never logged and never returned in tool results.
  `orchestrator_settings` reports only `tokenSet` and `webhookSecretSet`.

## Section 5: UI and testing

**UI** (unit D):
- A "Hermes" badge on supervised tasks in the sidebar.
- In the task header: the supervisor name plus **Take over** or **Let Hermes supervise**.
- Supervised approval and question bars say "Hermes is supervising — you can still answer".
- User messages with a `sender` show it as the author label instead of "You".
- Settings → Agents → Orchestrator:
  - enable toggle, bind address, port and max mode
  - webhook URL and secret
  - MCP URL with a "Copy token" button (calls `orchestrator_reveal_token`)
  - a copyable Hermes config snippet
- Preview mocks for every new command.
- The frontend skips `dispatchQueue` for supervised tasks.

**Tests:**
- **Unit B:** Rust tests for every tool against a temp store, covering clamping,
  supervisor enforcement in `respond`, and filtering.
- **Unit C:** signature vectors (a fixed secret, timestamp and body give a known hex),
  retry policy, and the human-only event sources.
- **Unit A:** an MCP endpoint test over real HTTP covering `initialize`, `tools/list`,
  `tools/call`, 401 without a token, 403 with an `Origin`, and the body limit.
- **Unit D:** a Playwright spec for the badge, take over, sender label and settings panel.
- **End to end (integration):** a scripted MCP client creates a task. A local HTTP
  receiver verifies the signed webhooks. A browser approves and takes over.
