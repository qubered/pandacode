"""Authorised enabled entrypoint exercised through real PluginContext and cleanup."""
import importlib.util
import json
import os
from pathlib import Path
from queue import SimpleQueue
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest, _plugin_home_scope
from tools.registry import registry

ROOT = Path(__file__).resolve().parents[1]


class EnabledPluginTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.write_config()
        self.enterContext(_plugin_home_scope(self.home))
        self.enterContext(patch.dict(os.environ, PANDACODE_BRIDGE_WEBHOOK_SECRET='s' * 32))
        self.manager = PluginManager(scope_key=str(self.home))
        self.addCleanup(self.manager.unload)
        self.ctx = PluginContext(PluginManifest(name='bridge', key='bridge', source='user'), self.manager)
        self.cli = self.manager._cli_ref = SimpleNamespace(session_id='first',
            agent=SimpleNamespace(session_id='first'), _pending_input=SimpleQueue(), _should_exit=False)
        spec = importlib.util.spec_from_file_location('enabled_bridge', ROOT / '__init__.py',
            submodule_search_locations=[str(ROOT)])
        self.module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.module
        spec.loader.exec_module(self.module)

    def write_config(self, grant=True, mcp=True):
        entry = {'allow_gateway_injection': grant, 'mcp_allowlist': ['pandacode'] if mcp else [],
                 'settings': {'enabled': True, 'expected_identity_contract': 'host-origin-v1', 'port': 0}}
        (self.home / 'config.yaml').write_text(json.dumps({'plugins': {'entries': {'bridge': entry}}}))

    def handler(self, name):
        return registry.get_entry(name, scope=str(self.home)).handler

    def test_enabled_register_capture_delivery_and_unload(self):
        self.module.register(self.ctx)
        self.assertIsNotNone(registry.get_entry('pandacode_bridge_update_brief', scope=str(self.home)),
                             'enabled entrypoint did not start')
        self.manager._hooks['pre_llm_call'][0](session_id='first')
        result = json.loads(self.handler('pandacode_bridge_update_brief')({'taskId': 'missing'}, session_id='first'))
        self.assertEqual(result['error'], 'brief_update_rejected')
        origin = self.ctx.resolve_origin('first')
        self.assertTrue(self.ctx.inject_message('test', session_key=origin['session_key'],
                                               expected_session_id=origin['conversation_id']))
        self.assertTrue(self.cli._pending_input.get_nowait().valid(self.cli, self.home))
        self.manager.unload()
        self.assertIsNone(registry.get_entry('pandacode_bridge_launch', scope=str(self.home)))
        self.assertFalse(self.manager._hooks.get('pre_llm_call'))

    def test_enabled_permission_revocation_fences_queued_entry(self):
        self.module.register(self.ctx)
        origin = self.ctx.resolve_origin('first')
        self.assertTrue(self.ctx.inject_message('test', session_key=origin['session_key'],
                                               expected_session_id=origin['conversation_id']))
        envelope = self.cli._pending_input.get_nowait()
        self.write_config(grant=False)
        self.assertFalse(envelope.valid(self.cli, self.home))
        self.assertFalse(self.ctx.inject_message('refused', session_key=origin['session_key'],
                                                expected_session_id=origin['conversation_id']))

    def test_enabled_launch_native_mcp_boundary_and_stale_update(self):
        import sqlite3
        self.module.register(self.ctx)
        self.manager._hooks['pre_llm_call'][0](session_id='first')
        args = dict(projectId='demo', prompt='No tool smoke', provider='codex', model='default',
                    mode='ask', constraints='Never deploy', authority='No command approval')
        # Only the remote MCP transport is doubled: real permission and envelope code run.
        with patch('tools.mcp_tool_handlers._make_tool_handler', return_value=lambda args:
                   json.dumps({'result': [], 'structuredContent': {'taskId': 'test-boundary'}})):
            result = json.loads(self.handler('pandacode_bridge_launch')(args, session_id='first'))
        self.assertEqual(result, {'taskId': 'test-boundary', 'mapping': 'bound'})
        db = sqlite3.connect(self.home / 'plugin-state/pandacode-bridge/bridge.sqlite3')
        self.addCleanup(db.close)
        # Exercise the bound retained context through the public update handler.
        result = json.loads(self.handler('pandacode_bridge_update_brief')(
            {'taskId': 'test-boundary', 'brief': 'Updated no-tool brief'}, session_id='first'))
        self.assertTrue(result['ok'])
        mapping = json.loads(db.execute('SELECT data FROM mappings').fetchone()[0])
        self.assertEqual(mapping['constraints'], 'Never deploy')
        self.assertEqual(mapping['brief'], 'Updated no-tool brief')
        self.cli.session_id = self.cli.agent.session_id = 'successor'
        result = json.loads(self.handler('pandacode_bridge_update_brief')(
            {'taskId': 'test-boundary', 'brief': 'Forbidden'}, session_id='first'))
        self.assertEqual(result['error'], 'brief_update_rejected')

    def test_missing_mcp_grant_is_default_deny(self):
        self.write_config(mcp=False)
        self.module.register(self.ctx)
        with self.assertRaises(PermissionError):
            self.ctx.call_mcp('pandacode', 'pandacode_create_task', {})


if __name__ == '__main__':
    unittest.main()
