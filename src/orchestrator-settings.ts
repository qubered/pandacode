/* Settings → Agents → Orchestrator: the endpoint an orchestrator such as Hermes uses to run
 * tasks here, and the webhook that wakes it. The bearer token and webhook secret stay in the
 * OS keychain; this view only learns whether they are set, and copies the token on request
 * without ever drawing it. */
import './orchestrator-settings.css';
import { infoMark } from './tooltip';

export type OrchestratorMode = 'plan' | 'ask' | 'auto' | 'full';
export type OrchestratorSettings = { enabled: boolean; name: string; bind: string; port: number; maxMode: OrchestratorMode | string; webhook: { url: string } | null };
export type OrchestratorSettingsView = OrchestratorSettings & { mcpUrl: string; running: boolean; tokenSet: boolean; webhookSecretSet: boolean };
export type OrchestratorHost = {
  call<T>(command: string, args?: Record<string, unknown>): Promise<T>;
  /** Told after settings are saved, so the rest of the window learns the orchestrator's name and state. */
  changed?(view: OrchestratorSettingsView): void;
};

const MODES: [OrchestratorMode, string][] = [['plan', 'Plan'], ['ask', 'Ask'], ['auto', 'Accept edits'], ['full', 'Full access']];
const WEBHOOK_HELP = 'In Hermes, add a webhook route named pandacode under platforms.webhook.extra.routes with the same secret, then set Webhook URL to http://<hermes-host>:8644/webhooks/pandacode. PandaCode signs every event with it (X-Webhook-Signature-V2) and only sends events for tasks Hermes supervises.';
const NAME_HELP = 'Shown on supervised tasks and on messages it sends. Letters, numbers, - and _.';
const MODE_HELP = 'The most autonomous mode the orchestrator may launch a task in. Anything higher is lowered to this.';

/** The orchestrator's Hermes config, ready to paste. The token is a placeholder: Copy token fetches the real one. */
export function hermesSnippet(view: Pick<OrchestratorSettingsView, 'name' | 'mcpUrl'>): string {
  return `mcp_servers:\n  pandacode:\n    url: "${view.mcpUrl}"\n    headers: { Authorization: "Bearer <token>" }`;
}

const row = (title: string, control: string, detail = '') => `<div class="setting-row"><div><strong>${title}</strong>${detail}</div><div class="orchestrator-control">${control}</div></div>`;

