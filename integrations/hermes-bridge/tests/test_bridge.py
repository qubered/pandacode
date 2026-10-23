import hashlib
import hmac
import json
import os
from pathlib import Path
import tempfile
import unittest

import bridge


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'private' / 'bridge.sqlite3'
        self.secret = b'test-only-secret-not-a-live-credential'

    def open_store(self):
        self.assertTrue(hasattr(bridge, 'Store'), 'durable Store is not implemented')
        self.store = bridge.Store(self.path)
        self.addCleanup(self.store.close)
        return self.store

    def test_mapping_survives_reopen_with_authority_and_identity(self):
        self.open_store()
        mapping = dict(task_id='task-1', profile='default', session_key='discord:dm:test',
                       conversation_id='conversation-A', brief='Build test',
                       constraints='No deploy', authority='Ask before shell')
        self.store.bind(**mapping)
        other = bridge.Store(self.path)
        self.addCleanup(other.close)
        self.assertEqual(other.mapping('task-1'), mapping)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.path.parent).st_mode & 0o777, 0o700)

    def request(self, payload=None, request_id='req-1', timestamp='1000'):
        body = json.dumps(payload or {'event_type': 'task.finished', 'task': {'id': 'task-1'}}).encode()
        headers = {'X-Webhook-Timestamp': timestamp, 'X-Request-ID': request_id,
                   'X-Webhook-Signature-V2': hmac.new(self.secret, timestamp.encode() + b'.' + body,
                                                    hashlib.sha256).hexdigest()}
        return headers, body

    def bind_task(self):
        self.open_store()
        self.store.bind(task_id='task-1', profile='default', session_key='route-A',
                        conversation_id='conversation-A', brief='Build test',
                        constraints='No deploy', authority='Ask before shell')

    def test_signed_event_is_durable_but_not_delivered(self):
        self.bind_task()
        self.assertTrue(hasattr(bridge, 'Inbox'), 'signed Inbox is not implemented')
        inbox = bridge.Inbox(self.store, self.secret, clock=lambda: 1000)
        headers, body = self.request()
        self.assertEqual(inbox.accept(headers, body), (202, 'stored'))
        other = bridge.Store(self.path)
        self.addCleanup(other.close)
        self.assertEqual(other.events()[0]['state'], 'blocked_host_identity')
        self.assertEqual(other.events()[0]['request_id'], 'req-1')

    def test_authentication_rejects_stale_future_missing_or_forged_v2(self):
        self.bind_task()
        inbox = bridge.Inbox(self.store, self.secret, clock=lambda: 1000)
        for timestamp in ('699', '1301', 'not-a-time', ''):
            with self.subTest(timestamp=timestamp):
                headers, body = self.request(timestamp=timestamp)
                self.assertEqual(inbox.accept(headers, body)[0], 401)
        headers, body = self.request()
        for replacement in ('', '0' * 64, 'é'):
            with self.subTest(signature=replacement):
                headers['X-Webhook-Signature-V2'] = replacement
                self.assertEqual(inbox.accept(headers, body)[0], 401)
        self.assertEqual(self.store.events(), [])

    def test_unknown_task_fails_closed_without_storing(self):
        self.bind_task()
        inbox = bridge.Inbox(self.store, self.secret, clock=lambda: 1000)
        headers, body = self.request({'event_type': 'task.finished', 'task': {'id': 'unknown'},
                                      'session_key': 'attacker-route'})
        self.assertEqual(inbox.accept(headers, body), (404, 'unknown_task'))
        self.assertEqual(self.store.events(), [])

    def test_duplicate_id_and_changed_id_replays_are_durable(self):
        self.bind_task()
        inbox = bridge.Inbox(self.store, self.secret, clock=lambda: 1000)
        headers, body = self.request()
        self.assertEqual(inbox.accept(headers, body), (202, 'stored'))
        try:
            duplicate = inbox.accept(headers, body)
        except Exception as exc:
            self.fail('duplicate was not handled: ' + type(exc).__name__)
        self.assertEqual(duplicate, (200, 'duplicate'))
        headers['X-Request-ID'] = 'changed-unsigned-id'
        self.assertEqual(inbox.accept(headers, body), (200, 'duplicate'))
        changed_headers, changed_body = self.request({'event_type': 'task.needs_response', 'task': {'id': 'task-1'}})
        self.assertEqual(inbox.accept(changed_headers, changed_body), (409, 'id_conflict'))
        other = bridge.Store(self.path)
        self.addCleanup(other.close)
        self.assertEqual(bridge.Inbox(other, self.secret, clock=lambda: 1000).accept(headers, body), (200, 'duplicate'))
        self.assertEqual(len(other.events()), 1)

    def test_ingress_requires_bounded_valid_object_and_request_id(self):
        self.bind_task()
        inbox = bridge.Inbox(self.store, self.secret, clock=lambda: 1000)
        for payload in ([], {'task': {}}, {'event_type': 'arbitrary', 'task': {'id': 'task-1'}},
                        {'event_type': 'task.finished', 'task': {'id': ['bad']}}):
            body = json.dumps(payload).encode()
            headers, _ = self.request()
            headers['X-Webhook-Signature-V2'] = hmac.new(self.secret, b'1000.' + body, hashlib.sha256).hexdigest()
            with self.subTest(payload=payload):
                try:
                    status = inbox.accept(headers, body)[0]
                except Exception as exc:
                    self.fail('invalid payload raised: ' + type(exc).__name__)
                self.assertEqual(status, 400)
        headers, body = self.request()
        headers.pop('X-Request-ID')
        self.assertEqual(inbox.accept(headers, body)[0], 400)
        self.assertEqual(inbox.accept({}, b'x' * 65537)[0], 413)
        self.assertEqual(self.store.events(), [])

    def test_binding_cannot_be_retargeted_or_extended(self):
        self.bind_task()
        original = self.store.mapping('task-1')
        with self.assertRaises(ValueError):
            self.store.bind(**{**original, 'session_key': 'another-route'})
        with self.assertRaises(ValueError):
            self.store.bind(**{**original, 'task_id': 'task-2', 'origin': 'arbitrary'})
        with self.assertRaises(ValueError):
            self.store.bind(**{**original, 'task_id': 'task-3', 'authority': ''})
        self.assertEqual(self.store.mapping('task-1'), original)

    def test_launch_binds_only_mcp_returned_id_to_trusted_origin(self):
        self.open_store()
        self.assertTrue(hasattr(bridge, 'Launcher'), 'launch wrapper is not implemented')
        calls = []
        def mcp(server, tool, args):
            calls.append((server, tool, args))
            return {'ok': True, 'structuredContent': {'taskId': 'verified-task'}}
        launcher = bridge.Launcher(self.store, mcp)
        identity = dict(profile='default', session_key='trusted-route', conversation_id='trusted-conversation')
        args = dict(projectId='demo-project', prompt='Build test', provider='codex', model='default',
                    mode='ask', constraints='No deploy', authority='Ask for commands')
        self.assertEqual(launcher.launch(args, identity), 'verified-task')
        self.assertEqual(self.store.mapping('verified-task'), dict(task_id='verified-task', **identity,
                          brief='Build test', constraints='No deploy', authority='Ask for commands'))
        self.assertEqual(calls[0][:2], ('pandacode', 'pandacode_create_task'))
        self.assertEqual(calls[0][2]['mode'], 'ask')
        self.assertIn('No deploy', calls[0][2]['prompt'])
        self.assertIn('Ask for commands', calls[0][2]['prompt'])
        self.assertEqual(self.store.launches()[0]['state'], 'bound')

    def test_launch_rejects_target_selection_and_unverified_results(self):
        self.open_store()
        calls = []
        identity = dict(profile='default', session_key='trusted-route', conversation_id='trusted-conversation')
        args = dict(projectId='demo-project', prompt='Build test', provider='codex', model='default',
                    mode='ask', constraints='No deploy', authority='Ask for commands')
        def mcp(*call):
            calls.append(call)
            return {'ok': True, 'structuredContent': {'taskId': 'verified-task'}}
        for bad in ({**args, 'session_key': 'attacker'}, {**args, 'taskId': 'existing'},
                    {**args, 'authority': ''}, {**args, 'mode': 'full'}):
            with self.subTest(bad=list(bad)):
                with self.assertRaises(ValueError):
                    bridge.Launcher(self.store, mcp).launch(bad, identity)
        self.assertEqual(calls, [])
        for result in ({'ok': False, 'structuredContent': {'taskId': 'forged'}},
                       {'ok': True, 'truncated': True, 'structuredContent': {'taskId': 'forged'}},
                       {'ok': True, 'structuredContent': {'taskId': ''}}):
            with self.subTest(result=result):
                with self.assertRaises(ValueError):
                    bridge.Launcher(self.store, lambda *a: result).launch(args, identity)
        self.assertIsNone(self.store.mapping('forged'))
        self.assertTrue(all(row['state'] == 'uncertain' for row in self.store.launches()))

    def test_loopback_listener_accepts_signed_http_and_stops(self):
        import http.client
        self.bind_task()
        self.assertTrue(hasattr(bridge, 'Listener'), 'HTTP listener is not implemented')
        listener = bridge.Listener(bridge.Inbox(self.store, self.secret, clock=lambda: 1000), port=0)
        self.addCleanup(listener.close)
        headers, body = self.request()
        connection = http.client.HTTPConnection(*listener.address, timeout=3)
        self.addCleanup(connection.close)
        connection.request('POST', '/pandacode', body=body, headers=headers)
        response = connection.getresponse()
        self.assertEqual(response.status, 202)
        self.assertEqual(json.loads(response.read()), {'status': 'stored'})
        connection.request('POST', '/other', body=body, headers=headers)
        response = connection.getresponse()
        self.assertEqual(response.status, 404)
        response.read()
        self.assertEqual(len(self.store.events()), 1)
        listener.close()
        self.assertFalse(listener.thread.is_alive())


if __name__ == '__main__':
    unittest.main()
