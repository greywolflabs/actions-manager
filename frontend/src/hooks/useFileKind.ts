import { useCallback, useEffect, useState } from "react";
import { fetchManagedFiles, type ManagedFileKind } from "../api/managedFiles";

export interface FileKindState {
  /** The server's definition of the kind: its label and paths. Null while it loads, or when it failed. */
  definition: ManagedFileKind | null;
  /** The definition could not be read, or the server defines no such kind. */
  failed: boolean;
  /** Reads the definition again. */
  retry: () => void;
}

/** The server's definition of *kind*, for a Managed Files project; nothing for a null kind. */
export function useFileKind(kind: string | null): FileKindState {
  const [definition, setDefinition] = useState<ManagedFileKind | null>(null);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const retry = useCallback(() => setAttempt((current) => current + 1), []);

  useEffect(() => {
    setDefinition(null);
    setFailed(false);
    if (!kind) return;
    let cancelled = false;
    fetchManagedFiles()
      .then((section) => {
        if (cancelled) return;
        const found = section.kinds.find((entry) => entry.kind === kind) ?? null;
        setDefinition(found);
        setFailed(found === null);
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        console.error("❌ Could not load the Managed Files kinds:", error);
        setFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [kind, attempt]);

  return { definition, failed, retry };
}
