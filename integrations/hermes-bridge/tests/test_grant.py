"""Task-specific grants for Full-mode tasks above maxMode, against a fake PandaCode core (FAKE ONLY).

The contract (supervision {level, epoch, grant}; grant == task mode, given only by the human)
is taken from the assignment; the backend change had not landed in the working tree when these
were written, so the denial wording and envelope here are assumptions, not observed behaviour.
"""
import json

import delegation
import test_delegation as base
from test_delegation import FakeCore


class GrantCore(FakeCore):
    """FakeCore plus mode, maxMode 'auto' and a human-only grant."""
    def task(self, task_id, supervisor=None, level=None, status='running', mode='full'):
        super().task(task_id, supervisor, level, status)
        self.tasks[task_id].update(mode=mode, grant=None)

    def grant(self, task_id, level='manage'):
        """The user hands the task over in PandaCode with an explicit grant (epoch moves)."""
        self.tasks[task_id]['grant'] = self.tasks[task_id]['mode']
        self._change(task_id, 'hermes', level)
        return self.emit(task_id, 'task.handed_over', data={'lastReply': ''})

    def withdraw(self, task_id, bump=True):
        task = self.tasks[task_id]
        task['grant'] = None
        if bump:
            task['epoch'] += 1

    def needs_grant(self, task):
        return task['mode'] == 'full' and task['grant'] != task['mode']

    def call_mcp(self, server, tool, args):
        task = self.tasks.get(args.get('taskId'))
        if tool == 'pandacode_attach_task' and task and args['level'] == 'manage' and self.needs_grant(task):
            self.calls.append((tool, dict(args)))
            return {'ok': False, 'result': 'This task runs in full mode, above your highest allowed mode. '
                                           'The user must grant it to you for this task.'}
        if (tool in ('pandacode_send_message', 'pandacode_respond', 'pandacode_stop_task')
                and task and self.needs_grant(task)):
            self.calls.append((tool, dict(args)))
            return {'ok': False}
        result = super().call_mcp(server, tool, args)
        if tool == 'pandacode_attach_task' and result.get('ok'):
            result['structuredContent']['grant'] = task['grant']
        return result

    def event(self, task_id, kind, data=None, snapshot=None, request_id=None):
        headers, body = super().event(task_id, kind, data, snapshot, request_id)
        payload = json.loads(body)
        payload['supervision']['grant'] = (snapshot or self.tasks[task_id]).get('grant')
        return self.sign(payload, headers['X-Request-ID'])

    @staticmethod
    def sign(payload, request_id):
        import hashlib
        import hmac
        from test_delegation import SECRET
        body = json.dumps(payload).encode()
        return ({'X-Webhook-Timestamp': '1000', 'X-Request-ID': request_id,
                 'X-Webhook-Signature-V2': hmac.new(SECRET, b'1000.' + body, hashlib.sha256).hexdigest()}, body)


