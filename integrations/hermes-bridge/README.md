# PandaCode → Hermes conversation bridge (source integration)

## Publication verification (2026-10-23)

This is the companion bridge source for this repository's remote-core/Hermes
handover integration, not a second installed plugin. Executable source and tests
match the reviewed frozen candidate. Independent automated evidence: Rust library
980 passed / 0 failed / 32 ignored; combined bridge and real-host boundary tests
68 passed (remote transport doubled); native Mac frontend tests 111 passed and
release-script tests 31 passed. These checks do not establish live delivery,
platform-wide acceptance, installation or activation. This publication does not
merge, release or deploy anything.

The audit and review sections below, and the companion audit/test documents,
preserve earlier checkpoints; their machine-local absence statements and older
counts are historical, not the current verification result. Required current
epochs are enforced by the core on all four write tools. Private handoff metadata
and obsolete snapshot checksums are intentionally not published.

**Supervision update (watch/manage, 2026-10-23):** see [AUDIT.md](AUDIT.md).

What it adds:
- `delegation.py` records each bound task's level (watch/manage) and PandaCode epoch.
- New enabled-only tools: `pandacode_bridge_attach`, `pandacode_bridge_act`,
  `pandacode_bridge_check` and `pandacode_bridge_brief`.
- Writes are fenced locally for watch, revoked or stale delegations.
- Takeover or release revokes the delegation. Late and duplicate callbacks are dropped.
- Notifications are triaged quietly.

The disabled registration is unchanged.

**Grant addition:** `supervision.grant` is recorded in the delegation row. The bridge never
requests or assumes a grant. A denied attach on a Full-mode task becomes a quiet "needs the
user to hand this over explicitly" notice, given once. Losing the grant or a change of epoch
revokes the delegation. Writes are refused locally when a required grant is missing.

**Test status (2026-10-23):** `python3 -I -W error::ResourceWarning -m pytest -q tests`
gives **60 passed, 24 subtests passed, 0 failed**.

| Kind | Tests | Status |
|---|---|---|
| Fake-core | 23: `test_delegation.py` (15) and `test_grant.py` (8) | Executed. Run against FakeCore and stub hosts, so this is NOT host integration. |
| Bridge-local services | 37: the original files that need no Hermes host | Executed |
| Real host | 5: `test_plugin_enabled.py` (4) and `test_broker_host.py` (1) | Not collected and not run. No Hermes source exists on this machine; see [HOST_TESTS.md](HOST_TESTS.md). |

An independent host run with the remote transport doubled reportedly passed five host tests:
`<private-review-evidence>/host-test-result.txt`. This was
reported by Jayden/Gerry. The file is not on this machine, so it is unverified here, and it
predates the delegation and grant changes.

**Negative-mutation checks.** Each fence was removed in a temporary copy, and the tests were
expected to fail:

| Fence removed | Tests that failed |
|---|---|
| watch write fence | `test_watch_is_read_only`, `test_user_takes_over_then_hands_back_same_task`, `test_tools_register_and_refuse_through_the_handlers` |
| takeover/release revocation | `test_takeover_while_running…`, `test_takeover_while_waiting_for_approval`, `test_subagent_brief_and_assert_active`, and the child-capability subtest |
| grant-change revocation | `test_grant_loss_revokes_even_at_the_same_epoch` |
| grant-denial notice | `test_no_grant_denial_is_surfaced_quietly_once` |
| missing-grant write fence | `test_local_write_refused_when_a_required_grant_is_missing` |

These failures are the intended result. The real tree's checksum was unchanged afterwards, and
the unmutated suite is fully green.

Status: UNVERIFIED against a live Hermes host and a live PandaCode core. The grant denial
wording and envelope are assumptions: the backend grant change had not landed in the working tree.

**GLOBAL CORRECTION:** [GLOBAL_SUPPORT.md](GLOBAL_SUPPORT.md) is the authoritative
current contract, tested-surface matrix and blocking-gap list. This working tree
removes the earlier Discord-only identity policy and adds host-origin descriptors.
It is **not deploy-ready or globally working**. The sections below preserve the
pre-global candidate's historical review and must not be used as current scope or
activation instructions. No live integration was changed.

