/* How the window names the orchestrator that supervises tasks (Hermes). */

/** "hermes" is shown as "Hermes". */
export const orchestratorLabel = (name: string): string => name ? name.charAt(0).toUpperCase() + name.slice(1) : name;

/** The sidebar badge: short, with the full sentence as its tooltip. */
export function supervisorBadge(name: string | null | undefined, escape: (text: string) => string): string {
  if (!name) return '';
  const label = orchestratorLabel(name);
  return `<span class="task-supervisor" role="img" aria-label="Supervised by ${escape(label)}" title="Supervised by ${escape(label)}">${escape(label)}</span>`;
}
