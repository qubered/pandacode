// Which core this window talks to, and through what. Pure, so tests can cover every case.
//   preview:  a browser with no core (fixtures)
//   native:   the desktop app on its own local core (Tauri IPC)
//   remote:   a headless core over a WebSocket, from a browser (`?core=`) or from the
//             desktop app (Settings → Remote core, remembered in local storage)
// `desktop` is the Tauri shell itself: window chrome, zoom, updates and local files stay
// with it even while tasks live on a remote core.

export const REMOTE_CORE_KEY = 'pandacode.remoteCore';

export type CoreMode = { desktop: boolean; remoteUrl: string | null; remote: boolean; preview: boolean; native: boolean };

/** Only a WebSocket address counts; anything else leaves the window in its normal mode. */
export function coreUrl(value: string | null | undefined): string | null {
  if (!value) return null;
  try { const url = new URL(value.trim()); return url.protocol === 'ws:' || url.protocol === 'wss:' ? url.href : null; } catch { return null; }
}

/** A `?core=` address wins; the desktop app otherwise uses the address saved in Settings. */
export function coreMode(input: { search: string; desktop: boolean; saved: string | null }): CoreMode {
  const remoteUrl = coreUrl(new URLSearchParams(input.search).get('core')) ?? (input.desktop ? coreUrl(input.saved) : null);
  const remote = !!remoteUrl;
  return { desktop: input.desktop, remoteUrl, remote, preview: !remote && !input.desktop, native: !remote && input.desktop };
}

/** A short label for the connected server, such as `100.87.96.22:7878`. */
export function coreLabel(url: string): string {
  try { return new URL(url).host; } catch { return url; }
}
