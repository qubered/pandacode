# Real-host test suites (skipped on this machine)

Two files need the actual Hermes host code. When `hermes_cli` cannot be imported,
`tests/conftest.py` leaves them out, so `pytest` lists them as not collected rather than passed.

| File | Tests | Host modules and APIs it uses |
|---|---|---|
| `tests/test_plugin_enabled.py` | 4: enabled register/capture/delivery/unload; permission revocation fences a queued entry; launch through the native MCP boundary plus a stale brief update; a missing MCP grant is denied by default | `hermes_cli.plugins`: `PluginContext`, `PluginManager`, `PluginManifest`, `_plugin_home_scope`.<br>`PluginManager._cli_ref`, `._hooks`, `.unload()`.<br>`PluginContext.resolve_origin`, `.inject_message(content, *, session_key, expected_session_id)` (the queued envelope's `.valid(cli, home)`), `.call_mcp`, `.profile_name`, `.get_config`.<br>`tools.registry.registry.get_entry(name, scope=)`, `tools.mcp_tool_handlers._make_tool_handler` (patched).<br>`hermes_constants.get_hermes_home`.<br>Plugin config in `<home>/config.yaml` (`plugins.entries.<key>`: `allow_gateway_injection`, `mcp_allowlist`, `settings`). |
| `tests/test_broker_host.py` | 1: canonical CLI brief update and delivery after compression, restart recovery | The `hermes_cli.plugins` classes above.<br>`hermes_state.SessionDB(db_path=)`: `.create_session(id, source=, parent_session_id=)`, `.end_session(id, reason)`, `.close()`.<br>The CLI `_pending_input` envelope's `.valid(cli, home)`. |

## Which Hermes source

These are not stock Hermes APIs. `resolve_origin`, the `hermes-origin-v1:` descriptor,
`inject_message(..., expected_session_id=)` and envelope `.valid()` come from the **staged
host extension**: `pandacode-bridge-host` at commit
`cf908e250b9b3b1b098a8decc6f5c76a760a1a1e`. That is a reviewed delta against baseline
`8f1e5164`, per GLOBAL_SUPPORT.md. The original run was:

```sh
PYTHONPATH=.:tests:/path/to/pandacode-bridge-host \
  /path/to/pandacode-bridge-host/.venv/bin/python -W error::ResourceWarning \
  -m unittest test_plugin_enabled test_broker_host -v
```

The version string of the matching upstream `hermes-agent` is not recorded in this snapshot.

Running both files against an unpatched Hermes is expected to fail:
- `resolve_origin` is missing, so the plugin's enabled path stops at `host_expected_identity_unavailable`;
- `_cli_ref` and envelope `.valid()` are missing.

## On this machine (2026-10-23): absent, not run

Read-only search, without opening any file contents:
- `find / -xdev` for `hermes_cli`, `hermes_constants.py`, `hermes_state*`, `hermes-agent` and `pandacode-bridge-host`: no matches.
- `python3 -c "import hermes_cli"` fails with `ModuleNotFoundError`.
- `~/.hermes` contains only `cache/scratch`, with no source tree.
- `<review-host-home>` does not exist.

So the two files **were not run**. Nothing was installed, activated or restarted, and no
credentials were read.

## Reported elsewhere (unverified here)

Jayden/Gerry report an independent run with the remote transport doubled: five host tests
passing, in `<private-review-evidence>/host-test-result.txt`.
That path is not on this machine (`<review-host-home>` does not exist), so the result cannot be checked
here. It also does not cover the delegation or grant code added in this copy.

## Running them where the host exists

Use the staged host checkout, read-only, with a throwaway HOME:

```sh
cd integrations/hermes-bridge
HOME=$(mktemp -d) PYTHONPATH=.:tests:/path/to/pandacode-bridge-host \
  /path/to/pandacode-bridge-host/.venv/bin/python -W error::ResourceWarning \
  -m pytest -q tests/test_plugin_enabled.py tests/test_broker_host.py
```

The tests make their own temporary Hermes home through `_plugin_home_scope` and `SessionDB`.
They never touch live settings.

The enabled path now also registers `pandacode_bridge_attach`, `_act`, `_check` and `_brief`.
These tools have not been registered through the real `tools.registry` (unverified). The
`EnabledToolTests` in `test_delegation.py` uses a stub context, not the host.
