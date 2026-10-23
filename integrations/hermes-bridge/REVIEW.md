# Compatibility review — candidate only, not installed

## Outcome

One deploy blocker was fixed with an independent RED→GREEN test: a task can emit
its completion webhook before `create_task` returns. The old unknown-task 404
permanently lost that event because PandaCode does not retry 404. While a launch
is durably uncertain, unknown signed tasks now receive 503 `binding_pending`;
no untrusted binding is created. Once the response binds the task, sender retry
can be stored normally. Uncertain launches still require manual reconciliation;
retry exhaustion is not repaired by this change.

Explicitly executed test files: `test_early_webhook.py` (1), `test_bridge.py` (10),
`test_dispatch.py` (5), `test_identity_brief.py` (2), `test_bind_address.py` (1),
`test_launch_recovery.py` (1). All passed. The new test first failed with the
expected 404-versus-503 assertion, then passed after the fix.

**Not executed:** `tests/test_plugin.py`, its pending approval command, plugin
doctor, installation, service configuration/restart, or active gateway tests.
The entrypoint remains source-reviewed, not integration-tested.

## Reviewed contracts

- Host artifact: `/path/to/pandacode-bridge-host`, commit `bb241ae0`.
  Parent reports its 20 focused tests passed; this reviewer did not rerun them.
- `PluginContext.register_tool` forwards a flat function schema, not an OpenAI
  outer wrapper. `tools/registry.py` adds `name` when exposing it; the bridge's
  omitted schema `name` is **not a blocker**. No speculative schema change made.
- `pre_llm_call` supplies keyword `session_id`, `platform`, `parent_session_id`;
  `capture(**metadata)` accepts it. Tool dispatch supplies its own session ID.
  Resolution reads only that observed root Discord DM from profile `state.db`.
  Missing/ended/group/child identities fail closed, with no latest-hook fallback.
- Enabled registration captures `get_hermes_home()` and `ctx.profile_name` once.
  The latter is a property in this host, not a method. Profile-scoped registration
  must remain intact; the background dispatcher never infers an ambient profile.
- Exact injection explicitly passes `session_key` and `expected_session_id`,
  never `origin`. The staged host API exists and refuses unsupported local queues;
  a signature alone does not establish its installed/runtime lifecycle safety.
- PandaCode source reviewed at `f2fdbc9bcc7ef6d7d3cdb787c2606a02db3de8f7`:
  `orchestrator/tools.rs` returns an object containing `taskId`; `mcp.rs` emits a
  summary plus JSON and `structuredContent`. Hermes `mcp_tool_handlers.py` keeps
  structured content when text includes that summary, and `_mcp_envelope` retains
  it alongside `ok`. This matches Launcher. Structured-only or faithful dual-emit
  variants can instead become `result` and deliberately remain uncertain here;
  do not loosen parsing without verifying the actual deployed contract.
- Signing matches timestamp + dot + raw bytes. Timestamp bounds, HMAC, durable
  request-ID/body-digest dedupe, claim-before-handoff and no automatic uncertain
  replay are covered by the executed core tests. Request ID is unsigned; body
  digest prevents changing only the ID from causing a second dispatch. Storage
  and owner lock are private. Authentication does not grant event instructions
  approval authority.

## Remaining release gates

1. Approval and execution of the existing entrypoint slice, then real enabled
   PluginContext lifecycle/permission tests. Do not run broad test discovery while
   that approval is outstanding. This review does not establish enabled startup.
2. Authorised staging/activation of the host patch while preserving the heavily
   dirty live checkout. Read back installed bytes and replacement runtime identity.
3. Real provider MCP result and signing/retry contract, authenticated tailnet path,
   grant/config readback, and exact originating Discord DM tool availability.
4. From that DM, launch a bounded no-write Ask task through the bridge launch tool
   with a restriction present only in the original chat. Verify persisted binding,
   signed event acceptance, existing-session turn and exact-DM result readback.
   Read current task/pending request before any response; acceptance is not delivery.
5. Exercise a busy-origin queued event, reset/switch rejection, compression refusal,
   and uncertain/crash reconciliation without replaying external side effects.

Desktop, CLI, group-channel origins and compression lineage are unsupported, not
unfinished functionality that can be claimed working. Compression changes identity
and therefore stops delivery. No recovery UI or automated catch-up is included.

## Deployment recipe — only after approval, not performed

1. Preserve private source/config backups and overlay manifest; verify the prepared
   host patch against actual source bytes before applying only its reviewed files.
   Do not reset, overwrite or commit unrelated live edits. Arrange authorised
   graceful activation and read back new process/code identity.
2. Resolve the candidate's full commit with `git -C
   /path/to/pandacode-hermes-bridge rev-parse HEAD`, then install that
   SHA via `hermes plugins install
   file:///path/to/pandacode-hermes-bridge --ref <FULL_SHA> --no-enable`.
   Read back the effective plugin ID and revision. This pin is a review candidate,
   not a release approval. Keep its named Git branch reachable.
3. Use supported `hermes config set` for the effective ID's top-level
   `mcp_allowlist` (`["pandacode"]`) and `allow_gateway_injection` (`true`), and its
   `.settings.expected_identity_contract` (`expected-session-id-v1`), `.settings.bind`
   (verified local tailnet IP), `.settings.port` and `.settings.enabled` (`true`).
   Grant no unrelated capabilities. Provision the shared >=32-byte secret through
   private environment/secret flows, never config settings or chat.
4. Configure PandaCode's webhook to that tailnet IP/port `/pandacode`, not Hermes's
   generic webhook-agent route. Verify route/ACLs and matching secret privately.
   Use supported plugin enable after approval, then read back settings/revision,
   registration and listener ownership. Host code requires owning-process reload;
   plugin registration alone does not activate the new host API.
5. Run the exact-DM acceptance sequence above using newly available gateway tools.
   Do not create a fresh webhook agent, send to a generic home channel or mistake
   `accepted` inbox state for delivered output. Stop and reconcile on uncertainty.
