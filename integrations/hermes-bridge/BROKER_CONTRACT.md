# Minimum host contract for multi-process bridge (bridge implementation staged)

Inspected actual staged `hermes_cli/plugins.py`, `plugins_origin.py` and public
`PluginContext.resolve_origin` / `inject_message` APIs. No imaginary broker host
API is required: shared SQLite + kernel locks live entirely in this plugin.
Do not modify host code to implement a second HTTP broker.

Required existing public API:

- `ctx.resolve_origin(actual_tool_session_id)` returns exactly
  `{profile, session_key, conversation_id}` or None; exact owning process only.
- Token format is `hermes-origin-v1:` followed by JSON containing exactly
  `host, home, key, anchor, generation` (nonempty strings). Host kind distinguishes
  gateway/local/CLI; generation fences route lifecycle, NOT bridge process UUID.
- `ctx.inject_message(content, *, session_key, expected_session_id)` returns
  True only for admission, False only for definite non-admission. Exceptions or
  any other return mean uncertain, never replay. It must revalidate permission,
  immutable home, generation and exact destination at final execution boundary.
- Compression recapture of an actual descendant handler session ID must return
  the identical canonical descriptor. This preserves immutable brief-edit identity.
  The broker durably stores that **host-observed session ID** as a lookup hint,
  never as authority. On reload it asks the actual host to resolve the hint and
  requires exact equality with the bound descriptor. An anchor lookup is also
  attempted; resolving old anchors is helpful but not a required additional API.
  Reset/switch/replacement must not preserve the canonical identity. If the
  current host cannot prove continuation, return None (undelivered, no fallback).

Plugin broker records the full exact token, verifies resolver possession before
registration and again before every SQL claim. Another process cannot acquire a
route simply by reading its mapping. Missing/ambiguous ownership blocks delivery;
parent/child ancestry and model fields never confer ownership. New bridge owners
may adopt only **unclaimed** work and only after exact host recapture. Crash during
handoff becomes uncertain; accepted host queues are never blindly reinjected even
if that host later dies. Host queue loss requires explicit reconciliation, not a
new automatic bridge replay API. No host callbacks beyond the existing APIs above
are requested. Kernel locks/SQLite require same local filesystem and cooperating
processes under one user; this is not a cross-machine or hostile-plugin boundary.

Lifecycle: `register` disabled => inert tool/capture handler only; enabled => one
shared-inbox service per host process, generation UUID + liveness lock, explicit
origin registration from host hooks/verified mappings, elected HTTP ingress.
Unloading stops/joins worker before closing listener, retiring registrations and
closing database. In-flight synchronous injection may delay unload (safer than
closing storage underneath a possibly accepted handoff). No wall-clock lease can
steal ownership from a live stalled process. A dead ingress loses its kernel lock;
one follower binds after election. A busy external port is retried, never stolen.

All new owners hold a shared migration fence on historical `owner.lock`; a live
old exclusive-worker build blocks new activation rather than mixing recovery
protocols. Owner UUID lock files are intentionally not unlinked while processes
may reference their inodes; they contain no credentials. Co-hosts must share this
same local profile directory (not a network filesystem or different machine).

All cohosts require matching bind/port/HMAC-secret fingerprint. Mismatch fails
closed. Offline configuration rotation is permitted when there are no other
registered live owners. Keep disabled until parent host final lifecycle fences,
canonical recapture and the separately approval-pending entrypoint gate pass.
