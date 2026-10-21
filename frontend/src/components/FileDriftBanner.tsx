import React from 'react';
import type { CodeownersDriftDetail } from '../api/drift';

/** One (repo, branch) where a file ActionsManager delivers whole — CODEOWNERS or a custom file — drifted. */
type FileDrift = CodeownersDriftDetail;

export const driftTargetLabel = (d: FileDrift) => `${d.repo} @ ${d.branch}`;

const driftVerb = (drift: FileDrift[]) => {
  const removed = drift.filter(d => d.deleted_in_github).length;
  if (removed === 0) return 'has changed';
  if (removed === drift.length) return 'was removed';
  return 'has changed or was removed';
};

/** "Drift detected", with each drifted (repo, branch) and what the check found there in its tooltip. */
export const FileDriftBadge: React.FC<{ readonly drift: FileDrift[] }> = ({ drift }) => (
  <span
    className="rounded-full bg-red-100 px-2.5 py-0.5 text-xs font-medium text-red-800 dark:bg-red-900/30 dark:text-red-300"
    title={drift.map(d => `${driftTargetLabel(d)}: ${d.message}`).join('\n')}
    data-testid="file-drift-badge"
  >
    Drift detected
  </span>
);

interface FileDriftBannerProps<T extends FileDrift> {
  readonly drift: T[];
  /** The file as the sentence names it, e.g. "the CODEOWNERS file". */
  readonly subject: string;
  /** How to put the project's version back, e.g. "include CODEOWNERS in a PR campaign". */
  readonly restoreHint: string;
  readonly canLoad: boolean;
  /** Which GitHub copy is loading into the editor, if any. */
  readonly loadingFrom: string | null;
  readonly onLoad: (detail: T) => void;
  /**
   * "Keep the ActionsManager version": a PR campaign carrying only this file
   * to the repositories it drifted in. Replaces the restore hint when given.
   */
  readonly onKeep?: () => void;
  readonly testId: string;
}

/** Names each (repo, branch) where the file drifted, offering to load every copy that still exists. */
export function FileDriftBanner<T extends FileDrift>({
  drift, subject, restoreHint, canLoad, loadingFrom, onLoad, onKeep, testId,
}: FileDriftBannerProps<T>) {
  const loadable = canLoad ? drift.filter(d => !d.deleted_in_github) : [];
  const keep = canLoad ? onKeep : undefined;
  return (
    <div
      className="rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-900 dark:border-red-700 dark:bg-red-900/20 dark:text-red-200"
      data-testid={testId}
    >
      <p>
        ⚠️ Drift detected — {subject} on GitHub {driftVerb(drift)} on{' '}
        {drift.map(driftTargetLabel).join(', ')} since ActionsManager last delivered it.
        {loadable.length > 0 && ' To keep the GitHub version, load it into the editor and Commit Locally.'}
        {keep
          ? ' To put the project’s version back, keep the ActionsManager version: a PR campaign carrying only this file opens for review.'
          : ` To put the project’s version back, ${restoreHint}.`}
      </p>
      {(loadable.length > 0 || keep) && (
        <div className="mt-2 flex flex-wrap gap-2">
          {keep && (
            <button
              type="button"
              onClick={keep}
              disabled={loadingFrom !== null}
              className="btn btn-primary"
              data-testid="keep-actionsmanager-version-button"
            >
              Keep ActionsManager version
            </button>
          )}
          {loadable.map(d => (
            <button
              key={driftTargetLabel(d)}
              type="button"
              onClick={() => onLoad(d)}
              disabled={loadingFrom !== null}
              className="btn btn-secondary"
            >
              {drift.length === 1 ? 'Load GitHub version' : `Load from ${driftTargetLabel(d)}`}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
