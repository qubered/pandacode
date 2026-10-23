"""Real spawned-process integration; only service imports, synthetic no-send hosts."""
import hashlib
import hmac
import http.client
import json
import multiprocessing
import os
from pathlib import Path
import queue
import socket
import tempfile
import time
import unittest

import bridge


def host_process(directory, home, profile, identity, port, control, reports, deliveries, stall):
    from broker import BridgeRuntime
    def resolve(sid):
        anchor = json.loads(identity['conversation_id'].split(':', 1)[1])['anchor']
        return identity if sid == anchor else None
    def inject(content, *, session_key, expected_session_id):
        deliveries.put((os.getpid(), expected_session_id, json.loads(content)))
        if stall:
            import threading
            threading.Event().wait(60)
        return True
    runtime = BridgeRuntime(Path(directory), profile, home, resolve, inject, b'z' * 32,
                            port=port, interval=0.02)
    try:
        while True:
            command = control.get(timeout=10)
            if command == 'stop':
                break
            if command == 'status':
                reports.put(runtime.ingress.listener.address if runtime.ingress.listener else None)
    finally:
        runtime.close()


def launch_process(path, identity, ready, release, reports):
    store = bridge.Store(Path(path))
    try:
        ready.wait(5)
        def mcp(server, tool, args):
            reports.put('external_create_called')
            release.wait(5)
            return dict(ok=True, structuredContent=dict(taskId='only-task'))
        try:
            task = bridge.Launcher(store, mcp).launch(dict(projectId='p', prompt='Work', provider='synthetic',
                model='synthetic', mode='ask', constraints='No deploy', authority='Ask'), identity)
            reports.put(task)
        except ValueError:
            reports.put('uncertain_origin_refused')
    finally:
        store.close()


class ProcessBrokerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.directory = self.home / 'plugin-state' / 'pandacode-bridge'
        self.store = bridge.Store(self.directory / 'bridge.sqlite3')
        self.addCleanup(self.store.close)
        self.context = multiprocessing.get_context('spawn')
        self.deliveries = self.context.Queue()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]

    def identity(self, host='gateway'):
        return dict(profile='work', session_key=host + ':key', conversation_id='hermes-origin-v1:' +
            json.dumps(dict(host=host, key=host + ':key', anchor=host + ':session',
                generation='route-one', home=str(self.home)), sort_keys=True, separators=(',', ':')))

    def start(self, identity, stall=False):
        control, reports = self.context.Queue(), self.context.Queue()
        process = self.context.Process(target=host_process, args=(str(self.directory), str(self.home),
            'work', identity, self.port, control, reports, self.deliveries, stall))
        process.start()
        def cleanup():
            if process.is_alive():
                control.put('stop')
                process.join(2)
            if process.is_alive():
                process.terminate()
                process.join(3)
            control.close()
            reports.close()
        self.addCleanup(cleanup)
        return process, control, reports

    def status(self, host):
        host[1].put('status')
        return host[2].get(timeout=5)

    def bind(self, task, identity):
        self.store.bind(task_id=task, **identity, brief='Exact origin work', constraints='No deploy', authority='Ask')

    def send(self, task, request):
        body = json.dumps(dict(event_type='task.finished', task=dict(id=task))).encode()
        timestamp = str(int(time.time()))
        headers = {'X-Webhook-Timestamp': timestamp, 'X-Request-ID': request,
            'X-Webhook-Signature-V2': hmac.new(b'z' * 32, timestamp.encode() + b'.' + body, hashlib.sha256).hexdigest()}
        client = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        try:
            client.request('POST', '/pandacode', body, headers)
            response = client.getresponse()
            response.read()
            return response.status
        finally:
            client.close()

    def test_three_processes_dispatch_only_their_exact_origins_and_one_listener(self):
        cli, gateway, desktop = self.identity('cli'), self.identity('gateway'), self.identity('local')
        self.bind('cli-task', cli)
        self.bind('gateway-task', gateway)
        self.bind('desktop-task', desktop)
        first = self.start(cli)
        self.assertIsNotNone(self.status(first))
        second = self.start(gateway)
        self.assertIsNone(self.status(second))
        third = self.start(desktop)
        self.assertIsNone(self.status(third))
        self.assertEqual(self.send('gateway-task', 'g'), 202)
        self.assertEqual(self.send('cli-task', 'c'), 202)
        self.assertEqual(self.send('desktop-task', 'd'), 202)
        received = [self.deliveries.get(timeout=5) for _ in range(3)]
        self.assertEqual({(pid, token) for pid, token, _ in received},
            {(first[0].pid, cli['conversation_id']), (second[0].pid, gateway['conversation_id']),
             (third[0].pid, desktop['conversation_id'])})
        self.assertEqual(self.send('gateway-task', 'g'), 200)
        with self.assertRaises(queue.Empty):
            self.deliveries.get(timeout=0.2)

    def test_sigterm_ingress_handoff_preserves_follower_delivery(self):
        first = self.start(self.identity('cli'))
        self.assertIsNotNone(self.status(first))
        gateway = self.identity('gateway')
        self.bind('task', gateway)
        second = self.start(gateway)
        self.assertIsNone(self.status(second))
        first[0].terminate()
        first[0].join(3)
        deadline = time.monotonic() + 5
        address = None
        while address is None and time.monotonic() < deadline:
            time.sleep(0.05)
            address = self.status(second)
        self.assertIsNotNone(address)
        self.assertEqual(self.send('task', 'after-handoff'), 202)
        received = self.deliveries.get(timeout=5)
        self.assertEqual(received[:2], (second[0].pid, gateway['conversation_id']))

    def test_death_inside_host_enqueue_is_uncertain_and_not_blindly_replayed(self):
        identity = self.identity()
        self.bind('task', identity)
        first = self.start(identity, stall=True)
        self.assertIsNotNone(self.status(first))
        self.assertEqual(self.send('task', 'crash-window'), 202)
        self.deliveries.get(timeout=5)  # host entered; acceptance outcome not yet known
        first[0].terminate()
        first[0].join(3)
        second = self.start(identity)
        self.assertIsNotNone(self.status(second))
        deadline = time.monotonic() + 3
        while self.store.events()[0]['state'] != 'uncertain' and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.store.events()[0]['state'], 'uncertain')
        self.assertEqual(self.send('task', 'crash-window'), 200)
        with self.assertRaises(queue.Empty):
            self.deliveries.get(timeout=0.2)
    def test_shared_uncertain_launch_ledger_prevents_cross_process_duplicate_create(self):
        ready, release, reports = self.context.Event(), self.context.Event(), self.context.Queue()
        children = [self.context.Process(target=launch_process,
            args=(str(self.directory / 'bridge.sqlite3'), self.identity(), ready, release, reports)) for _ in range(2)]
        for child in children:
            child.start()
        try:
            ready.set()
            pending = [reports.get(timeout=5), reports.get(timeout=5)]
            self.assertCountEqual(pending, ['external_create_called', 'uncertain_origin_refused'])
            release.set()
            self.assertEqual(reports.get(timeout=5), 'only-task')
            for child in children:
                child.join(5)
                self.assertEqual(child.exitcode, 0)
            self.assertEqual(len(self.store.launches()), 1)
        finally:
            release.set()
            for child in children:
                if child.is_alive():
                    child.terminate()
                child.join(3)
            reports.close()


if __name__ == '__main__':
    unittest.main()
