// Settings → General → Remote core: point the desktop app at a headless core
// (docs/remote-core.md) instead of its own, or go back. In a browser the address comes from
// `?core=`, so the section only says where it is connected.
import { desktop, remote, remoteCore, setRemoteCore } from './bridge';
import { coreLabel, coreUrl } from './core-mode';
import { escape } from './dom';

export function remoteCoreSettingsHtml(): string {
  if (!desktop) {
    return remote ? `<div class="settings-group"><div class="setting-row"><div><strong>Connected to ${escape(coreLabel(remoteCore!))}</strong><small>Chosen with ?core= in this page's address.</small></div></div></div>` : '';
  }
  return `<div class="settings-group remote-core-settings">
    <div class="setting-row"><div><strong>${remote ? `Connected to ${escape(coreLabel(remoteCore!))}` : 'This computer'}</strong><small>${remote ? 'Tasks, projects and agents run on that server. Window, zoom and updates stay here.' : 'Tasks and agents run on this computer.'}</small></div>${remote ? '<button type="button" class="secondary-button" data-remote-core="disconnect">Use this computer</button>' : ''}</div>
    <form class="setting-row remote-core-form"><div><label for="remote-core-url"><strong>Remote core address</strong></label><small>A headless PandaCode core, such as ws://100.87.96.22:7878.</small></div><div class="remote-core-connect"><input id="remote-core-url" name="url" placeholder="ws://host:7878" value="${escape(remoteCore ?? '')}" autocomplete="off" spellcheck="false"><button class="primary-button" data-remote-core="connect">${remote ? 'Switch' : 'Connect'}</button></div></form>
    <p class="field-help remote-core-error" role="alert" hidden></p>
  </div>`;
}

/** Tries the address before switching, so a typo never leaves the window on a dead core. */
export function probeCore(url: string, timeoutMs = 5000): Promise<void> {
  return new Promise((resolve, reject) => {
    const socket = new WebSocket(url);
    const timer = setTimeout(() => { socket.close(); reject(new Error(`No answer from ${coreLabel(url)}.`)); }, timeoutMs);
    socket.onopen = () => { clearTimeout(timer); socket.close(); resolve(); };
    socket.onerror = () => { clearTimeout(timer); reject(new Error(`Could not connect to ${coreLabel(url)}. Check the address and that pandacode-core is running.`)); };
  });
}

export function mountRemoteCoreSettings(host: HTMLElement): () => void {
  const error = host.querySelector<HTMLElement>('.remote-core-error');
  const show = (message: string) => { if (error) { error.textContent = message; error.hidden = !message; } };
  const submit = async (event: Event) => {
    event.preventDefault();
    const input = host.querySelector<HTMLInputElement>('#remote-core-url');
    const button = host.querySelector<HTMLButtonElement>('[data-remote-core="connect"]');
    const url = coreUrl(input?.value);
    if (!url) { show('Use a ws:// or wss:// address, such as ws://100.87.96.22:7878.'); return; }
    show('');
    if (button) button.disabled = true;
    try { await probeCore(url); setRemoteCore(url); } catch (cause) { show(cause instanceof Error ? cause.message : String(cause)); } finally { if (button) button.disabled = false; }
  };
  const click = (event: Event) => {
    if ((event.target as HTMLElement).closest('[data-remote-core="disconnect"]')) setRemoteCore(null);
  };
  const form = host.querySelector('form');
  form?.addEventListener('submit', submit);
  host.addEventListener('click', click);
  return () => { form?.removeEventListener('submit', submit); host.removeEventListener('click', click); };
}
