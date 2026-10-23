"""Actual staged public host APIs + bridge services, not plugin registration."""
import json
from pathlib import Path
from queue import SimpleQueue
import tempfile
from types import SimpleNamespace
import unittest

from broker import BridgeRuntime


class ActualHostBrokerTests(unittest.TestCase):
    def test_actual_cli_canonical_brief_update_and_delivery_after_compression(self):
        from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
        from hermes_state import SessionDB
        # Cleanup stack closes runtime/host DB before removing the fixture.
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        # Private temporary host fixture: never touches live settings.
        (home / 'config.yaml').write_text('plugins:\n  entries:\n    bridge:\n      allow_gateway_injection: true\n')
        manager = PluginManager(scope_key=str(home))
        ctx = PluginContext(PluginManifest(name='bridge', key='bridge', source='user'), manager)
        db = SessionDB(db_path=home / 'state.db')
        self.addCleanup(db.close)
        db.create_session('first', source='cli')
        cli = manager._cli_ref = SimpleNamespace(session_id='first', agent=SimpleNamespace(session_id='first'),
            _pending_input=SimpleQueue(), _should_exit=False, _session_db=db)
        runtime = BridgeRuntime(home / 'plugin-state' / 'pandacode-bridge', ctx.profile_name,
            home, ctx.resolve_origin, ctx.inject_message, b'j' * 32, port=0, interval=60)
        self.addCleanup(runtime.close)
        runtime.observe(session_id='first')
        identity = runtime.identity('first')
        runtime.store.bind(task_id='task', **identity, brief='Original', constraints='Never deploy', authority='Ask')
        db.end_session('first', 'compression')
        db.create_session('second', source='cli', parent_session_id='first')
        cli.session_id = cli.agent.session_id = 'second'
        runtime.observe(session_id='second')
        self.assertEqual(runtime.identity('second'), identity)
        runtime.store.update_brief('task', runtime.identity('second'), brief='Continuation after compression')
        with runtime.store.db:
            runtime.store.db.execute('INSERT INTO inbox VALUES (?, ?, ?, ?, ?)',
                ('canonical', 'digest', 'task', b'{}', 'blocked_host_identity'))
        runtime.dispatcher.drain()
        envelope = cli._pending_input.get_nowait()
        self.assertTrue(envelope.valid(cli, home))
        self.assertEqual(json.loads(envelope.content)['brief'], 'Continuation after compression')
        self.assertEqual(runtime.store.events()[0]['state'], 'accepted')
        runtime.close()
        # Reload under the SAME actual host recovers the host-validated hint,
        # without another synthetic user turn or replaying earlier accepted work.
        recovered = BridgeRuntime(home / 'plugin-state' / 'pandacode-bridge', ctx.profile_name,
            home, ctx.resolve_origin, ctx.inject_message, b'j' * 32, port=0, interval=60)
        self.addCleanup(recovered.close)
        with recovered.store.db:
            recovered.store.db.execute('INSERT INTO inbox VALUES (?, ?, ?, ?, ?)',
                ('after-reload', 'different', 'task', b'{}', 'blocked_host_identity'))
        recovered.dispatcher.drain()
        self.assertEqual(recovered.store.events()[1]['state'], 'accepted')
        self.assertTrue(cli._pending_input.get_nowait().valid(cli, home))


if __name__ == '__main__':
    unittest.main()
