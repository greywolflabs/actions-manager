import { useEffect, useState } from "react";
import { fetchTakenRepos, type TakenRepo } from "../api/managedFiles";
import { apiErrorMessage } from "../utils/apiErrorMessage";

const NONE: ReadonlyMap<string, string> = new Map();

/** The label a taken repository's row shows. */
export const takenRepoLabel = (holder: TakenRepo): string =>
  holder.project_name ? `Used by ${holder.project_name}` : "Used by another project";

export interface TakenRepos {
  /** Repository name -> why it can't be picked, for the picker's unavailableRepos. */
  taken: ReadonlyMap<string, string>;
  error: string | null;
}

/**
 * Which of *repoNames* another Managed Files project of *kind* already holds.
 *
 * Asks again when the names, the kind or *refreshSignal* change, never when
 * the selection does: a selection is not saved yet, so it changes nothing the
 * server would answer. A parent that saves repositories bumps *refreshSignal*
 * once the save resolves.
 */
export function useTakenRepos(
  kind: string,
  repoNames: string[],
  excludeProjectId?: number,
  refreshSignal = 0,
): TakenRepos {
  const [result, setResult] = useState<TakenRepos>({ taken: NONE, error: null });
  const namesKey = repoNames.join("\n");

  useEffect(() => {
    if (!kind || !namesKey) {
      setResult({ taken: NONE, error: null });
      return;
    }
    let cancelled = false;
    fetchTakenRepos(kind, namesKey.split("\n"), excludeProjectId)
      .then((holders) => {
        if (cancelled) return;
        const taken = new Map(Object.entries(holders).map(([name, holder]) => [name, takenRepoLabel(holder)]));
        setResult({ taken, error: null });
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setResult({
          taken: NONE,
          error: apiErrorMessage(error, "Couldn't check which repositories other projects use."),
        });
      });
    return () => {
      cancelled = true;
    };
  }, [kind, namesKey, excludeProjectId, refreshSignal]);

  return result;
}
