"""Watch/manage delegations, takeover and quiet notifications, against a fake PandaCode core.

FakeCore mirrors the backend contract being landed in src-tauri/src/orchestrator (attach_task,
supervision {level, epoch}, epoch-checked writes, release_task handover, guarded webhooks).
It is a test double: nothing here proves the live Hermes host or a live PandaCode core.
"""
import hashlib
import hmac
import importlib.util
import itertools
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

import bridge
import delegation

SECRET = b'test-only-secret-not-a-live-credential'
ORIGIN = dict(profile='default', session_key='discord:dm:user', conversation_id='session-A')


class FakeCore:
    """PandaCode's orchestrator as the contract describes it; refuses what the backend refuses."""
    def __init__(self, inbox):
        self.inbox, self.tasks, self.calls = inbox, {}, []
        self.ids = itertools.count(1)

    def task(self, task_id, supervisor=None, level=None, status='running'):
        self.tasks[task_id] = dict(supervisor=supervisor, level=level, epoch=0, status=status)

    def _change(self, task_id, supervisor, level):
        task = self.tasks[task_id]
        before = dict(task)
        task.update(supervisor=supervisor, level=level, epoch=task['epoch'] + 1)
        return before

    def call_mcp(self, server, tool, args):
        self.calls.append((tool, dict(args)))
        name = tool.removeprefix('pandacode_')
        if name == 'create_task':
            task_id = 'task-' + str(next(self.ids))
            self.task(task_id)
            self._change(task_id, 'hermes', 'manage')
            return self.ok({'taskId': task_id, 'level': 'manage', 'epoch': self.tasks[task_id]['epoch']})
        task = self.tasks.get(args.get('taskId'))
        if task is None:
            return {'ok': False}
        if name == 'attach_task':
            if task['supervisor'] not in (None, 'hermes'):
                return {'ok': False}
            if (task['supervisor'], task['level']) != ('hermes', args['level']):
                self._change(args['taskId'], 'hermes', args['level'])
            return self.ok({'taskId': args['taskId'], 'level': task['level'], 'epoch': task['epoch']})
        if task['supervisor'] != 'hermes' or ('epoch' in args and args['epoch'] != task['epoch']):
            return {'ok': False}
        if name == 'release_task':
            before = self._change(args['taskId'], None, None)
            self.emit(args['taskId'], 'task.released', snapshot=before,
                      data={'handover': args['handover']} if 'handover' in args else {})
            return self.ok({'ok': True, 'epoch': task['epoch']})
        if task['level'] != 'manage':
            return {'ok': False}
        return self.ok({'ok': True})

    @staticmethod
    def ok(data):
        return {'ok': True, 'structuredContent': data}

    # ---- user actions in the PandaCode window ----
    def take_over(self, task_id):
        before = self._change(task_id, None, None)
        return self.emit(task_id, 'task.taken_over', snapshot=before)

    def hand_over(self, task_id, level='manage'):
        self._change(task_id, 'hermes', level)
        return self.emit(task_id, 'task.handed_over', data={'lastReply': 'Over to you.'})

    # ---- webhooks ----
    def event(self, task_id, kind, data=None, snapshot=None, request_id=None):
        """Builds a signed delivery (headers, body) without sending it, so tests can delay it."""
        task = snapshot or self.tasks[task_id]
        request_id = request_id or 'evt-' + str(next(self.ids))
        body = json.dumps({'event_type': kind, 'id': request_id, 'task': {'id': task_id},
                           'supervision': {'level': task['level'], 'epoch': task['epoch']},
                           'data': data or {}}).encode()
        headers = {'X-Webhook-Timestamp': '1000', 'X-Request-ID': request_id,
                   'X-Webhook-Signature-V2': hmac.new(SECRET, b'1000.' + body, hashlib.sha256).hexdigest()}
        return headers, body

    def deliver(self, delivery):
        return self.inbox.accept(*delivery)

    def emit(self, task_id, kind, data=None, snapshot=None):
        return self.deliver(self.event(task_id, kind, data, snapshot))


