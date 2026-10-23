# Audit: the bridge against the watch/manage operating model

Source audited: the installed `pandacode-bridge` snapshot (commit `f7ba2d1b`, staged at
`<source-handoff-directory>`, left unmodified).
Backend contract: the orchestrator changes being landed in `src-tauri/src/orchestrator`
(`pandacode_attach_task`, `supervision {level, epoch}` on tasks and webhooks, epoch-checked
writes, `release_task` handover, guarded webhook delivery). Hermes-side status:
**tested against fakes only, UNVERIFIED against a live Hermes host or a live PandaCode core.**

## Operating model

The user is in charge. Gerry (Hermes) helps only when asked.

- **Workflow 1:** the user starts a task in PandaCode, then asks Gerry to WATCH it (observe, flag
  meaningful problems; never message, approve or stop) or MANAGE it (guide it and handle
  routine decisions within the brief).
- **Workflow 2:** Gerry launches a task from a Discord DM, and the user takes it over later.

Either way it stays the same task, history and agent. After a takeover, Gerry and his subagents
stop acting. Only a genuine decision, an important blocker or a useful completed result reaches
the user.

## Gaps found in the original

| # | Area | What the original did | Gap |
|---|---|---|---|
| 1 | Attach to an existing task | Only `pandacode_bridge_launch` (create_task). | No way to watch or manage a task the user started (workflow 1). |
| 2 | Watch vs manage | No level concept. `authority` was free text, "context, not a permission grant". | Nothing stopped a watching Gerry from messaging, approving or stopping. |
| 3 | Epoch | Unknown to the bridge. | Writes could not be fenced to the current delegation, and stale events could not be recognised. |
| 4 | Takeover | `task.taken_over` / `task.released` were stored and **delivered** like any other event. | No revocation. Gerry was woken to react to the user's own takeover, and nothing stopped later actions. |
| 5 | Late or duplicate callbacks | Deduped by X-Request-ID and body digest only. | A delayed event from before a takeover was still delivered and woke Gerry. |
| 6 | Notification noise | Every stored event was injected into the DM conversation, each one a turn. | Routine approvals, human-caused events (`human_message`, `human_responded`), the user's own stops and bursts each woke the conversation, so the user could hear about every one. |
| 7 | Subagents | No brief or state for background subagents. Child sessions fail closed for identity (kept). | Subagents could not learn the level or epoch, and had no "am I still delegated?" check. |
| 8 | Approval handling | The bridge itself makes no approval decisions (kept). Responses go straight to PandaCode MCP. | No local gate and no epoch on those calls. |
| 9 | Handover | None. | No changed/checked/open/next summary when Gerry hands the task back. |
| 10 | Return direction | `task.handed_over` was delivered as data only. | The user handing a task back to Gerry did not renew the delegation. |
| 11 | Launch result | Read only `taskId`. | The `level` / `epoch` from create_task were not recorded. |
| 12 | Test runner | unittest with `PYTHONPATH=.:tests`. | `python3 -I -m pytest` could not collect: the root `__init__.py` was imported as a package. |

## Closed here (minimum, same design)