**Broker staging update:** `broker.py` now provides per-host delivery ownership
with one elected signed-ingress listener and shared SQLite claims; it replaces
the historical exclusive-worker mechanism described below. See
[BROKER_CONTRACT.md](BROKER_CONTRACT.md) for the exact existing host APIs, lifecycle,
compression-hint revalidation and no-replay recovery rules. Independent spawned
process and actual staged CLI-host service tests are recorded in GLOBAL_SUPPORT.
Entrypoint execution remains under its original pending approval gate.

See [REVIEW.md](REVIEW.md) for the compatibility review, early-webhook race fix,
explicitly executed tests, remaining gates and approval-only deployment recipe.
The Git pin packages a review candidate, not an activated or release-approved plugin.

This artifact is **not an activated integration**. No host files, configuration,
credentials, or live tasks were changed. Importing the package has no side effects.
Registration is disabled by default: it exposes a launch tool returning
`capture_only_not_enabled` and a no-op capture hook, and opens no listener or database.

## Scope and guarantees

- Decision-free transport: no LLM calls, classification, approvals, automatic task
  responses, deployment, generic-channel sends, or newly created Hermes sessions.
- Initial scope is **top-level Discord DMs only**, not group channels, Desktop,
  CLI, or subagents. Unsupported/missing identity fails closed.
- The model supplies the task brief/provider/model/mode (`plan`, `ask`, or `auto`),
  constraints and authority. It cannot supply a destination/profile/session.
- `pandacode_bridge_launch` calls the existing host MCP client using server
  `pandacode` / tool `pandacode_create_task`. Only a nontruncated successful
  `structuredContent.taskId` result is accepted. This exact envelope still needs
  validation against the deployed PandaCode MCP server; other shapes remain uncertain.
- The returned task ID is bound once to profile, durable routing key and actual
  host session ID. The database field `conversation_id` means this **session ID**,
  not a channel ID or the hook's sometimes differently named conversation field.
  Retargeting/rebinding is forbidden.
- `pandacode_bridge_update_brief` changes only brief/constraints/authority from
  the original host session, retaining revision history. It does not send a task
  message or approve a command. A changed authority text is context, not a new
  machine-level permission grant.

## Actual host identity capture

The implementation does not guess a DM from sender ID, use a configured home
channel, scrape assistant text, or save a process-global "last conversation".

Source inspected in `/path/to/hermes-agent`:

- `hermes_cli/plugins.py`: `PluginContext.register_tool`, `register_hook`,
  `profile_name`, `call_mcp`, `inject_message`, `on_unload`.
- `agent/turn_context.py` `_collect_pre_llm_call_context`: the real hook passes
  `session_id`, `platform`, `parent_session_id` (plus other host metadata), but
  **does not pass the gateway routing key**.
- `agent/agent_runtime_helpers.py` `invoke_tool`: host supplies the actual
  `session_id` in dispatch metadata; no parent agent handle is assumed.
- `model_tools.py` and `tools/registry.py`: the actual handler is called as
  `handler(args, **kwargs)` with host IDs, signature-filtered.
- `hermes_state_common.py`: sessions have `id`, `source`, `session_key`,
  `chat_type`, `ended_at`, `parent_session_id`.

The plugin pins `get_hermes_home()` and `ctx.profile_name` during enabled
registration. A hook observes eligible host session IDs. Each tool call uses its
**own metadata `session_id`**, requires prior observation, and reads that exact
session from the same profile's `state.db` in SQLite `mode=ro`. Only an unended
root Discord DM with a real routing key is accepted. No host database creation,
migration or write occurs. Missing/new host schema is a hard failure, not a
fallback. Hook observation is in memory; a normal host turn re-establishes it
following restart. Durable existing mappings do not depend on this observation.

## Required host extension — NOT assumed available

The staged host artifact `/path/to/pandacode-bridge-host` at commit
`bb241ae0` provides `expected_session_id`; this is **not evidence the live gateway
has loaded it**. Existing origin-based gateway injection can start a chat session
and is unsafe for this bridge. The plugin never uses `origin` or falls back to
old injection.

The independently prepared host extension must provide this explicit contract:

```python
ctx.inject_message(content, role='user', *, session_key=...,
                   expected_session_id=...) -> bool
```

For a request carrying `expected_session_id`, the host must:

1. Enforce the plugin's profile and normal operator injection permissions.
2. Resolve **only an existing** session for that exact durable routing key, and
   compare its current session identity to the expected ID. Never create a session,
   choose the generic home channel, follow an alias to a new conversation, or
   strip the expected identity on compatibility fallback.
