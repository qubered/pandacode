import test from 'node:test';
import assert from 'node:assert/strict';
import { coreMode, coreUrl, coreLabel } from '../src/core-mode.ts';

test('a browser without ?core= is the preview; with it, a remote core', () => {
  assert.deepEqual(coreMode({ search: '', desktop: false, saved: null }), { desktop: false, remoteUrl: null, remote: false, preview: true, native: false });
  const remote = coreMode({ search: '?core=ws://100.87.96.22:7878', desktop: false, saved: null });
  assert.equal(remote.remoteUrl, 'ws://100.87.96.22:7878/');
  assert.deepEqual([remote.remote, remote.preview, remote.native, remote.desktop], [true, false, false, false]);
});

test('the desktop app uses its own core unless Settings saved a remote one', () => {
  assert.deepEqual(coreMode({ search: '', desktop: true, saved: null }), { desktop: true, remoteUrl: null, remote: false, preview: false, native: true });
  const saved = coreMode({ search: '', desktop: true, saved: 'wss://core.example:7878' });
  assert.deepEqual([saved.remote, saved.preview, saved.native, saved.desktop], [true, false, false, true]);
  // ?core= wins over the saved address; a browser never reads the saved one.
  assert.equal(coreMode({ search: '?core=ws://a:1', desktop: true, saved: 'ws://b:2' }).remoteUrl, 'ws://a:1/');
  assert.equal(coreMode({ search: '', desktop: false, saved: 'ws://b:2' }).remote, false);
});

test('only WebSocket addresses count', () => {
  for (const bad of ['http://x:1', 'javascript:alert(1)', 'not a url', '', null, undefined]) assert.equal(coreUrl(bad), null, String(bad));
  assert.equal(coreUrl('  ws://x:1  '), 'ws://x:1/');
  // A bad saved address leaves the desktop app on its own core.
  assert.equal(coreMode({ search: '', desktop: true, saved: 'http://x:1' }).native, true);
  assert.equal(coreLabel('ws://100.87.96.22:7878/'), '100.87.96.22:7878');
});
