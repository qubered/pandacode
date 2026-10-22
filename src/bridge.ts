import { coreMode, REMOTE_CORE_KEY } from './core-mode';
import type { AgentEvent, BrowserElement, BrowserStatus, TerminalEvent } from './types';

// Which core this window talks to (see core-mode.ts). `native` means the desktop app on its
// own core; `desktop` means the Tauri shell is present, also while tasks live on a remote core.
const savedCore = (() => { try { return localStorage.getItem(REMOTE_CORE_KEY); } catch { return null; } })();
const mode = coreMode({ search: typeof location === 'undefined' ? '' : location.search, desktop: typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window, saved: savedCore });
const remoteUrl = mode.remoteUrl;
export const { desktop, remote, preview, native } = mode;
/** The remote core's address while connected to one. */
export const remoteCore = remoteUrl;
/** Desktop app: remember (or forget, with null) a remote core and reload onto it. */
export function setRemoteCore(url: string | null): void {
  try { if (url) localStorage.setItem(REMOTE_CORE_KEY, url); else localStorage.removeItem(REMOTE_CORE_KEY); } catch { /* The choice lasts this session only. */ }
  location.reload();
}
/** A command for the desktop shell itself (window, files on this machine), never the remote core. */
export async function localCall<T>(command: string, args: Record<string, unknown> = {}): Promise<T> {
  if (!desktop) throw new Error(`${command} needs the desktop app.`);
  return (await import('@tauri-apps/api/core')).invoke<T>(command, args);
}
let transport: Promise<import('./remote-transport').Transport> | undefined;
const rt = () => transport ??= import('./remote-transport').then(module => module.connect(remoteUrl!));
/** Runs after the remote transport reconnects, so the app can reload what it missed. */
export async function onCoreReconnect(callback: () => void): Promise<() => void> {
  return remote ? (await rt()).onReconnect(callback) : () => {};
}
export async function onCoreStatus(callback: (status: import('./remote-transport').TransportStatus) => void): Promise<() => void> {
  return remote ? (await rt()).onStatus(callback) : () => {};
}
async function pickRemote(options: import('./remote-picker').PickOptions): Promise<string[]> {
  return (await import('./remote-picker')).pickRemote(call, options);
}
// Remote commands that act on this machine instead of the core: a link opens here, not on the
// server. Runs before any await so the browser still sees the click that asked for it.
// In the desktop app they go to its own shell; a browser opens them itself.
const LOCAL_IN_REMOTE: Record<string, (args: Record<string, unknown>) => unknown> = {
  open_external_url: args => {
    if (desktop) return localCall('open_external_url', args);
    const url = new URL(String(args.url));
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) throw new Error('Use an HTTP or HTTPS URL.');
    window.open(url.href, '_blank', 'noopener,noreferrer');
  },
  open_support: () => {
    if (desktop) return localCall('open_support');
    window.open('https://github.com/aicodeking/pandacode/issues', '_blank', 'noopener,noreferrer');
  },
};
export async function call<T>(command: string, args: Record<string, unknown> = {}): Promise<T> {
  if (remote && command in LOCAL_IN_REMOTE) return LOCAL_IN_REMOTE[command](args) as T;
  if (remote) return (await rt()).call<T>(command, args);
  if (preview) return (await import('./preview')).invoke<T>(command, args);
  return (await import('@tauri-apps/api/core')).invoke<T>(command, args);
}
export async function subscribe(name: 'agent-update-progress', callback: (event: import('./agent-updates').AgentUpdateProgress) => void): Promise<() => void>;
export async function subscribe(name: 'jev-run', callback: (event: import('./jev').JevReport) => void): Promise<() => void>;
export async function subscribe(name: 'jev-frame', callback: (event: import('./jev-live').JevFrame) => void): Promise<() => void>;
export async function subscribe(name: 'jev-install', callback: (event: { status: string; message: string }) => void): Promise<() => void>;
export async function subscribe(name: 'sessions-changed', callback: () => void): Promise<() => void>;
export async function subscribe(name: 'queue-changed', callback: (event: { sessionId: string }) => void): Promise<() => void>;
export async function subscribe(name: 'bots-changed', callback: () => void): Promise<() => void>;
export async function subscribe(name: 'bot-notification', callback: (event: { botId: string; text: string }) => void): Promise<() => void>;
export async function subscribe(name: 'bot-pairing', callback: (event: { botId: string; channelId: string; senderName: string; code: string }) => void): Promise<() => void>;
export async function subscribe(name: 'sessions-deleted', callback: (event: {sessionIds: string[]}) => void): Promise<() => void>;
export async function subscribe(name: 'provider-models-changed', callback: (event: {provider: import('./types').ProviderId}) => void): Promise<() => void>;
export async function subscribe(name: 'overlay-open-task', callback: (event: { sessionId: string; workerId?: string; workerSessionId?: string; subagentId?: string }) => void): Promise<() => void>;
export async function subscribe(name: 'architect-event', callback: (event: import('./architect-types').TeamEvent) => void): Promise<() => void>;
export async function subscribe(name: 'agent-ui-action', callback: (event: import('./agent-ui-actions').AgentUiAction) => void): Promise<() => void>;
export async function subscribe(name: 'android-setup', callback: (event: import('./simulator').AndroidSetup) => void): Promise<() => void>;
export async function subscribe(name: 'cloud-setup', callback: (event: { provider: string; label: string }) => void): Promise<() => void>;
export async function subscribe(name: 'vm-changed', callback: (event: { show?: boolean } | string[] | null) => void): Promise<() => void>;
export async function subscribe(name: 'simulator-changed', callback: (event: { udid: string; show: boolean }) => void): Promise<() => void>;
export async function subscribe(name: 'agent-event', callback: (event: AgentEvent) => void): Promise<() => void>;
export async function subscribe(name: 'terminal-event', callback: (event: TerminalEvent) => void): Promise<() => void>;
export async function subscribe(name: 'browser-state', callback: (event: BrowserStatus) => void): Promise<() => void>;
export async function subscribe(name: 'browser-element', callback: (event: BrowserElement) => void): Promise<() => void>;
export async function subscribe(name: string, callback: (event: any) => void): Promise<() => void> {
  if (remote) return (await rt()).subscribe(name, callback);
  if (preview) return (await import('./preview')).listen(name, callback);
  return (await import('@tauri-apps/api/event')).listen(name, event => callback(event.payload));
}
export async function chooseFolder(title = 'Open a project in PandaCode'): Promise<string | null> {
  if (remote) return (await pickRemote({ title, mode: 'folder' }))[0] ?? null;
  if (preview) return '/Users/you/Projects/new-project';
  const result = await (await import('@tauri-apps/plugin-dialog')).open({ directory: true, multiple: false, title });
  return typeof result === 'string' ? result : null;
}
/** Files to attach to a message. The preview returns a fixed sample selection. */
const ATTACHMENT_TYPES = ['png', 'jpg', 'jpeg', 'webp', 'pdf', 'txt', 'md', 'markdown', 'csv', 'json', 'yaml', 'yml', 'xml', 'html', 'rtf', 'doc', 'docx', 'odt', 'pages', 'key', 'numbers', 'pptx', 'xlsx', 'epub', 'log'];
export async function chooseAttachments(): Promise<string[]> {
  if (remote) return pickRemote({ title: 'Attach to your message', mode: 'files', extensions: ATTACHMENT_TYPES });
  if (preview) return (await import('./preview')).samplePick('files');
  const result = await (await import('@tauri-apps/plugin-dialog')).open({ multiple: true, title: 'Attach to your message', filters: [
    { name: 'Images and documents', extensions: ATTACHMENT_TYPES },
    { name: 'All files', extensions: ['*'] },
  ] });
  return Array.isArray(result) ? result : typeof result === 'string' ? [result] : [];
}
/** A VS Code extension package holding color themes. */
export async function chooseVsix(): Promise<string | null> {
  if (remote) return (await pickRemote({ title: 'Import a VS Code theme', mode: 'file', extensions: ['vsix'] }))[0] ?? null;
  if (preview) return '/Users/you/Downloads/tokyo-night.vsix';
  const result = await (await import('@tauri-apps/plugin-dialog')).open({ multiple: false, title: 'Import a VS Code theme', filters: [{ name: 'VS Code extension', extensions: ['vsix'] }] });
  return typeof result === 'string' ? result : null;
}
export async function chooseAttachmentFolder(): Promise<string | null> {
  if (remote) return (await pickRemote({ title: 'Attach a folder to your message', mode: 'folder' }))[0] ?? null;
  if (preview) return (await import('./preview')).samplePick('folder')[0] ?? null;
  const result = await (await import('@tauri-apps/plugin-dialog')).open({ directory: true, multiple: false, title: 'Attach a folder to your message' });
  return typeof result === 'string' ? result : null;
}
/** Any single file on the machine running the core (remote mode) or this one. */
export async function chooseFile(title: string): Promise<string | null> {
  if (remote) return (await pickRemote({ title, mode: 'file' }))[0] ?? null;
  const result = await (await import('@tauri-apps/plugin-dialog')).open({ directory: false, multiple: false, title });
  return typeof result === 'string' ? result : null;
}