3. Preserve/recheck the identity through queueing and actual execution so reset,
   switch, resume and concurrent replacement cannot send old task data into an
   unrelated successor. A pre-enqueue-only comparison is insufficient.
4. Route the normal host result to the same Discord DM; private data must never
   be promoted into a group/thread or a different active conversation.
5. Return exactly `True` for accepted handoff and `False` for definite refusal.
   Acceptance is **not** proof of delivery/completion. Exceptions/non-booleans
   mean unknown outcome to the bridge.

Detection requires explicit named `session_key` and `expected_session_id`
parameters; `**kwargs` alone does not count. Additionally the operator must
acknowledge the reviewed contract with the setting
`expected_identity_contract: expected-session-id-v1`. A setting cannot create
host support or establish its semantics. This is a compatibility gate, not a
claim that signature inspection proves lifecycle safety. Until the host extension
and its messaging lifecycle tests are independently verified, **do not enable**.
The bridge has no host monkeypatch or fallback and makes no host edits.

## Signed ingress and remote reachability

`Listener` accepts bounded POST `/pandacode` only, max 64 KiB, one Content-Length,
no transfer encoding, and a bounded socket timeout. It validates
`X-Webhook-Signature-V2 = hex(HMAC-SHA256(secret, timestamp + '.' + raw_body))`,
`X-Webhook-Timestamp` within ±300 seconds, and a bounded `X-Request-ID`.
The task must already have a local mapping. Invalid authentication is 401;
unknown task is 404; stored is 202; a replay is 200; same request ID with changed
body is 409. Keep both clocks synchronized. HMAC is over original bytes; do not
parse/re-encode the JSON in a proxy. The request ID is not signed, so a durable
body digest also suppresses replay under a changed ID. Identical bodies are
coalesced even under different IDs; no semantic duplicate inference is attempted.
Recognized event types are `task.finished`, `task.needs_response`,
`task.human_message`, `task.human_responded`, `task.taken_over`,
`task.handed_over`, `task.released`.

