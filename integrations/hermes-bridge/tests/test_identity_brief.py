"""Host-owned identity and brief services; independent of pending entrypoint slice."""
from pathlib import Path
import sqlite3
import tempfile
import unittest
import bridge


class IdentityBriefTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        db = sqlite3.connect(self.home / 'state.db')
        db.execute('CREATE TABLE sessions (id TEXT, source TEXT, session_key TEXT, chat_type TEXT, ended_at REAL, parent_session_id TEXT)')
        db.executemany('INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)', [
            ('A', 'discord', 'discord:dm:A', 'dm', None, None),
            ('B', 'discord', 'discord:dm:B', 'dm', None, None),
            ('child', 'discord', 'discord:dm:A', 'dm', None, 'A'),
            ('ended', 'discord', 'discord:dm:A', 'dm', 10, None),
            ('group', 'discord', 'discord:group:X', 'group', None, None)])
        db.commit()
        db.close()

    def test_identity_is_resolved_from_exact_host_session_not_latest_hook_or_args(self):
        self.assertTrue(hasattr(bridge, 'IdentityCapture'), 'IdentityCapture is missing')
        origins = {sid: dict(profile='default', session_key='route:' + sid,
                             conversation_id='hermes-origin-v1:' + sid) for sid in ('A', 'B')}
        capture = bridge.IdentityCapture(origins.get, 'default')
        capture.observe(session_id='A', platform='discord', parent_session_id='')
        capture.observe(session_id='B', platform='photon', parent_session_id='')
        self.assertEqual(capture.resolve('A'), origins['A'])
        self.assertEqual(capture.resolve('B')['session_key'], 'route:B')
        self.assertIsNone(capture.resolve('missing'))
        self.assertIsNone(capture.resolve('child'))
        self.assertIsNone(capture.resolve('ended'))
        self.assertIsNone(capture.resolve('group'))
        self.assertIsNone(capture.resolve(None))

    def test_brief_update_requires_same_host_identity_and_cannot_change_target(self):
        self.assertTrue(hasattr(bridge.Store, 'update_brief'), 'brief updater is missing')
        store = bridge.Store(self.home / 'private' / 'bridge.sqlite3')
        self.addCleanup(store.close)
        identity = dict(profile='default', session_key='discord:dm:A', conversation_id='A')
        store.bind(task_id='task-1', **identity, brief='Original', constraints='No deploy', authority='Ask')
        with self.assertRaises(ValueError):
            store.update_brief('task-1', {**identity, 'conversation_id': 'B'}, brief='Hijack')
        with self.assertRaises(ValueError):
            store.update_brief('task-1', identity, session_key='new')
        store.update_brief('task-1', identity, brief='Correction')
        mapping = store.mapping('task-1')
        self.assertEqual(mapping, dict(task_id='task-1', **identity, brief='Correction', constraints='No deploy', authority='Ask'))
        self.assertEqual(store.brief_history('task-1')[0]['brief'], 'Original')
        self.assertEqual(store.brief_history('task-1')[1]['brief'], 'Correction')


if __name__ == '__main__':
    unittest.main()