| Gaps | Fix |
|---|---|
| 1, 11 | `delegation.Attacher` and the `pandacode_bridge_attach` tool (enabled path only). It is bound to the calling conversation exactly like launch: host-resolved identity, retargeting forbidden, the same uncertain ledger (an early event gets a 503), and only a verified `{taskId, level, epoch}` is accepted. Launch now records `level` and `epoch`. |
| 2, 3, 8 | A `delegations` table (level, epoch, state). `delegation.Actor` / `pandacode_bridge_act` refuses `send_message`, `respond` and `stop_task` locally for watch, revoked or missing delegations. It always sends the recorded epoch, so PandaCode refuses a stale call as well; callers cannot override `taskId` or `epoch`. |
| 4, 5, 10 | `delegation.classify` runs inside the inbox's write transaction:<br>- **Taken over or released:** revoke, cancel anything still waiting, store it silently.<br>- **Event from an older epoch or a revoked delegation:** stored, but marked `dropped_stale` (HTTP 200, never delivered). The same X-Request-ID gets 200 `duplicate`.<br>- **Newer epoch with no hand-over:** revoke (fail closed).<br>- **Signed `handed_over` at a newer epoch:** renews the delegation at that level.<br>Before every handoff, the dispatcher checks again that the delegation is still active at the same epoch. |
| 6 | `delegation.triage` is a fixed table with no content judgement:<br>- **Silent:** human-caused events, a run stopped by the user (or by Gerry), and requests on a watched task.<br>- **Quiet:** routine requests and hand-overs.<br>- **Result:** a finished run under manage.<br>- **Blocker:** failed or limited runs, at most once per delegation.<br>Waiting events coalesce to the newest per task, and that one lists the rest under `superseded`. Delivered messages carry `delegation`, `notify` and `rules`. |
| 7 | `delegation.brief` / `pandacode_bridge_brief`: the original brief, constraints, authority, level, epoch, task ID and rules. `pandacode_bridge_check` (read-only, no identity needed, so subagents can call it) returns whether the delegation is active at an epoch. |
| 9 | `handover_summary` / `handover_text`: changed/checked/open/next, each capped at 1500 bytes like the backend. `act release_task` validates the note, sends it and revokes the delegation locally. |
| 12 | `tests/conftest.py` and `tests/pytest.ini`. Real-host suites are skipped visibly when `hermes_cli` is absent. |

## Grant addition (Full-mode tasks above maxMode)

| Need | Done |
|---|---|
| Never request or assume a grant | The bridge only reads `grant` from the attach result or signed events. No tool asks for one, and a denied attach is never retried. |
| Denial surfaced once, quietly | When a refused attach mentions a grant, the attach tool returns `needs_user_grant`: `quiet` with the hand-over notice the first time per task and conversation (`grant_denials` table), `silent` after that. Nothing is bound. |
| Grant recorded | `grant` and `requires_grant` are stored in the delegation row and shown in the subagent brief. |
| Grant loss or epoch change revokes | A different grant, even at the same epoch, revokes the delegation (`grant_lost`). The dispatcher also checks the grant before handoff. |
| Local write refusal | `assert_active(write=True)` raises `grant_missing` when a required grant is absent. |

UNVERIFIED: denial detection matches "grant" in PandaCode's error text, because the backend
envelope is not landed. A core that sends no `grant` field keeps the earlier behaviour.

**Child capabilities.** After a lost grant, an epoch change or a takeover, a subagent's
`brief`, `assert_active` (read and write) and `act` are all refused.
`test_child_capabilities_end_on_grant_loss_epoch_change_or_takeover` covers this.

## Still open (not closable from the plugin, or UNVERIFIED)

- **Direct MCP calls bypass the local fence.** If Hermes also has PandaCode's MCP server
  configured directly, Gerry can call `pandacode_send_message` and the others without
  `pandacode_bridge_act`. They are then guarded only by PandaCode's own checks: its level check
  always, and its epoch check only when an `epoch` is passed, which the brief and rules require.
  A host pre-tool hook could enforce this, but none was verified in the host, so none is
  registered.
- **Quiet is advisory.** The notify policy reaches the conversation as data. Whether a "quiet"
  turn actually produces no Discord message depends on the host and the model. No live host
  support for a silent turn was verified.
- **A small race remains.** The check before handoff and the injection are not atomic with a
  takeover that lands in between. Any action that follows is still refused, locally and by
  PandaCode.
- **Ordering assumptions.** Takeover and release webhooks are assumed to carry the
  pre-change epoch (`release_task` sends its snapshot). An ending event with a newer epoch also
  revokes. Re-attaching at the same level is assumed to keep the epoch.
- **The `pandacode_attach_task` envelope is unverified.** `{taskId, level, epoch}` in
  `structuredContent` is taken from the working-tree backend, not from a deployed core.
- **Original gates unchanged.** Every acceptance gate in GLOBAL_SUPPORT.md and REVIEW.md still
  applies: host identity contract, signed remote path, real DM delivery. The real-host suites
  (`test_plugin_enabled`, `test_broker_host`) were not run here.