export function mountOrchestratorSettings(host: HTMLElement, options: OrchestratorHost): () => void {
  host.innerHTML = `<div class="settings-group orchestrator-settings" aria-label="Orchestrator">
    <div class="setting-row"><div><strong>Orchestrator</strong><small class="orchestrator-state" role="status">Loading…</small></div><input type="checkbox" class="settings-switch" name="enabled" aria-label="Enable orchestrator"></div>
    <form class="orchestrator-form" novalidate aria-label="Orchestrator settings">
      ${row('Name', '<input class="orchestrator-input" name="name" type="text" maxlength="40" autocomplete="off" spellcheck="false" aria-label="Orchestrator name">', infoMark(NAME_HELP))}
      ${row('Address', '<input class="orchestrator-input" name="bind" type="text" autocomplete="off" spellcheck="false" aria-label="Address" placeholder="127.0.0.1">')}
      ${row('Port', '<input class="orchestrator-input orchestrator-port" name="port" type="number" min="1" max="65535" aria-label="Port">')}
      ${row('Highest mode', `<select class="orchestrator-input" name="maxMode" aria-label="Highest mode">${MODES.map(([id, label]) => `<option value="${id}">${label}</option>`).join('')}</select>`, infoMark(MODE_HELP))}
      ${row('Webhook URL', '<input class="orchestrator-input orchestrator-wide" name="webhookUrl" type="text" autocomplete="off" spellcheck="false" aria-label="Webhook URL" placeholder="http://hermes-host:8644/webhooks/pandacode">', infoMark(WEBHOOK_HELP))}
      ${row('Webhook secret', '<input class="orchestrator-input" name="webhookSecret" type="password" autocomplete="new-password" spellcheck="false" aria-label="Webhook secret"><button type="button" class="secondary-button" data-orchestrator="remove-secret" hidden>Remove</button>')}
      <div class="orchestrator-footer"><p class="orchestrator-error" role="alert" hidden></p><p class="orchestrator-saved" role="status" hidden></p><button type="submit" class="primary-button">Save</button></div>
    </form>
    <div class="setting-row"><div><strong>MCP URL</strong><code class="orchestrator-url"></code></div><div class="orchestrator-control"><button type="button" class="secondary-button" data-orchestrator="copy-url">Copy URL</button></div></div>
    <div class="setting-row"><div><strong>Token</strong><small class="orchestrator-token-state">Not created yet</small></div><div class="orchestrator-control"><button type="button" class="secondary-button" data-orchestrator="copy-token">Copy token</button></div></div>
    <div class="orchestrator-snippet-block"><div class="orchestrator-snippet-head"><strong>Hermes config</strong>${infoMark('Paste this into Hermes config.yaml and replace <token> with the token from Copy token. Hermes reads the pandacode tools from it. The webhook route is described beside Webhook URL.')}<button type="button" class="secondary-button" data-orchestrator="copy-snippet">Copy snippet</button></div><pre class="orchestrator-snippet" tabindex="0"></pre></div>
  </div>`;
  const $ = <T extends HTMLElement>(selector: string) => host.querySelector<T>(selector)!;
  const form = $<HTMLFormElement>('form'), enabled = $<HTMLInputElement>('[name="enabled"]');
  const field = (name: string) => form.elements.namedItem(name) as HTMLInputElement | HTMLSelectElement;
  const errorLine = $('.orchestrator-error'), savedLine = $('.orchestrator-saved');
  let view: OrchestratorSettingsView | undefined, disposed = false, busy = false, savedTimer = 0;

  const message = (error: unknown) => error instanceof Error ? error.message : String(error);
  function say(kind: 'error' | 'saved', text: string): void {
    const line = kind === 'error' ? errorLine : savedLine;
    line.textContent = text; line.hidden = !text;
    if (kind === 'saved') { window.clearTimeout(savedTimer); if (text) savedTimer = window.setTimeout(() => { savedLine.hidden = true; }, 3000); }
  }
  function fill(next: OrchestratorSettingsView): void {
    view = next;
    enabled.checked = next.enabled;
    (field('name') as HTMLInputElement).value = next.name;
    (field('bind') as HTMLInputElement).value = next.bind;
    (field('port') as HTMLInputElement).value = String(next.port);
    field('maxMode').value = next.maxMode;
    (field('webhookUrl') as HTMLInputElement).value = next.webhook?.url ?? '';
    const secret = field('webhookSecret') as HTMLInputElement;
    secret.value = ''; secret.placeholder = next.webhookSecretSet ? 'Saved · leave empty to keep' : 'From hermes webhook subscribe';
    $<HTMLElement>('[data-orchestrator="remove-secret"]').hidden = !next.webhookSecretSet;
    $('.orchestrator-state').textContent = !next.enabled ? 'Off' : next.running ? `Running · ${next.name} can use this app` : 'On · starts when saved';
    $('.orchestrator-url').textContent = next.mcpUrl;
    $('.orchestrator-token-state').textContent = next.tokenSet ? 'Created · never shown here' : 'Created when you turn this on';
    $('.orchestrator-snippet').textContent = hermesSnippet(next);
  }
  function setBusy(value: boolean): void {
    busy = value;
    host.querySelectorAll<HTMLButtonElement | HTMLInputElement>('button, input[type="checkbox"]').forEach(control => { control.disabled = value; });
  }
  async function load(): Promise<void> {
    try { const next = await options.call<OrchestratorSettingsView>('orchestrator_settings'); if (!disposed) fill(next); }
    catch (error) { if (!disposed) $('.orchestrator-state').textContent = message(error); }
  }
  /** Writes the form, restarting the endpoint. `secret` is undefined to keep the saved one, or '' to remove it. */
  async function save(secret: string | undefined, enable = enabled.checked): Promise<boolean> {
    if (busy) return false;
    const port = Number((field('port') as HTMLInputElement).value), url = (field('webhookUrl') as HTMLInputElement).value.trim();
    if (!Number.isInteger(port) || port < 1 || port > 65535) { say('error', 'Enter a port from 1 to 65535.'); return false; }
    const settings: OrchestratorSettings = { enabled: enable, name: (field('name') as HTMLInputElement).value.trim(), bind: (field('bind') as HTMLInputElement).value.trim(), port, maxMode: field('maxMode').value, webhook: url ? { url } : null };
    say('error', ''); say('saved', ''); setBusy(true);
    try {
      const args: Record<string, unknown> = { settings };
      if (secret !== undefined) args.webhookSecret = secret;
      const next = await options.call<OrchestratorSettingsView>('orchestrator_save_settings', args);
      if (disposed) return true;
      fill(next); options.changed?.(next); say('saved', secret === '' ? 'Webhook secret removed' : 'Saved');
      return true;
    } catch (error) {
      if (!disposed) { say('error', message(error)); enabled.checked = view?.enabled ?? false; }
      return false;
    } finally { if (!disposed) setBusy(false); }
  }
  /** Copies without drawing: the clipboard gets the text, the page never does. */
  async function copy(text: string, done: string): Promise<void> {
    try { await navigator.clipboard.writeText(text); say('error', ''); say('saved', done); }
    catch { say('saved', ''); say('error', 'Copying isn’t available here. Select the text and copy it instead.'); }
  }
  async function copyToken(): Promise<void> {
    if (busy) return;
    say('error', ''); setBusy(true);
    try {
      const token = await options.call<string>('orchestrator_reveal_token');
      if (disposed) return;
      await copy(token, 'Token copied');
      const next = await options.call<OrchestratorSettingsView>('orchestrator_settings').catch(() => undefined);
      if (next && !disposed) fill(next);
    } catch (error) { if (!disposed) say('error', message(error)); }
    finally { if (!disposed) setBusy(false); }
  }

  const submit = (event: Event) => { event.preventDefault(); const secret = (field('webhookSecret') as HTMLInputElement).value.trim(); void save(secret || undefined); };
  const toggle = () => { void save(undefined); };
  const click = (event: MouseEvent) => {
    const target = event.target instanceof Element ? event.target.closest<HTMLElement>('[data-orchestrator]') : null;
    if (!target || !host.contains(target)) return;
    switch (target.dataset.orchestrator) {
      case 'remove-secret': void save(''); break;
      case 'copy-url': void copy($('.orchestrator-url').textContent ?? '', 'URL copied'); break;
      case 'copy-token': void copyToken(); break;
      case 'copy-snippet': void copy($('.orchestrator-snippet').textContent ?? '', 'Snippet copied'); break;
    }
  };
  form.addEventListener('submit', submit);
  enabled.addEventListener('change', toggle);
  host.addEventListener('click', click);
  void load();
  return () => {
    disposed = true; window.clearTimeout(savedTimer);
    form.removeEventListener('submit', submit); enabled.removeEventListener('change', toggle); host.removeEventListener('click', click);
    host.replaceChildren();
  };
}