The current upstream fork documentation confirms the Generic V2 signing headers,
event names and headless remote-core design:
[Hermes integration](https://github.com/qubered/pandacode/blob/main/docs/hermes.md),
[remote core](https://github.com/qubered/pandacode/blob/main/docs/remote-core.md).
Its generic webhook recipe starts an agent session and is deliberately not used
here. Documentation is not proof of the deployed server's exact result envelope.

Default bind is loopback. **Remote PandaCode cannot reach 127.0.0.1 on Hermes.**
The supported prepared remote design is a direct **authenticated tailnet bind**:

- Set `bind` to the Hermes machine's explicit local Tailscale IPv4 in
  `100.64.0.0/10` or IPv6 in `fd7a:115c:a1e0::/48`. Wildcard, hostname, arbitrary
  LAN and public addresses are rejected. The bind fails if the address is not local.
- PandaCode's execution host must itself have a verified route into that tailnet.
  Configure its webhook destination to `http://<Hermes-tailnet-IP>:<port>/pandacode`
  (IPv6 URL needs brackets), only after authorisation. Tailnet encrypts the
  transport; application HMAC authenticates each request. Tailnet ACLs/firewall
  should restrict the port to the PandaCode host; a tailnet IP alone is not auth.
- Provision a matching high-entropy shared secret of at least 32 bytes through
  each application's private environment/secret flow. Plugin environment name:
  `PANDACODE_BRIDGE_WEBHOOK_SECRET`. Never put it in this README, manifest,
  config settings, command history, or logs.
- No tailnet endpoint, ACL, PandaCode webhook URL, secret, DNS record, service,
  or firewall rule was configured here. Only loopback HTTP has been exercised.
  Both remote reachability and signed real-provider delivery remain unverified.

Do not point this at a generic Hermes webhook route which launches a new agent
session. An alternative forwarding route would have to preserve the signed raw
body and headers and forward to this inbox without running an agent; no such
route is included or assumed installed.

## Persistence, dedupe and uncertain outcomes

Enabled runtime uses profile-local
`plugin-state/pandacode-bridge/bridge.sqlite3` with 0700 directory/0600 DB,
SQLite FULL synchronization, private owner lock, and supervised unload cleanup.
The exclusive process lock prevents two active owners recovering each other's
in-flight claims. There is one bounded HTTP worker and one decision-free dispatch
thread; shutdown waits for any current handoff before closing storage.

- `launches`: args and trusted origin are committed as `uncertain` **before**
  calling remote creation. Exceptions, truncated/unexpected result, duplicate
  task binding, or crash leave it uncertain. Same-origin launches are blocked
  while any uncertain launch exists. There is no automatic external create retry.
- `mappings`: durable task ID and immutable origin plus retained brief context.
- `brief_revisions`: original/current context is retained on first edit, then
  each subsequent revision. Updates cannot modify any identity field.
- `inbox`: unique request ID and body digest, committed before 202 response.
  Unknown-task events are not stored. While any launch remains uncertain, they
  receive retryable 503 `binding_pending`; otherwise they receive 404. This handles
  a task finishing before the create response can establish its binding without
  trusting event data as a destination. Reviewed PandaCode source does **not** retry
  404; it retries 429/5xx/network failures. Crashed uncertain launches still require
  operator reconciliation and sender retries eventually expire.
- Dispatch atomically claims `blocked_host_identity` → `dispatching` before host
  handoff, always using the stored profile/key/expected ID, never payload targets.
  A verified compatible dispatcher can drain stored blocked rows after startup.
- Exactly `True` becomes `accepted` (not "delivered"). Exactly `False` becomes
  terminal `rejected_identity`; do not retarget that event after a reset/switch.
  Exception/non-boolean becomes `uncertain`. Restart converts abandoned
  `dispatching` to `uncertain`; these rows are never blindly replayed.
- No exactly-once end-to-end delivery is claimed: a crash after acceptance but
  before receipt persistence is ambiguous. An operator must compare host
  transcript/queue receipts (use the request ID in message content), stored raw
  event and PandaCode task state before considering any manual correction.
  There is intentionally no auto-rebind, unsafe replay, deletion or "mark done"
  tool. Recovery tooling for ambiguous launches/handoffs is not included.

External event JSON is marked **untrusted data**, packaged with brief,
constraints and authority, and sent only into the original conversation. The
bridge never decides a command response or widens permissions. This label is
not a substitute for normal host prompt-injection and approval defenses.
Private DB contents include task prompts/events; retain and back them up as
sensitive data. No event-body or signed-header logs are emitted.

## Configuration contract (not applied)

Plugin settings, if an authorised operator later installs it, are read via
`ctx.get_config` under `plugins.entries.<effective-plugin-id>.settings`:
`enabled` (false), `expected_identity_contract` (empty), `bind` (127.0.0.1),
`port` (8743). Normal host top-level plugin grants must separately allow MCP
server `pandacode` (`mcp_allowlist`) and gateway injection
(`allow_gateway_injection`). The plugin does not grant itself capabilities or
change policy. Use supported Hermes configuration commands, not hand-edited
live YAML. This document is not installation approval.

## Verification and remaining blockers

Independent tests were written, observed failing for missing behavior, then run
successfully after implementation. Executed explicitly, without broad discovery:

```text
python -m unittest discover -s tests -p test_bridge.py -v            # 10 passed
python -m unittest discover -s tests -p test_dispatch.py -v          #  5 passed
python -m unittest discover -s tests -p test_identity_brief.py -v    #  2 passed
python -m unittest discover -s tests -p test_bind_address.py -v      #  1 passed
python -m unittest discover -s tests -p test_launch_recovery.py -v   #  1 passed
```

`tests/test_plugin.py` is the existing pending approval slice. **It has not been
run**, and the pending `execute_code` gate `assert run('11-red') != 0` was neither
retried nor rephrased. The entrypoint is prepared and syntax-checked only;
its host registration, enabled lifecycle, permission failures and real provider
MCP envelope are not integration-tested. Do not run unfiltered test discovery
while this approval remains pending. No installation/activation was attempted.

Remaining acceptance gates: authorised plugin-boundary tests; reviewed host
expected-identity implementation and reset/switch/queue race tests; exact deployed
MCP and webhook contracts; authorised remote signed event path; real launch →
event → existing-DM transcript/result readback; identity rejection after reset;
crash/uncertain reconciliation with actual host receipts. Local doubles prove
bridge state transitions, not real Discord delivery or host lifecycle safety.