class Host:
    """Exact-identity host injection double; records what would wake the conversation."""
    def __init__(self):
        self.messages = []

    def inject(self, content, *, session_key, expected_session_id):
        self.messages.append(json.loads(content))
        return True

    def notifications(self):
        """Deliveries that permit telling the user something unprompted."""
        return [m for m in self.messages if m.get('notify', {}).get('policy') in (delegation.RESULT, delegation.BLOCKER)]


class DelegationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = bridge.Store(Path(tmp.name) / 'private' / 'bridge.sqlite3')
        self.addCleanup(self.store.close)
        self.core = FakeCore(bridge.Inbox(self.store, SECRET, clock=lambda: 1000))
        self.host = Host()
        self.dispatcher = bridge.Dispatcher(self.store, 'default', self.host.inject)

    def attach(self, task_id, level, **extra):
        return delegation.Attacher(self.store, self.core.call_mcp).attach(
            dict(dict(brief='Finish the login fix'), taskId=task_id, level=level, **extra), ORIGIN)

    def act(self, task_id, action, _epoch='recorded', **arguments):
        if _epoch == 'recorded':
            _epoch = delegation.get(self.store.db, task_id)['epoch']
        return delegation.Actor(self.store, self.core.call_mcp).act(
            dict(taskId=task_id, action=action, epoch=_epoch, arguments=arguments), ORIGIN)

    def launch(self):
        return bridge.Launcher(self.store, self.core.call_mcp).launch(
            dict(projectId='p', prompt='Fix the login bug', provider='claude', model='default',
                 mode='ask', constraints='No deploy', authority='Approve test runs'), ORIGIN)

    def states(self):
        return [e['state'] for e in self.store.events()]

    def writes(self):
        return [c for c in self.core.calls if c[0] in ('pandacode_send_message', 'pandacode_respond',
                                                       'pandacode_stop_task')]

    # ---- workflow 1: the user starts the task, then asks Gerry ----

    def test_watch_is_read_only(self):
        self.core.task('t1')
        self.assertEqual(self.attach('t1', 'watch'), {'taskId': 't1', 'level': 'watch', 'epoch': 1})
        for action, arguments in (('send_message', {'text': 'hi'}), ('respond', {'requestId': 'r', 'approve': True}),
                                  ('stop_task', {})):
            with self.subTest(action=action):
                with self.assertRaises(delegation.DelegationInactive) as refusal:
                    self.act('t1', action, **arguments)
                self.assertEqual(str(refusal.exception), 'watch_only')
        self.assertEqual(self.writes(), [], 'a watch-level write reached PandaCode')
        # A watched approval is the user's: it never wakes the conversation.
        self.core.emit('t1', 'task.needs_response', {'kind': 'approval', 'requestId': 'r1', 'text': 'Run tests?'})
        self.dispatcher.drain()
        self.assertEqual(self.host.messages, [])
        # A failed run is flagged once, as a blocker.
        self.core.emit('t1', 'task.finished', {'outcome': 'failed'})
        self.dispatcher.drain()
        self.assertEqual([m['notify']['policy'] for m in self.host.messages], ['blocker'])
        self.assertEqual(self.host.messages[0]['delegation'], {'taskId': 't1', 'level': 'watch', 'epoch': 1})

    def test_manage_keeps_routine_work_moving_quietly(self):
        self.core.task('t1')
        self.attach('t1', 'manage')
        self.core.emit('t1', 'task.needs_response', {'kind': 'approval', 'requestId': 'r1', 'text': 'Run cargo test?'})
        self.dispatcher.drain()
        [message] = self.host.messages
        self.assertEqual(message['notify']['policy'], 'quiet')
        self.assertEqual(self.host.notifications(), [])
        self.assertEqual(self.act('t1', 'respond', requestId='r1', approve=True), {'ok': True, 'result': {'ok': True}})
        self.act('t1', 'send_message', text='Now run the linter.')
        self.assertEqual([args['epoch'] for _, args in self.writes()], [1, 1], 'epoch must ride every write')
        with self.assertRaises(ValueError):
            self.act('t1', 'send_message', text='x', epoch=99)  # epoch and taskId are bridge-owned

    def test_attach_cannot_retarget_and_changing_level_moves_the_epoch(self):
        self.core.task('t1')
        self.attach('t1', 'watch')
        with self.assertRaises(ValueError):
            delegation.Attacher(self.store, self.core.call_mcp).attach(
                dict(taskId='t1', level='manage'), {**ORIGIN, 'conversation_id': 'session-B'})
        self.assertEqual(self.attach('t1', 'manage')['epoch'], 2)
        self.assertEqual(delegation.assert_active(self.store, 't1', 2, write=True)['level'], 'manage')
        with self.assertRaises(delegation.DelegationInactive):
            delegation.assert_active(self.store, 't1', 1)
        self.core.tasks['t2'] = dict(supervisor='someone-else', level='manage', epoch=4, status='idle')
        with self.assertRaises(ValueError):
            self.attach('t2', 'manage')
        self.assertIsNone(self.store.mapping('t2'))

    # ---- takeover ----

    def test_takeover_while_running_revokes_and_drops_late_events(self):
        task_id = self.launch()
        delayed = self.core.event(task_id, 'task.finished', {'outcome': 'done'})  # queued at epoch 1
        self.assertEqual(self.core.take_over(task_id), (202, 'stored'))
        with self.assertRaises(delegation.DelegationInactive):
            self.act(task_id, 'send_message', text='keep going')
        self.assertEqual(self.writes(), [])
        # The delayed delivery and its retry with the same X-Request-ID are dropped, not delivered.
        self.assertEqual(self.core.deliver(delayed), (200, 'dropped_stale'))
        self.assertEqual(self.core.deliver(delayed), (200, 'duplicate'))
        self.dispatcher.drain()
        self.assertEqual(self.host.messages, [], 'takeover and late events must not wake anyone')
        self.assertEqual(self.states(), ['suppressed', 'dropped_stale'])

    def test_takeover_while_waiting_for_approval(self):
        task_id = self.launch()
        self.core.emit(task_id, 'task.needs_response', {'kind': 'approval', 'requestId': 'r1', 'text': 'Deploy?'})
        # The approval is waiting for delivery when the user takes over: it is never delivered.
        self.core.take_over(task_id)
        self.dispatcher.drain()
        self.assertEqual(self.host.messages, [])
        self.assertEqual(self.states(), ['revoked', 'suppressed'])
        # And if it had already reached the conversation, the answer is refused locally.
        with self.assertRaises(delegation.DelegationInactive):
            self.act(task_id, 'respond', requestId='r1', approve=True)
        self.assertEqual(self.writes(), [])

    def test_backend_refuses_a_write_whose_epoch_went_stale_in_flight(self):
        task_id = self.launch()
        self.core._change(task_id, None, None)  # takeover whose webhook has not arrived yet
        result = self.act(task_id, 'stop_task')
        self.assertFalse(result['ok'])
        self.assertEqual(self.writes()[-1][1]['epoch'], 1)

    def test_subagent_brief_and_assert_active(self):
        task_id = self.launch()
        text = delegation.brief(self.store, task_id)
        for part in ('Task: ' + task_id, 'Level: manage', 'Epoch: 1', 'Brief: Fix the login bug',
                     'Constraints: No deploy', 'Authority: Approve test runs', 'pandacode_bridge_check'):
            self.assertIn(part, text)
        self.assertEqual(delegation.assert_active(self.store, task_id, 1)['level'], 'manage')
        self.core.take_over(task_id)
        with self.assertRaises(delegation.DelegationInactive):
            delegation.assert_active(self.store, task_id, 1)
        with self.assertRaises(delegation.DelegationInactive):
            delegation.brief(self.store, task_id)

    def test_unannounced_supervision_change_fails_closed(self):
        task_id = self.launch()
        self.core._change(task_id, 'hermes', 'watch')  # level change whose hand-over was lost
        self.assertEqual(self.core.emit(task_id, 'task.finished', {'outcome': 'done'}), (200, 'dropped_stale'))
        with self.assertRaises(delegation.DelegationInactive):
            delegation.assert_active(self.store, task_id)

    # ---- quiet ----

    def test_noisy_stream_of_30_events_yields_at_most_one_notification(self):
        task_id = self.launch()
        emit = self.core.emit
        early = self.core.event(task_id, 'task.needs_response', {'kind': 'approval', 'requestId': 'r0'})
        self.assertEqual(self.core.deliver(early), (202, 'stored'))
        sent = 1
        for i in range(1, 9):  # routine approvals and the agent's own run ends
            emit(task_id, 'task.needs_response', {'kind': 'approval', 'requestId': 'r' + str(i), 'text': 'Run tests?'})
            emit(task_id, 'task.finished', {'outcome': 'stopped' if i % 2 else 'done'})
            sent += 2
        for i in range(5):  # the user typing in PandaCode
            emit(task_id, 'task.human_message', {'text': 'note ' + str(i)})
            sent += 1
        for _ in range(4):  # the same callback retried
            self.core.deliver(early)
            sent += 1
        for _ in range(2):  # delayed callbacks from before the task was supervised
            stale = self.core.event(task_id, 'task.finished', {'outcome': 'done'}, snapshot={'level': None, 'epoch': 0})
            self.assertEqual(self.core.deliver(stale), (200, 'dropped_stale'))
            sent += 1
        emit(task_id, 'task.finished', {'outcome': 'failed'})
        emit(task_id, 'task.finished', {'outcome': 'failed'})
        sent += 2
        self.assertEqual(sent, 30)
        self.dispatcher.drain()
        self.assertLessEqual(len(self.host.messages), 1, 'one wake-up carries the whole burst')
        self.assertLessEqual(len(self.host.notifications()), 1)
        [message] = self.host.messages
        self.assertGreater(len(message['superseded']), 1)

    def test_one_event_at_a_time_still_stays_quiet(self):
        task_id = self.launch()
        for i in range(10):
            self.core.emit(task_id, 'task.needs_response', {'kind': 'approval', 'requestId': 'r' + str(i)})
            self.core.emit(task_id, 'task.human_responded', {'requestId': 'r' + str(i), 'approved': True})
            self.core.emit(task_id, 'task.finished', {'outcome': 'failed'})
            self.dispatcher.drain()
        self.assertEqual(len(self.host.notifications()), 1, 'repeated failures surface once per delegation')

    # ---- both handover directions ----

    def test_gerry_launches_then_hands_the_task_to_the_user(self):
        task_id = self.launch()  # workflow 2: from the Discord DM
        note = {'changed': 'Fixed the token refresh.', 'checked': 'Unit tests pass.',
                'open': 'No browser check yet.', 'next': 'Try a login in Safari.'}
        self.assertTrue(self.act(task_id, 'release_task', handover=note)['ok'])
        tool, args = self.core.calls[-1]
        self.assertEqual((tool, args['handover'], args['epoch']), ('pandacode_release_task', note, 1))
        self.assertIn('Still open: No browser check yet.', delegation.handover_text(note))
        self.dispatcher.drain()
        self.assertEqual(self.host.messages, [], 'own release is not news')
        with self.assertRaises(delegation.DelegationInactive):
            self.act(task_id, 'send_message', text='one more thing')
        with self.assertRaises(ValueError):
            delegation.handover_summary({'changed': 'x' * 1501})
        with self.assertRaises(ValueError):
            delegation.handover_summary({'summary': 'not a field'})

    def test_user_takes_over_then_hands_back_same_task(self):
        task_id = self.launch()
        self.core.take_over(task_id)
        stale = self.core.event(task_id, 'task.finished', {'outcome': 'done'}, snapshot={'level': 'manage', 'epoch': 1})
        self.assertEqual(self.core.hand_over(task_id, 'watch'), (202, 'stored'))
        self.assertEqual(self.core.deliver(stale), (200, 'dropped_stale'))
        self.dispatcher.drain()
        [message] = self.host.messages
        self.assertEqual(message['delegation'], {'taskId': task_id, 'level': 'watch', 'epoch': 3})
        self.assertEqual(message['notify']['policy'], 'quiet')
        with self.assertRaises(delegation.DelegationInactive):
            self.act(task_id, 'send_message', text='watch only now')
        self.assertEqual(self.store.mapping(task_id)['session_key'], ORIGIN['session_key'], 'same task, same DM')

    def test_user_task_attached_then_taken_back(self):
        self.core.task('mine')  # workflow 1 then back to the user
        self.attach('mine', 'manage')
        self.core.take_over('mine')
        self.assertEqual(self.core.emit('mine', 'task.human_message', {'text': 'I have it'}), (200, 'dropped_stale'))
        self.dispatcher.drain()
        self.assertEqual(self.host.messages, [])

    def test_legacy_binding_without_delegation_is_unchanged(self):
        self.store.bind(task_id='old', **ORIGIN, brief='b', constraints='c', authority='a')
        self.core.task('old', 'hermes', 'manage')
        self.core.emit('old', 'task.human_message')
        self.dispatcher.drain()
        self.assertEqual(len(self.host.messages), 1)
        self.assertNotIn('notify', self.host.messages[0])

    # ---- caller epoch and attach refresh ----

    def test_a_stale_child_cannot_adopt_a_regrant_between_calls(self):
        task_id = self.launch()
        old_epoch = delegation.get(self.store.db, task_id)['epoch']
        delegation.assert_active(self.store, task_id, old_epoch, write=True)  # the child's check passes
        self.core.take_over(task_id)
        self.core.hand_over(task_id) if hasattr(self.core, 'hand_over') else self.attach(task_id, 'manage')
        current = delegation.get(self.store.db, task_id)['epoch']
        self.assertGreater(current, old_epoch)
        writes = len(self.writes())
        with self.assertRaises(delegation.DelegationInactive):
            self.act(task_id, 'send_message', _epoch=old_epoch, text='from the old delegation')
        self.assertEqual(len(self.writes()), writes, 'zero MCP writes for the stale caller')
        self.assertTrue(self.act(task_id, 'send_message', _epoch=current, text='fresh')['ok'])
        self.assertEqual(self.writes()[-1][1]['epoch'], current)

    def test_act_requires_the_callers_epoch(self):
        task_id = self.launch()
        writes = len(self.writes())
        for epoch in (None, True, -1, '1'):
            with self.assertRaises(ValueError):
                self.act(task_id, 'send_message', _epoch=epoch, text='x')
        self.assertEqual(len(self.writes()), writes)

    def test_reattach_refreshes_restrictions_with_the_confirmed_level(self):
        self.core.task('t5', status='running')
        self.attach('t5', 'watch')
        watching = self.store.mapping('t5')
        self.assertIn('Watch only', watching['constraints'])
        self.attach('t5', 'manage')
        managing = self.store.mapping('t5')
        self.assertEqual((managing['constraints'], managing['authority']), delegation.Attacher.DEFAULTS['manage'])
        self.assertEqual(managing['brief'], watching['brief'])
        self.assertNotIn('Never message', managing['authority'])
        # Explicit new restrictions replace the old ones, and the history keeps the earlier ones.
        self.attach('t5', 'manage', brief='Only touch the login form', constraints='No deploys', authority='Approve test runs only')
        explicit = self.store.mapping('t5')
        self.assertEqual((explicit['brief'], explicit['constraints'], explicit['authority']),
                         ('Only touch the login form', 'No deploys', 'Approve test runs only'))
        # Back to watch without text: the manage authority does not survive.
        self.attach('t5', 'watch')
        self.assertEqual(self.store.mapping('t5')['authority'], delegation.Attacher.DEFAULTS['watch'][1])
        self.assertEqual({k: self.store.mapping('t5')[k] for k in ('profile', 'session_key', 'conversation_id')},
                         {k: ORIGIN[k] for k in ('profile', 'session_key', 'conversation_id')}, 'identity never changes')
        self.assertGreaterEqual(len(self.store.brief_history('t5')), 3)


