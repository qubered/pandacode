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
  sidebar marks them.
- **Approvals and questions:** on those tasks, either of you can answer, and the first
  answer wins. Hermes cannot answer on tasks you supervise.
- **Take over:** in the task header, this makes a task yours again. *Let Hermes
  supervise* hands one of your tasks to Hermes.
- **Highest mode:** Hermes cannot start tasks above this setting. The default is Accept
  edits, so shell commands still ask for approval.
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

The tools are:
- `pandacode_list_projects`, `add_project`
- `list_tasks`, `search_tasks`, `read_task`
- `create_task`, `send_message`, `respond`
- `release_task`, `stop_task`
- `wait_for_task`, `list_models`

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

Only supervised tasks send webhooks. If Hermes is unreachable, PandaCode retries with
backoff for about 35 minutes. After that, Hermes can catch up with `list_tasks` and
`read_task`.
