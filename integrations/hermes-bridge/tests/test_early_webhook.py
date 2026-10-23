"""Creation/webhook ordering, independent of pending plugin-entrypoint tests."""
import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import unittest

from bridge import Inbox, Launcher, Store


class EarlyWebhookTests(unittest.TestCase):
    def test_event_during_create_is_retryable_until_task_binding_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'private' / 'bridge.sqlite3')
            self.addCleanup(store.close)
            secret = b's' * 32
            inbox = Inbox(store, secret, clock=lambda: 1000)
            body = json.dumps({'event_type': 'task.finished', 'task': {'id': 'fast-task'}}).encode()
            headers = {'X-Webhook-Timestamp': '1000', 'X-Request-ID': 'early-event',
                       'X-Webhook-Signature-V2': hmac.new(secret, b'1000.' + body, hashlib.sha256).hexdigest()}
            observed = []

            def create(server, tool, arguments):
                observed.append(inbox.accept(headers, body))
                return {'ok': True, 'structuredContent': {'taskId': 'fast-task'}}

            Launcher(store, create).launch(
                dict(projectId='project', prompt='No-tool smoke', provider='codex', model='default',
                     mode='ask', constraints='No writes', authority='Ask first'),
                dict(profile='default', session_key='discord:dm:origin', conversation_id='session-A'))
            self.assertEqual(observed, [(503, 'binding_pending')])
            self.assertEqual(store.events(), [])
            self.assertEqual(inbox.accept(headers, body), (202, 'stored'))
            self.assertEqual(inbox.accept(headers, body), (200, 'duplicate'))


if __name__ == '__main__':
    unittest.main()