class GrantTests(base.DelegationTests):
    # Reuse the fixture only; the inherited workflow tests run in test_delegation.py.
    def setUp(self):
        super().setUp()
        self.core = GrantCore(self.core.inbox)

    def row(self, task_id):
        with self.store.lock:
            return delegation.get(self.store.db, task_id)

    def test_no_grant_denial_is_surfaced_quietly_once(self):
        self.core.task('full-task')
        first = self.attach('full-task', 'manage')
        self.assertEqual(first['error'], 'needs_user_grant')
        self.assertEqual(first['notify']['policy'], 'quiet')
        self.assertIn('hand this over explicitly', first['message'])
        again = self.attach('full-task', 'manage')
        self.assertEqual((again['error'], again['notify']['policy']), ('needs_user_grant', 'silent'))
        # Never requests or assumes a grant: only the two attach calls went out, nothing was bound.
        self.assertEqual([c[0] for c in self.core.calls], ['pandacode_attach_task'] * 2)
        self.assertIsNone(self.store.mapping('full-task'))
        self.assertIsNone(self.row('full-task'))
        self.assertEqual([l['state'] for l in self.store.launches()], ['refused', 'refused'])
        # Watch needs no grant.
        self.assertNotIn('grant', {k: v for k, v in self.attach('full-task', 'watch').items() if v})

    def test_grant_is_recorded_and_writes_carry_the_epoch(self):
        self.core.task('full-task')
        self.core.tasks['full-task']['grant'] = 'full'  # granted by the user before Gerry attaches
        result = self.attach('full-task', 'manage')
        self.assertEqual(result, {'taskId': 'full-task', 'level': 'manage', 'epoch': 1, 'grant': 'full'})
        self.assertEqual((self.row('full-task')['grant'], self.row('full-task')['requires_grant']), ('full', True))
        self.assertIn('Grant: full', delegation.brief(self.store, 'full-task'))
        self.assertTrue(self.act('full-task', 'send_message', text='continue')['ok'])
        self.assertEqual(self.writes()[-1][1]['epoch'], 1)

    def test_grant_loss_revokes_even_at_the_same_epoch(self):
        self.core.task('full-task')
        self.core.tasks['full-task']['grant'] = 'full'
        self.attach('full-task', 'manage')
        self.core.withdraw('full-task', bump=False)
        self.assertEqual(self.core.emit('full-task', 'task.needs_response', {'kind': 'approval', 'requestId': 'r'}),
                         (200, 'dropped_stale'))
        self.assertEqual((self.row('full-task')['state'], self.row('full-task')['reason']), ('revoked', 'grant_lost'))
        with self.assertRaises(delegation.DelegationInactive):
            self.act('full-task', 'respond', requestId='r', approve=True)
        self.dispatcher.drain()
        self.assertEqual(self.host.messages, [])
        self.assertEqual(self.writes(), [])

    def test_grant_loss_with_epoch_change_revokes_and_waiting_events_are_not_delivered(self):
        self.core.task('full-task')
        self.core.grant('full-task')
        self.dispatcher.drain()  # no binding yet: the hand-over of an unbound task is a 404
        self.core.tasks['full-task']['grant'] = 'full'
        self.attach('full-task', 'manage')
        self.core.emit('full-task', 'task.finished', {'outcome': 'done'})  # waiting for delivery
        self.core.withdraw('full-task')
        self.core.emit('full-task', 'task.finished', {'outcome': 'done'})
        self.dispatcher.drain()
        self.assertEqual(self.host.messages, [])
        self.assertEqual(self.row('full-task')['state'], 'revoked')

    def test_stale_subagent_is_refused_after_grant_loss_and_regrant(self):
        self.core.task('full-task')
        self.core.tasks['full-task']['grant'] = 'full'
        epoch = self.attach('full-task', 'manage')['epoch']
        subagent_brief = delegation.brief(self.store, 'full-task')
        self.assertIn('Epoch: %d' % epoch, subagent_brief)
        self.core.withdraw('full-task')
        self.core.emit('full-task', 'task.human_message', {'text': 'mine for now'})
        self.assertEqual(self.core.grant('full-task'), (202, 'stored'))  # the user grants it again
        current = self.row('full-task')
        self.assertEqual((current['state'], current['grant']), ('active', 'full'))
        self.assertGreater(current['epoch'], epoch)
        # The background subagent still holds the old epoch: local check and PandaCode both refuse.
        with self.assertRaises(delegation.DelegationInactive) as refusal:
            delegation.assert_active(self.store, 'full-task', epoch, write=True)
        self.assertEqual(str(refusal.exception), 'stale_epoch')
        stale = self.core.call_mcp('pandacode', 'pandacode_send_message',
                                   {'taskId': 'full-task', 'text': 'old plan', 'epoch': epoch})
        self.assertFalse(stale['ok'])

    def test_local_write_refused_when_a_required_grant_is_missing(self):
        self.core.task('full-task')
        self.core.tasks['full-task']['grant'] = 'full'
        self.attach('full-task', 'manage')
        with self.store.lock, self.store.db:
            row = delegation.get(self.store.db, 'full-task')
            row['grant'] = None
            delegation.put(self.store.db, 'full-task', row)
        calls = len(self.core.calls)
        with self.assertRaises(delegation.DelegationInactive) as refusal:
            self.act('full-task', 'stop_task')
        self.assertEqual(str(refusal.exception), 'grant_missing')
        self.assertEqual(len(self.core.calls), calls)

    def test_core_without_grant_field_keeps_previous_behaviour(self):
        self.core.task('auto-task', mode='auto')
        self.assertEqual(self.attach('auto-task', 'manage')['grant'], None)
        self.assertFalse(self.row('auto-task')['requires_grant'])
        self.assertTrue(self.act('auto-task', 'send_message', text='go')['ok'])

    def test_child_capabilities_end_on_grant_loss_epoch_change_or_takeover(self):
        """A subagent's brief and its assert-active check stop working for every cause of revocation."""
        def withdraw_grant(task_id):
            self.core.withdraw(task_id, bump=False)
            self.core.emit(task_id, 'task.human_message', {'text': 'grant withdrawn'})

        def change_epoch(task_id):
            self.core._change(task_id, 'hermes', 'watch')  # level change whose hand-over was lost
            self.core.emit(task_id, 'task.finished', {'outcome': 'done'})

        causes = {'grant_lost': withdraw_grant, 'epoch_mismatch': change_epoch,
                  'task.taken_over': self.core.take_over}
        for reason, cause in causes.items():
            with self.subTest(cause=reason):
                task_id = 'child-' + reason
                self.core.task(task_id)
                self.core.tasks[task_id]['grant'] = 'full'
                epoch = self.attach(task_id, 'manage')['epoch']
                self.assertIn('Epoch: %d' % epoch, delegation.brief(self.store, task_id))
                delegation.assert_active(self.store, task_id, epoch, write=True)
                cause(task_id)
                self.assertEqual(self.row(task_id)['reason'], reason)
                for check in (lambda: delegation.brief(self.store, task_id),
                              lambda: delegation.assert_active(self.store, task_id, epoch),
                              lambda: delegation.assert_active(self.store, task_id, epoch, write=True),
                              lambda: self.act(task_id, 'send_message', text='from a subagent')):
                    with self.assertRaises(delegation.DelegationInactive):
                        check()
        self.assertEqual(self.writes(), [], 'no child write reached PandaCode')


# Only the grant tests run here; DelegationTests' own tests are collected from test_delegation.py.
for name in [n for n in dir(base.DelegationTests) if n.startswith('test_')]:
    setattr(GrantTests, name, None)
