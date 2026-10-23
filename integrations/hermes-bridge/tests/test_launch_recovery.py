import tempfile
from pathlib import Path
import unittest
import bridge


class LaunchRecoveryTests(unittest.TestCase):
    def test_uncertain_launch_blocks_same_origin_retry_without_second_mcp_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = bridge.Store(Path(tmp) / 'private' / 'bridge.sqlite3')
            self.addCleanup(store.close)
            calls = []
            def mcp(*args):
                calls.append(args)
                raise TimeoutError('external task may exist')
            args = dict(projectId='p', prompt='Build', provider='codex', model='default',
                        mode='ask', constraints='No deploy', authority='Ask')
            identity = dict(profile='default', session_key='route', conversation_id='A')
            for _ in range(2):
                with self.assertRaises((ValueError, TimeoutError)):
                    bridge.Launcher(store, mcp).launch(args, identity)
            self.assertEqual(len(calls), 1, 'uncertain launch was blindly replayed')
            self.assertEqual(len(store.launches()), 1)
