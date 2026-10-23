# Global origin support — integrated candidate, rollout authorised

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

This is the authoritative bridge contract and acceptance matrix. Historical README,
REVIEW and evidence files describe earlier staged slices, not current activation
state. Rollout authorisation cleared the earlier test_plugin execution gate.

## Integrated artifacts

- Host: `cf908e250b9b3b1b098a8decc6f5c76a760a1a1e`, reviewed production delta
  relative to baseline `8f1e5164` (20 Python files). The host's GLOBAL_SUPPORT.md
  records the completed local lifecycle and compute-host implementation.
- Bridge: broker implementation based on `2b691cb8a52adb50528c2a99538ef6f302a5f6b8`,
  plus enabled-entrypoint integration tests and this reconciled matrix.
- Default remains disabled. Installation, owning-process activation and real
  external delivery must be independently read back; this file is not a runtime receipt.

## Contract

`PluginContext.resolve_origin(actual_handler_session_id)` returns exactly
`{profile, session_key, conversation_id}` or None. The actual tool-dispatch identity
must also have been observed through the host pre-LLM hook. A model argument,
webhook field, parent relation or last-hook guess cannot confer ownership.
The private `hermes-origin-v1:` descriptor binds immutable home, host kind,
route key, canonical conversation anchor and generation. It is an identity fence,
not a bearer credential.

Injection uses the exact captured descriptor with current plugin permission at
admission, async preparation, dequeue and final runner boundary. No fresh agent
session, alternate host, home chat, parent, broadcast or rebind fallback exists.
Reset/resume/switch-back revoke generations. SQLite-proven compression retains
canonical identity and allows brief updates from the continuing conversation.
Desktop/CLI payloads and explicit pending ownership are durable before admission;
restart/remount does not replay an uncertain handoff or choose a successor.
Compute children use private supported parent RPC, never renderer messages or
self-created delivery ownership. Route-less machine/child sessions fail closed;
no implicit human recipient or cross-profile grant is provided.

Signed webhook events are untrusted change notices, not commands or authority.
The bridge retains the original brief, constraints and approval boundaries, but
makes no approval decisions. The informed original agent must re-read current
PandaCode state and pending requests before responding. Claims are durable before
handoff. Accepted is not delivered; uncertain outcomes never auto-replay.

## Completed local automated acceptance

| Layer | Verified |
|---|---|
| Host integrated suites | 205 passed, `verification/continuity-final.txt` in host checkout |
| Host published API/lifecycle | CLI and Desktop FIFO, final prepare/runner and compute fences, permission revocation, reset/resume/switch-back, compression, profile isolation |
| Adapter ingress | Actual Photon DM/group/quoted-reply and Discord DM/group/thread metadata through signed inbox and host runner fences; network transport doubled |
| Broker services | Signed bounded admission, dedupe, launch races, brief recovery, exact registered owner, process election/takeover, death during handoff, no stealing live work |
| Disabled entrypoint | `tests/test_plugin.py` executed after authorisation |
| Enabled entrypoint | Real PluginContext registration, native MCP permission/envelope boundary with transport double, canonical origin capture, brief updates, stale update refusal, queue permission revocation and actual manager unload |

Reproduce bridge integration (from bridge checkout):

```sh
PYTHONPATH=.:tests:/path/to/pandacode-bridge-host \
 /path/to/pandacode-bridge-host/.venv/bin/python -W error::ResourceWarning \
 -m unittest test_plugin test_plugin_enabled test_bridge test_dispatch \
 test_identity_brief test_bind_address test_launch_recovery test_early_webhook \
 test_global_origin test_broker test_broker_process test_broker_host -v
```

## Operational requirements and remaining live acceptance

- Install durable local Git source at the actual full commit SHA, disabled first.
  Explicit operator grants: MCP server pandacode and gateway injection; settings:
  enabled=true, expected_identity_contract=host-origin-v1, bind=explicit local
  tailnet address, port=8743. Secrets only through private environment/secret flows.
- All cooperating hosts use the same private profile state and signed-ingress
  settings. One elected HTTP owner admits; exact delivery ownership remains
  per-host. Kernel locks and SQLite are not hostile same-user isolation.
- Apply host delta only against exact baseline hashes; retain private rollback
  source/config copies and baseline/result manifests. Never overwrite unrelated edits.
- Messaging and Desktop are separate owning processes. Plugin reload is not host
  activation. Desktop retirement must be fenced; current gateway reload must wait
  for the owning turn and lease release via a verified detached handoff if needed.
- Configure the remote PandaCode signed webhook only after listener health and
  remote reachability are proven. Read back the exact target privately. Do not
  leave generic webhook approval sessions active in parallel.
- Real originating-chat launch -> callback -> informed response -> exact-target
  delivery readback, busy queueing and a private-context conflicting approval
  remain live acceptance, separately per actual surface. Never fabricate ownership
  bindings or claim code-path doubles prove external platform delivery.
- No global broadcast is authorised. Tests may use only the explicitly authorised
  originating private conversation. Do not send team/group tests.
- Runtime receipts and private manifests, not this source document, establish
  which installation/process/network steps have actually completed.
