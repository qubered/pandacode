/* How the window names the orchestrator that supervises tasks (Hermes). */

/** "hermes" is shown as "Hermes". */
export const orchestratorLabel = (name: string): string => name ? name.charAt(0).toUpperCase() + name.slice(1) : name;

/** A handover note the orchestrator left when it released a task (`role: 'handover'`, JSON content). */
export function handoverHtml(content: string, escape: (text: string) => string): string {
  let note: Record<string, unknown>;
  try { note = JSON.parse(content); } catch { return ''; }
  const rows = (['changed', 'checked', 'open', 'next'] as const).map((field, i) => typeof note[field] === 'string' && note[field] ? `<p><strong>${['Changed', 'Checked', 'Open', 'Next'][i]}:</strong> ${escape(note[field] as string)}</p>` : '').join('');
  const from = typeof note.from === 'string' ? orchestratorLabel(note.from) : 'the orchestrator';
  return `<details class="provider-handoff supervisor-handover" open><summary><span>Handover from ${escape(from)}</span></summary>${rows}</details>`;
}

const MODES = ['plan', 'ask', 'auto', 'full'];
/** Whether a task's mode is above the orchestrator's highest mode (unknown modes are not). */
export const modeAbove = (mode: string, max: string): boolean => {
  const wanted = MODES.indexOf(mode), ceiling = MODES.indexOf(max);
  return wanted >= 0 && ceiling >= 0 && wanted > ceiling;
};

/** "Supervised by Hermes" at level manage (the default), "Watched by Hermes" at level watch. */
export const supervisionText = (name: string, level?: string | null): string => `${level === 'watch' ? 'Watched' : 'Supervised'} by ${orchestratorLabel(name)}`;

/** The sidebar badge: short, with the full sentence (and level) as its tooltip. */
export function supervisorBadge(name: string | null | undefined, escape: (text: string) => string, level?: string | null): string {
  if (!name) return '';
  const label = orchestratorLabel(name), text = escape(supervisionText(name, level));
  return `<span class="task-supervisor" data-level="${level === 'watch' ? 'watch' : 'manage'}" role="img" aria-label="${text}" title="${text}">${escape(label)}</span>`;
}
