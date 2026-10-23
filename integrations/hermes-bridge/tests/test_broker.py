"""Independent broker service tests: never imports the plugin entrypoint."""
import json
from pathlib import Path
import tempfile
import unittest
import bridge


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / 'private'
        self.store = bridge.Store(self.directory / 'bridge.sqlite3')
        self.addCleanup(self.store.close)

    def identity(self, kind='cli', generation='one'):
        token = 'hermes-origin-v1:' + json.dumps(dict(host=kind, home=str(self.directory.parent),
            key=kind + ':route', anchor=kind + '-session', generation=generation), sort_keys=True,
            separators=(',', ':'))
        return dict(profile='work', session_key=kind + ':route', conversation_id=token)

    def broker(self, identities):
        from broker import OwnerBroker
        result = OwnerBroker(self.store, self.directory, 'work',
                             lambda sid: next((i for i in identities if json.loads(i['conversation_id'].split(':', 1)[1])['anchor'] == sid), None))
        self.addCleanup(result.close)
        return result

    def test_separate_hosts_register_exact_origins_and_only_owner_dispatches(self):
        cli, gateway = self.identity(), self.identity('gateway')
        first, second = self.broker([cli]), self.broker([gateway])
        self.assertTrue(first.register_origin(cli))
        self.assertFalse(first.register_origin(gateway))
        self.assertTrue(second.register_origin(gateway))
        for index, identity in enumerate((cli, gateway)):
            self.store.bind(task_id=str(index), **identity, brief='Do it', constraints='No deployment', authority='Ask')
            with self.store.db:
                self.store.db.execute('INSERT INTO inbox VALUES (?, ?, ?, ?, ?)',
                    (str(index), str(index), str(index), b'{}', 'blocked_host_identity'))
        calls = []
        def inject(content, *, session_key, expected_session_id):
            calls.append(expected_session_id)
            return True
        second.dispatcher(inject).drain()
        self.assertEqual(calls, [gateway['conversation_id']])
        first.dispatcher(inject).drain()
        first.dispatcher(inject).drain()
        self.assertEqual(calls, [gateway['conversation_id'], cli['conversation_id']])
        self.assertEqual([x['state'] for x in self.store.events()], ['accepted', 'accepted'])
    def test_one_ingress_and_follower_takes_over_without_bind_conflict(self):
        from broker import IngressOwner
        first, second = self.broker([]), self.broker([])
        a = IngressOwner(first, b'x' * 32, port=0)
        b = IngressOwner(second, b'x' * 32, port=0)
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        self.assertTrue(a.tick())
        self.assertFalse(b.tick())
        self.assertIsNone(b.listener)
        address = a.listener.address
        import http.client
        client = http.client.HTTPConnection(*address, timeout=3)
        client.request('POST', '/pandacode', b'{}', {'Content-Length': '2'})
        self.assertEqual(client.getresponse().status, 401)
        client.close()
        a.close()
        self.assertTrue(b.tick())
        self.assertIsNotNone(b.listener)

    def test_crashed_dispatch_claim_is_uncertain_not_adopted(self):
        identity = self.identity()
        first = self.broker([identity])
        first.register_origin(identity)
        self.store.bind(task_id='one', **identity, brief='Work', constraints='No deploy', authority='Ask')
        with self.store.db:
            self.store.db.execute('INSERT INTO inbox VALUES (?, ?, ?, ?, ?)',
                                 ('req', 'digest', 'one', b'{}', 'blocked_host_identity'))
        first.claim(self.store.events()[0], self.store.mapping('one'))
        # Simulate abrupt death: kernel lock released, DB owner/claim left behind.
        first.file.close()
        first.legacy_file.close()
        first.closed = True
        second = self.broker([identity])
        second.refresh()
        self.assertEqual(self.store.events()[0]['state'], 'uncertain')
        self.assertFalse(second.claim(self.store.events()[0], self.store.mapping('one')))

    def test_live_owner_is_never_reaped_or_replayed_by_other_host(self):
        identity = self.identity()
        first, second = self.broker([identity]), self.broker([identity])
        first.register_origin(identity)
        second.register_origin(identity)
        self.store.bind(task_id='one', **identity, brief='Work', constraints='No deploy', authority='Ask')
        with self.store.db:
            self.store.db.execute('INSERT INTO inbox VALUES (?, ?, ?, ?, ?)',
                                 ('req', 'digest', 'one', b'{}', 'blocked_host_identity'))
        second.reap()
        self.assertFalse(first.claim(self.store.events()[0], self.store.mapping('one')))
        second.close()
        self.assertTrue(first.claim(self.store.events()[0], self.store.mapping('one')))
        third = self.broker([])
        third.reap()
        self.assertEqual(self.store.events()[0]['state'], 'dispatching')
    def test_runtime_integrates_capture_launch_signed_http_delivery_and_unload(self):
        from broker import BridgeRuntime
        identity = self.identity()
        calls = []
        def inject(content, *, session_key, expected_session_id):
            calls.append(json.loads(content))
            return True
        def mcp(server, tool, args):
            return dict(ok=True, structuredContent=dict(taskId='created'))
        runtime = BridgeRuntime(self.directory, 'work', self.directory.parent,
            lambda sid: identity if sid == 'cli-session' else None, inject,
            b'k' * 32, port=0, interval=0.01)
        self.addCleanup(runtime.close)
        runtime.observe(session_id='cli-session')
        captured = runtime.identity('cli-session')
        task = bridge.Launcher(runtime.store, mcp).launch(dict(projectId='p', prompt='Work',
            provider='synthetic', model='synthetic', mode='ask', constraints='Never deploy', authority='Ask'), captured)
        self.assertEqual(task, 'created')
        import hashlib, hmac, http.client, time
        body = json.dumps(dict(event_type='task.finished', task=dict(id=task))).encode()
        timestamp = str(int(time.time()))
        headers = {'X-Webhook-Timestamp': timestamp, 'X-Request-ID': 'runtime-req',
            'X-Webhook-Signature-V2': hmac.new(b'k' * 32, timestamp.encode() + b'.' + body, hashlib.sha256).hexdigest()}
        conn = http.client.HTTPConnection(*runtime.ingress.listener.address, timeout=3)
        conn.request('POST', '/pandacode', body, headers)
        self.assertEqual(conn.getresponse().status, 202)
        conn.close()
        deadline = time.monotonic() + 3
        while not calls and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['constraints'], 'Never deploy')
        runtime.close()
        self.assertFalse(runtime.worker.is_alive())
        self.assertIsNone(runtime.ingress.listener)
        self.assertTrue(runtime.owner.closed)
    def test_legacy_exclusive_worker_blocks_new_owners_instead_of_mixing_protocols(self):
        import fcntl
        from broker import OwnerBroker
        legacy = open(self.directory / 'owner.lock', 'a')
        self.addCleanup(legacy.close)
        fcntl.flock(legacy, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with self.assertRaises(BlockingIOError):
            OwnerBroker(self.store, self.directory, 'work', lambda sid: None)

    def test_ingress_configuration_mismatch_fails_closed(self):
        from broker import IngressOwner
        a, b = self.broker([]), self.broker([])
        ingress = IngressOwner(a, b'x' * 32, port=0)
        self.addCleanup(ingress.close)
        with self.assertRaises(ValueError):
            IngressOwner(b, b'y' * 32, port=0)
        self.assertTrue(ingress.tick())

    def test_bind_conflict_never_prevents_origin_registration_or_steals_socket(self):
        import socket
        from broker import IngressOwner
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0))
            occupied.listen()
            owner = self.broker([self.identity()])
            ingress = IngressOwner(owner, b'x' * 32, port=occupied.getsockname()[1])
            self.addCleanup(ingress.close)
            self.assertFalse(ingress.tick())
            self.assertIsNone(ingress.listener)
            self.assertTrue(owner.register_origin(self.identity()))
        self.assertTrue(ingress.tick())

    def test_new_broker_quarantines_pre_broker_abandoned_claims(self):
        owner = self.broker([])
        with self.store.db:
            self.store.db.execute('INSERT INTO inbox VALUES (?, ?, ?, ?, ?)',
                                 ('legacy', 'digest', 'task', b'{}', 'dispatching'))
        owner.dispatcher(lambda content: True).recover()
        self.assertEqual(self.store.events()[0]['state'], 'uncertain')
    def test_cross_process_style_brief_edits_preserve_disjoint_updates(self):
        import threading
        identity = self.identity()
        self.store.bind(task_id='task', **identity, brief='Original', constraints='No deploy', authority='Ask')
        other = bridge.Store(self.directory / 'bridge.sqlite3')
        self.addCleanup(other.close)
        read, release, second_done = threading.Event(), threading.Event(), threading.Event()
        original_mapping = self.store.mapping
        def paused_mapping(task):
            value = original_mapping(task)
            read.set()
            release.wait(3)
            return value
        self.store.mapping = paused_mapping
        failures = []
        def first_edit():
            try:
                self.store.update_brief('task', identity, brief='New brief')
            except Exception as error:
                failures.append(error)
        def second_edit():
            try:
                other.update_brief('task', identity, constraints='Never deploy or send')
            except Exception as error:
                failures.append(error)
            finally:
                second_done.set()
        first = threading.Thread(target=first_edit)
        second = threading.Thread(target=second_edit)
        first.start()
        self.assertTrue(read.wait(3))
        second.start()
        second_done.wait(0.2)
        release.set()
        first.join(4)
        second.join(4)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(failures, [])
        current = other.mapping('task')
        self.assertEqual(current['brief'], 'New brief')
        self.assertEqual(current['constraints'], 'Never deploy or send')
    def test_revoked_host_registration_is_withdrawn_without_retargeting_claims(self):
        identity = self.identity()
        first_origins = [identity]
        first, second = self.broker(first_origins), self.broker([identity])
        first.register_origin(identity)
        second.register_origin(identity)
        self.store.bind(task_id='task', **identity, brief='Work', constraints='No deploy', authority='Ask')
        with self.store.db:
            self.store.db.execute('INSERT INTO inbox VALUES (?, ?, ?, ?, ?)',
                                 ('new', 'digest', 'task', b'{}', 'blocked_host_identity'))
        first_origins.clear()  # actual host resolver no longer grants this exact route
        first.refresh()
        self.assertTrue(second.claim(self.store.events()[0], self.store.mapping('task')))


if __name__ == '__main__':
    unittest.main()