class EnabledToolTests(unittest.TestCase):
    """The enabled entrypoint with a stub host facade (not the real Hermes PluginContext)."""
    def test_tools_register_and_refuse_through_the_handlers(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name).resolve()
        token = 'hermes-origin-v1:' + json.dumps(dict(host='cli', home=str(home), key='cli:route',
                                                      anchor='first', generation='g'))
        origin = dict(profile='default', session_key='cli:route', conversation_id=token)
        # Restores whatever module was there (the real one on a Hermes host) instead of popping it,
        # so later tests in the same process keep the host's own constants and home scope.
        patcher = mock.patch.dict(sys.modules, {'hermes_constants': types.SimpleNamespace(get_hermes_home=lambda: str(home))})
        patcher.start()
        self.addCleanup(patcher.stop)
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location('stub_enabled_bridge', root / '__init__.py',
                                                      submodule_search_locations=[str(root)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        core = FakeCore(None)
        core.task('t1')

        class Ctx:
            profile_name = 'default'
            tools, hooks, cleanups = {}, {}, []
            def get_config(self, key, default=None):
                return {'enabled': True, 'expected_identity_contract': 'host-origin-v1', 'port': 0}.get(key, default)
            def register_tool(self, **kw): self.tools[kw['name']] = kw['handler']
            def register_hook(self, name, cb): self.hooks[name] = cb
            def on_unload(self, cb): self.cleanups.append(cb)
            def resolve_origin(self, sid): return dict(origin) if sid == 'first' else None
            def inject_message(self, content, *, session_key, expected_session_id): return True
            call_mcp = staticmethod(core.call_mcp)

        ctx = Ctx()
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, PANDACODE_BRIDGE_WEBHOOK_SECRET='s' * 32):
            module.register(ctx)
        self.addCleanup(lambda: [cb() for cb in reversed(ctx.cleanups)])
        self.assertLessEqual({'pandacode_bridge_attach', 'pandacode_bridge_act', 'pandacode_bridge_check',
                              'pandacode_bridge_brief'}, set(ctx.tools))
        ctx.hooks['pre_llm_call'](session_id='first')
        call = lambda name, args, **kw: json.loads(ctx.tools[name](args, **kw))
        self.assertEqual(call('pandacode_bridge_attach', {'taskId': 't1', 'level': 'watch', 'brief': 'Watch it'},
                              session_id='first'), {'taskId': 't1', 'level': 'watch', 'epoch': 1})
        refused = call('pandacode_bridge_act', {'taskId': 't1', 'action': 'stop_task', 'epoch': 1}, session_id='first')
        self.assertEqual(call('pandacode_bridge_act', {'taskId': 't1', 'action': 'stop_task'}, session_id='first')['error'], 'action_rejected', 'epoch is required')
        self.assertEqual((refused['error'], refused['reason']), ('refused_locally', 'watch_only'))
        self.assertEqual(call('pandacode_bridge_check', {'taskId': 't1', 'epoch': 1}), {'active': True, 'level': 'watch', 'epoch': 1})
        self.assertFalse(call('pandacode_bridge_check', {'taskId': 't1', 'epoch': 0})['active'])
        self.assertIn('Level: watch', call('pandacode_bridge_brief', {'taskId': 't1'}, session_id='first')['brief'])
        self.assertEqual(call('pandacode_bridge_brief', {'taskId': 't1'}, session_id='other')['error'], 'brief_rejected')
        self.assertEqual(call('pandacode_bridge_attach', {'taskId': 't1', 'level': 'manage'}, session_id='other')['error'],
                         'attach_failed_or_uncertain')
        self.assertEqual([c[0] for c in core.calls], ['pandacode_attach_task'])


if __name__ == '__main__':
    unittest.main()
