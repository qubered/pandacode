import test from 'node:test';
import assert from 'node:assert/strict';
import { connect } from '../src/remote-transport.ts';

// A scripted socket: the test opens, feeds and drops it by hand.
function harness() {
  const sockets = [];
  const socket = url => {
    const s = { url, sent: [], readyState: 0, onopen: null, onclose: null, onerror: null, onmessage: null };
    s.send = frame => { assert.equal(s.readyState, 1, 'sent only on an open socket'); s.sent.push(JSON.parse(frame)); };
    s.close = () => { if (s.readyState === 3) return; s.readyState = 3; s.onclose?.({}); };
    s.open = () => { s.readyState = 1; s.onopen?.({}); };
    s.receive = frame => s.onmessage?.({ data: JSON.stringify(frame) });
    s.drop = () => { s.readyState = 3; s.onclose?.({}); };
    sockets.push(s);
    return s;
  };
  const transport = connect('ws://core:7878', { socket, backoff: () => 0 });
  return { sockets, transport, last: () => sockets.at(-1) };
}
const tick = () => new Promise(resolve => setTimeout(resolve, 0));

test('calls made before the socket opens go out once it does, and resolve by id', async () => {
  const { transport, last } = harness();
  assert.equal(last().url, 'ws://core:7878');
  const listed = transport.call('list_projects');
  const failed = transport.call('get_task', { sessionId: 'nope' });
  assert.deepEqual(last().sent, []);
  last().open();
  assert.deepEqual(last().sent, [{ subscribe: [] }, { id: 1, command: 'list_projects', args: {} }, { id: 2, command: 'get_task', args: { sessionId: 'nope' } }]);
  last().receive({ id: 2, ok: false, error: 'Task not found' });
  last().receive({ id: 1, ok: true, result: [{ id: 'p1' }] });
  assert.deepEqual(await listed, [{ id: 'p1' }]);
  await assert.rejects(failed, /Task not found/);
});

test('events fan out to every subscriber of that name, and the core learns the set', async () => {
  const { transport, last } = harness();
  last().open();
  const seen = [];
  const offA = await transport.subscribe('agent-event', payload => seen.push(['a', payload]));
  await transport.subscribe('agent-event', payload => seen.push(['b', payload]));
  await transport.subscribe('terminal-event', payload => seen.push(['t', payload]));
  assert.deepEqual(last().sent.at(-1), { subscribe: ['agent-event', 'terminal-event'] });
  last().receive({ event: 'agent-event', payload: { kind: 'delta' } });
  last().receive({ event: 'sessions-changed', payload: null });
  assert.deepEqual(seen, [['a', { kind: 'delta' }], ['b', { kind: 'delta' }]]);
  offA();
  const frames = last().sent.length;
  last().receive({ event: 'agent-event', payload: 2 });
  assert.deepEqual(seen.at(-1), ['b', 2]);
  assert.equal(last().sent.length, frames, 'a name with listeners left is not resubscribed');
});

test('a drop rejects calls in flight, reconnects, re-sends subscriptions and reports the reconnect', async () => {
  const { transport, sockets, last } = harness();
  const statuses = [];
  transport.onStatus(status => statuses.push(status));
  let reconnects = 0;
  transport.onReconnect(() => reconnects++);
  last().open();
  await transport.subscribe('agent-event', () => {});
  const inFlight = transport.call('send_message', { sessionId: 's', text: 'hi' });
  last().drop();
  await assert.rejects(inFlight, /Lost the connection/);
  assert.equal(reconnects, 0);
  await tick();
  assert.equal(sockets.length, 2, 'dialed again');
  // A call made while reconnecting survives a failed attempt and is sent exactly once.
  const queued = transport.call('list_sessions');
  last().drop();
  await tick();
  assert.equal(sockets.length, 3);
  last().open();
  assert.deepEqual(last().sent, [{ subscribe: ['agent-event'] }, { id: 2, command: 'list_sessions', args: {} }]);
  assert.equal(reconnects, 1);
  last().receive({ id: 2, ok: true, result: [] });
  assert.deepEqual(await queued, []);
  assert.ok(!sockets.slice(1).some(s => s.sent.some(frame => frame.command === 'send_message')), 'send_message is never replayed');
  assert.deepEqual(statuses, ['open', 'closed', 'connecting', 'closed', 'connecting', 'open']);
});

test('close stops reconnecting and rejects what is pending', async () => {
  const { transport, sockets, last } = harness();
  last().open();
  const pending = transport.call('list_projects');
  transport.close();
  await assert.rejects(pending);
  await tick();
  assert.equal(sockets.length, 1);
  await assert.rejects(transport.call('list_projects'), /closed/);
});
