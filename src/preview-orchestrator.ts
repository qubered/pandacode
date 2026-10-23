/* Browser-preview stand-ins for the orchestrator commands: settings that persist in memory
 * (never the real token or secret), and a sample token to copy. */
import type { OrchestratorSettings, OrchestratorSettingsView } from './orchestrator-settings';

export function createPreviewOrchestrator() {
  let settings: OrchestratorSettings = { enabled: true, name: 'hermes', bind: '127.0.0.1', port: 7879, maxMode: 'auto', webhook: null };
  let tokenSet = true, webhookSecretSet = false;
  const view = (): OrchestratorSettingsView => ({ ...settings, mcpUrl: `http://${settings.bind}:${settings.port}/mcp`, running: settings.enabled, tokenSet, webhookSecretSet });
  return {
    settings: view,
    save(next: OrchestratorSettings, webhookSecret?: string | null): OrchestratorSettingsView {
      const name = String(next.name ?? '').trim();
      if (!name || name.length > 40 || !/^[A-Za-z0-9_-]+$/.test(name)) throw new Error('Use a short name of letters, numbers, - or _.');
      if (!['plan', 'ask', 'auto', 'full'].includes(next.maxMode)) throw new Error('Choose plan, ask, auto or full as the highest mode.');
      if (!/^(\d{1,3}\.){3}\d{1,3}$/.test(next.bind) && !next.bind.includes(':')) throw new Error('The address must be an IP address.');
      if (next.webhook) {
        let url: URL;
        try { url = new URL(next.webhook.url); } catch { throw new Error('The webhook URL is invalid.'); }
        if (!['http:', 'https:'].includes(url.protocol)) throw new Error('The webhook URL must use http or https.');
      }
      if (typeof webhookSecret === 'string') webhookSecretSet = webhookSecret.trim() !== '';
      settings = { ...next, name, webhook: next.webhook ?? null };
      if (settings.enabled) tokenSet = true;
      return view();
    },
    revealToken(): string { tokenSet = true; return 'preview-orchestrator-token'; },
    /** set_task_supervisor's change to a task: level only with a supervisor, a grant only at manage
     *  and equal to the task's mode, and a new epoch on any change. */
    supervise(task: { mode?: string; supervisor?: string | null; supervisionLevel?: string | null; supervisionEpoch?: number; supervisionGrant?: string | null }, supervisor: string | null, level: 'watch' | 'manage', grantMode: string | null = null) {
      if (grantMode && (!supervisor || level !== 'manage')) throw new Error('A mode can be granted only to an orchestrator that manages the task.');
      if (grantMode && grantMode !== task.mode) throw new Error(`The grant must be the task's current mode (${task.mode}).`);
      const next = { supervisor, supervisionLevel: supervisor ? level : null, supervisionGrant: grantMode || null };
      const changed = (task.supervisor ?? null) !== next.supervisor || (task.supervisionLevel ?? null) !== next.supervisionLevel || (task.supervisionGrant ?? null) !== next.supervisionGrant;
      return { ...next, supervisionEpoch: (task.supervisionEpoch ?? 0) + (changed ? 1 : 0) };
    },
  };
}
