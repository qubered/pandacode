# Orchestrating PandaCode from Hermes

PandaCode can be driven by [Hermes Agent](https://hermes-agent.nousresearch.com/)
(or any MCP client):
- **MCP tools:** Hermes finds projects, starts and messages tasks, reads results, and
  answers approvals on the tasks it supervises.
- **Webhooks:** PandaCode wakes Hermes when one of those tasks finishes, needs an
  answer, or when you act on it.

You keep using PandaCode as usual on the same tasks. Design:
[the orchestrator spec](superpowers/specs/2026-10-22-hermes-orchestrator-design.md).

## How control works

- **Supervision:** tasks Hermes creates are *supervised by Hermes*. A badge in the
  sidebar marks them. Every supervised task has a **level**:
  - **watch**: Hermes can read the task and wait on it, and is woken only when a run
    finishes or an answer is needed. It cannot message, answer or stop it.
  - **manage**: Hermes can also message, steer, answer approvals and questions, and stop.
- **Epoch:** a number on each task that changes whenever its supervisor, level or grant
  changes. Write tools accept the epoch Hermes last saw and refuse a stale one, so a call
  planned before you took a task over never lands after it.
- **Approvals and questions:** on those tasks, either of you can answer, and the first
  answer wins. Hermes cannot answer on tasks you supervise.
- **Take over:** in the task header, this makes a task yours again at once: the agent
  keeps running (nothing restarts), messages Hermes queued are dropped (yours stay), its
  pending webhooks are dropped, and its next write call is refused. *Let Hermes
  supervise* / *Let Hermes watch* in the task menu hand one of your tasks over, or
  change the level.
- **Attach:** Hermes can also attach itself to one of your tasks with
  `pandacode_attach_task`, running or waiting on an approval, without restarting it. It
  cannot attach to a task another orchestrator supervises, nor manage one above its
  highest mode.
- **Handover:** when Hermes releases a task it can leave a note (what changed, what was
  checked, what is still open, the next step). The note is saved on the task's thread
  as *Handover from Hermes* and sent with `task.released`.
- **Highest mode:** Hermes cannot start tasks above this setting. The default is Accept
  edits: file edits, and shell commands the agent CLI considers safe, run without asking,
  while other commands still ask. Hermes can always choose **Ask** mode for a task, so
  every command needs an answer. Hermes cannot message, answer or stop tasks running
  above this setting (such as your Full access tasks), so it can't use them to get
  around it.
- **Full access, this task only:** handing a task above the highest mode to Hermes asks
  you to confirm *Let Hermes manage this Full-access task (this task only)*. That grants
  Hermes this task's mode for this task alone; the setting itself does not change. Only
  you can grant it (no tool can), and it ends when you take the task over, change its
  mode, change the level, or hand it over again without it, or when Hermes releases it.
- **Queued messages:** on supervised tasks, PandaCode delivers them itself. They go out
  even when no PandaCode window is open.

## 1. Turn on the endpoint

**Headless core** (on a server, see [remote-core.md](remote-core.md)):

```bash
pandacode-core --bind "$(tailscale ip -4)" --port 7878 --mcp-port 7879
pandacode-core --print-mcp-token
```

**Desktop app:** go to Settings → Agents → Orchestrator, turn it on, set the address
(your tailnet IP) and port, and use **Copy token**.

The endpoint requires the bearer token on every request and refuses browser requests.
Keep it on your tailnet.

## 2. Give Hermes the tools

In `~/.hermes/config.yaml` on the Hermes machine:

```yaml
mcp_servers:
  pandacode:
    url: "http://<pandacode-tailnet-ip>:7879/mcp"
    headers:
      Authorization: "Bearer <token>"
```

The 13 tools (all prefixed `pandacode_`):

| Tool | Needs |
|---|---|
| `list_projects`, `add_project`, `list_models` | nothing |
| `list_tasks`, `search_tasks`, `read_task`, `wait_for_task` | nothing (any task) |
| `attach_task {taskId, level}` | the task is yours or unsupervised; `manage` within the highest mode |
| `create_task` | the task is created supervised at `manage` |
| `send_message`, `respond`, `stop_task` | supervisor is Hermes, level `manage`, mode allowed (or granted), required current `epoch` |
| `release_task {taskId, epoch, handover?}` | supervisor is Hermes, either level |

`read_task` and `list_tasks` report `supervision: {level, epoch, grant}`; `attach_task`
returns `{taskId, level, epoch, grant, status}`.

### Contract for the Hermes plugin

- Treat `supervision.epoch` as a lease. Read it from `create_task`, `attach_task`,
  `read_task` or the webhook's `supervision` field, and pass it as `epoch` on every
  `send_message`, `respond`, `stop_task` and `release_task`.
- A refusal that says *Stale epoch*, *only watch* or *the user supervises* means the
  delegation changed: stop acting on the task, re-read it, and do not retry the same call.
- PandaCode re-checks supervisor, level, epoch and grant right before each action and
  before delivering a queued message, so a refusal can arrive even after a successful read.
- Webhooks for a delegation that ended are dropped; `task.taken_over` and `task.released`
  are the last events of a delegation. At level watch, only `task.finished`,
  `task.needs_response`, `task.handed_over`, `task.taken_over` and `task.released` arrive.
- Release with a `handover` `{changed, checked, open, next}` (each up to 1500 bytes) when
  handing work back.

## 3. Let PandaCode wake Hermes (optional)

1. Create a webhook route on the Hermes machine:

   ```bash
   hermes webhook subscribe pandacode \
     --prompt "PandaCode {event_type}: {summary}. Task {task.id} in {task.projectName}. Use the pandacode tools to read the task and act."
   ```

2. Copy the URL and secret it prints into PandaCode's Orchestrator settings (Webhook
   URL, Webhook secret). For the headless core, use the settings panel from a connected
   frontend.

**Signing:** PandaCode signs every webhook with Hermes's Generic V2 scheme
(`X-Webhook-Signature-V2` over `<timestamp>.<body>`) and sets `X-Request-ID`, so retries
are not processed twice.

**Events (in `event_type`):**

| Event | Sent when |
|---|---|
| `task.finished` | A run ended: done, failed, stopped or limited |
| `task.needs_response` | An approval or question is waiting |
| `task.human_message` | You messaged, queued or steered |
| `task.human_responded` | You answered |
| `task.taken_over` | You took a task over |
| `task.handed_over` | You handed a task to Hermes |
| `task.released` | Hermes released a task |

Every payload carries `supervision: {level, epoch}`. Only supervised tasks send
webhooks. If Hermes is unreachable, PandaCode retries with
backoff for about 35 minutes. After that, Hermes can catch up with `list_tasks` and
`read_task`.


## Epoch is required on writes

`send_message`, `respond`, `stop_task` and `release_task` must carry the `epoch` from
`attach_task`, `create_task` or `read_task`; a call without it is refused. The authority is
checked again where the action happens: when the run is claimed (including a team objective),
when an approval, answer or steer is sent to the agent, when a team is stopped, and when a
queued message is delivered (the epoch is captured at dequeue; a stale one is neither sent nor
restored). A takeover that lands in between always wins.
