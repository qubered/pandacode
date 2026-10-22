// File and folder picker for remote mode: projects and attachments live on the
// machine running the core, so the native dialog on this machine cannot see them.
// Browses through the core's `fs_home` and `fs_list` commands.
import { escape, icon } from './dom';
import './remote-picker.css';

export type RemoteEntry = { name: string; path: string; kind: 'dir' | 'file'; size: number };
export type RemoteListing = { path: string; parent: string | null; entries: RemoteEntry[] };
export type PickMode = 'folder' | 'file' | 'files';
export type PickOptions = { title: string; mode: PickMode; extensions?: string[] };
type Call = <T>(command: string, args?: Record<string, unknown>) => Promise<T>;

/** Whether `name` passes an extension filter (`['*']` or none passes everything). */
export function matchesExtensions(name: string, extensions?: string[]): boolean {
  if (!extensions?.length || extensions.includes('*')) return true;
  const dot = name.lastIndexOf('.');
  return dot > 0 && extensions.includes(name.slice(dot + 1).toLowerCase());
}

let lastFolder: string | null = null;

/** Resolves to the chosen paths, or an empty list when dismissed. */
export function pickRemote(call: Call, options: PickOptions): Promise<string[]> {
  const { mode, extensions } = options;
  const dialog = document.createElement('dialog');
  dialog.className = 'remote-picker';
  dialog.setAttribute('aria-label', options.title);
  dialog.innerHTML = `<div class="dialog-heading"><h2>${escape(options.title)}</h2><button type="button" class="icon-button" data-act="cancel" aria-label="Close">${icon('close')}</button></div>
<p class="dialog-description">Files on the PandaCode core.</p>
<form class="remote-picker-path"><button type="button" class="icon-button" data-act="up" aria-label="Parent folder" title="Parent folder">${icon('back')}</button><input name="path" aria-label="Folder path" autocomplete="off" spellcheck="false"></form>
<div class="remote-picker-list" role="listbox" aria-label="Folder contents" ${mode === 'files' ? 'aria-multiselectable="true"' : ''}></div>
<p class="remote-picker-error field-help" role="alert" hidden></p>
<div class="dialog-footer"><label class="remote-picker-hidden"><input type="checkbox" data-act="hidden"> Show hidden</label><button type="button" class="secondary-button" data-act="cancel">Cancel</button><button type="button" class="primary-button" data-act="choose"></button></div>`;
  const input = dialog.querySelector<HTMLInputElement>('input[name=path]')!;
  const list = dialog.querySelector<HTMLElement>('.remote-picker-list')!;
  const error = dialog.querySelector<HTMLElement>('.remote-picker-error')!;
  const choose = dialog.querySelector<HTMLButtonElement>('[data-act=choose]')!;
  const hidden = dialog.querySelector<HTMLInputElement>('[data-act=hidden]')!;
  let listing: RemoteListing | null = null;
  const selected = new Set<string>();
  let request = 0;

  const update = () => {
    choose.disabled = mode === 'folder' ? !listing : selected.size === 0;
    choose.textContent = mode === 'folder' ? 'Choose this folder' : selected.size > 1 ? `Choose ${selected.size} files` : 'Choose';
  };
  const draw = () => {
    if (!listing) { list.innerHTML = ''; return; }
    const rows = listing.entries.filter(entry => entry.kind === 'dir' || (mode !== 'folder' && matchesExtensions(entry.name, extensions)));
    list.innerHTML = rows.length
      ? rows.map(entry => `<button type="button" role="option" class="remote-picker-row" data-path="${escape(entry.path)}" data-kind="${entry.kind}" aria-selected="${selected.has(entry.path)}"${mode === 'folder' && entry.kind === 'file' ? ' disabled' : ''}>${icon(entry.kind === 'dir' ? 'folder' : 'file')}<span>${escape(entry.name)}</span></button>`).join('')
      : '<p class="remote-picker-empty">This folder is empty.</p>';
    update();
  };
  const open = async (path?: string) => {
    const mine = ++request;
    error.hidden = true;
    try {
      const next = await call<RemoteListing>('fs_list', { path: path ?? await call<string>('fs_home'), showHidden: hidden.checked });
      if (mine !== request) return;
      listing = next; lastFolder = next.path; input.value = next.path;
      if (mode !== 'files') selected.clear();
      draw();
      list.scrollTop = 0;
    } catch (cause) {
      if (mine !== request) return;
      error.textContent = cause instanceof Error ? cause.message : String(cause);
      error.hidden = false;
      if (listing) input.value = listing.path;
    }
  };

  return new Promise<string[]>(resolve => {
    const finish = (paths: string[]) => { resolve(paths); dialog.close(); dialog.remove(); };
    dialog.addEventListener('cancel', event => { event.preventDefault(); finish([]); });
    dialog.querySelector('form')!.addEventListener('submit', event => { event.preventDefault(); void open(input.value.trim() || undefined); });
    hidden.addEventListener('change', () => void open(listing?.path));
    dialog.addEventListener('click', event => {
      const target = (event.target as HTMLElement).closest<HTMLElement>('[data-act],.remote-picker-row');
      if (!target) return;
      const act = target.dataset.act;
      if (act === 'cancel') finish([]);
      else if (act === 'up') { if (listing?.parent) void open(listing.parent); }
      else if (act === 'choose') finish(mode === 'folder' ? (listing ? [listing.path] : []) : [...selected]);
      else if (target.dataset.kind === 'dir') void open(target.dataset.path);
      else if (target.dataset.path) {
        const path = target.dataset.path;
        if (mode === 'files') { if (!selected.delete(path)) selected.add(path); }
        else { selected.clear(); selected.add(path); }
        draw();
      }
    });
    list.addEventListener('dblclick', event => {
      const row = (event.target as HTMLElement).closest<HTMLElement>('.remote-picker-row');
      if (row?.dataset.kind === 'file' && mode === 'file') finish([row.dataset.path!]);
    });
    document.body.append(dialog);
    dialog.showModal();
    update();
    void open(lastFolder ?? undefined);
  });
}
