import { normalizeWorkflowStem } from './workflowFilename';

/** A workflow a save gave the other kind: its trigger gained or lost workflow_call (#2318). */
export interface WorkflowKindChange {
  name: string;
  is_reusable: boolean;
  workflow_status?: string;
}

interface ListedWorkflow {
  name: string;
  isReusable?: boolean;
  isModified?: boolean;
  savedName?: string;
  workflowStatus?: string;
}

export interface WorkflowKindMoves<T extends ListedWorkflow> {
  nextRegular: (prev: T[]) => T[];
  nextReusable: (prev: T[]) => T[];
  selectedId: string | null;
}

const nameKey = (name?: string): string => normalizeWorkflowStem(name ?? '').toLowerCase();

const without = <T extends ListedWorkflow>(list: T[], keys: Set<string>): T[] =>
  list.filter(w => !keys.has(nameKey(w.name)));

/**
 * The editor selects workflows by position ("regular-2"), so moving one between
 * sections shifts the rest. Resolve the selected workflow by name instead, so
 * the editor stays on the workflow it was showing.
 */
function followSelection<T extends ListedWorkflow>(
  selectedId: string | null,
  before: Record<'regular' | 'reusable', T[]>,
  after: Record<'regular' | 'reusable', T[]>,
): string | null {
  const match = /^(regular|reusable)-(\d+)$/.exec(selectedId ?? '');
  if (!match) return selectedId;
  const type = match[1] as 'regular' | 'reusable';
  const selected = before[type][Number(match[2])];
  if (!selected) return selectedId;
  const key = nameKey(selected.name);
  const other = type === 'regular' ? 'reusable' : 'regular';
  for (const candidate of [type, other] as const) {
    const index = after[candidate].findIndex(w => nameKey(w.name) === key);
    if (index !== -1) return `${candidate}-${index}`;
  }
  return null;
}

/**
 * Moves each workflow a save re-kinded to the other section, marked saved with
 * the status the server reported. The list updaters are functional and keyed by
 * name, so they apply on top of any update still queued from the same save,
 * such as the "saved" marks for the workflows that stayed put. With
 * `markSaved` off the workflows only change sections: a push that follows the
 * save still has to see them as modified.
 */
export function planWorkflowKindMoves<T extends ListedWorkflow>(
  regular: T[],
  reusable: T[],
  changes: WorkflowKindChange[],
  selectedId: string | null,
  markSaved = true,
): WorkflowKindMoves<T> | null {
  const statusByKey = new Map(changes.map(c => [nameKey(c.name), c.workflow_status]));
  const wanted = (isReusable: boolean) =>
    new Set(changes.filter(c => c.is_reusable === isReusable).map(c => nameKey(c.name)));
  const moved = (w: T, isReusable: boolean): T => markSaved
    ? {
        ...w,
        isReusable,
        isModified: false,
        savedName: w.name,
        workflowStatus: statusByKey.get(nameKey(w.name)) ?? w.workflowStatus,
      }
    : { ...w, isReusable };

  const toReusableKeys = wanted(true);
  const toRegularKeys = wanted(false);
  const toReusable = regular.filter(w => toReusableKeys.has(nameKey(w.name))).map(w => moved(w, true));
  const toRegular = reusable.filter(w => toRegularKeys.has(nameKey(w.name))).map(w => moved(w, false));
  if (toReusable.length === 0 && toRegular.length === 0) return null;

  const leavingRegular = new Set(toReusable.map(w => nameKey(w.name)));
  const leavingReusable = new Set(toRegular.map(w => nameKey(w.name)));
  const nextRegular = (prev: T[]) => [...without(prev, leavingRegular), ...toRegular];
  const nextReusable = (prev: T[]) => [...without(prev, leavingReusable), ...toReusable];

  return {
    nextRegular,
    nextReusable,
    selectedId: followSelection(
      selectedId,
      { regular, reusable },
      { regular: nextRegular(regular), reusable: nextReusable(reusable) },
    ),
  };
}
