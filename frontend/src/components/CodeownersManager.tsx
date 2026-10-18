import React, { useState, useEffect, useLayoutEffect, useRef } from 'react';
import PlainFileEditor from './PlainFileEditor';
import WorkflowStatusBadge from './WorkflowStatusBadge';
import { UnlockOverlay, UnlockUnderReviewDialog } from './UnlockUnderReview';
import { updateWorkflows } from '../api/workflows';
import { assertPushed } from '../utils/workflowOperations';
import { getCodeowners, saveProjectCodeowners, CodeownersRecord } from '../api/codeowners';
import type { CodeownersDriftDetail } from '../api/drift';
import { FileDriftBadge, FileDriftBanner, driftTargetLabel } from './FileDriftBanner';

interface CodeownersManagerProps {
  readonly user: string;
  readonly projectName: string;
  readonly projectId: number;
  /** The project's repositories: every one receives this file, and any can be imported from. */
  readonly selectedRepos: string[];
  readonly isReadOnly?: boolean;
  /** The project's CODEOWNERS file, owned by the page; null when it has none. */
  readonly codeowners: CodeownersRecord | null;
  /** Each (repo, branch) where the last drift check found the file changed on GitHub. */
  readonly drift?: CodeownersDriftDetail[];
  /** Called with what the server saved and the project's resulting pr_state. */
  readonly onSaved?: (record: CodeownersRecord, prState?: string) => void;
  /** The repositories whose pull request Commit and Update PR reached. */
  readonly onReposDelivered?: (repos: string[]) => void;
}

const DEFAULT_PATH = '.github/CODEOWNERS';

const FILE_PATH_OPTIONS = [
  { value: '.github/CODEOWNERS', label: '.github/CODEOWNERS (recommended)' },
  { value: 'CODEOWNERS', label: 'CODEOWNERS (repository root)' },
];

const STATUS_LABELS: Record<string, string> = {
  new: 'New',
  committed_locally: 'Draft changes',
  under_review: 'PR open',
  synced_with_github: 'Synced',
};

const NO_CHANGES = 'No changes to save';

const errorDetail = (err: unknown, fallback: string): string => {
  const e = err as { response?: { data?: { detail?: string } }; message?: string };
  return e?.response?.data?.detail ?? e?.message ?? fallback;
};

interface PrimaryActionProps {
  readonly unlockedPR: boolean;
  readonly locked: boolean;
  readonly saving: boolean;
  readonly disabled: boolean;
  readonly isDirty: boolean;
  readonly onCommitLocally: () => void;
  readonly onCommitAndUpdatePR: () => void;
}

// Commit and Update PR only once a file under review is unlocked; otherwise Commit Locally, greyed while locked.
const PrimaryAction: React.FC<PrimaryActionProps> = ({ unlockedPR, locked, saving, disabled, isDirty, onCommitLocally, onCommitAndUpdatePR }) => {
  if (unlockedPR) {
    return (
      <button type="button" onClick={onCommitAndUpdatePR} disabled={disabled} className="btn btn-primary" data-testid="update-pr-button">
        {saving ? 'Saving…' : '🔄 Commit and Update PR'}
      </button>
    );
  }
  let title: string | undefined;
  if (locked) title = 'Locked while its pull request is open. Unlock the file below to edit it.';
  else if (!isDirty) title = 'No unsaved changes';
  return (
    <button type="button" onClick={onCommitLocally} disabled={disabled} title={title} className="btn btn-primary">
      {saving ? 'Saving…' : '💾 Commit Locally'}
    </button>
  );
};

const CodeownersManager: React.FC<CodeownersManagerProps> = ({
  user,
  projectName,
  projectId,
  selectedRepos,
  isReadOnly = false,
  codeowners,
  drift = [],
  onSaved,
  onReposDelivered,
}) => {
  const [content, setContent] = useState<string>(codeowners?.content ?? '');
  const [filePath, setFilePath] = useState<string>(codeowners?.file_path ?? DEFAULT_PATH);
  const [isDirty, setIsDirty] = useState<boolean>(false);
  const [saving, setSaving] = useState<boolean>(false);
  // Which GitHub copy is loading into the editor, if any.
  const [loadingFrom, setLoadingFrom] = useState<string | null>(null);
  const [importRepo, setImportRepo] = useState<string>(selectedRepos[0] ?? '');
  const [warnings, setWarnings] = useState<string[]>([]);
  const [message, setMessage] = useState<{ text: string; type: 'success' | 'error' } | null>(null);
  const messageTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Read when the page hands over a new copy of the file: synced before that
  // effect runs, which a passive effect would not be.
  const isDirtyRef = useRef(isDirty);
  useLayoutEffect(() => { isDirtyRef.current = isDirty; }, [isDirty]);

  const isUnderReview = codeowners?.status === 'under_review';
  // Unlocked for editing while its PR is open; locked again once the status moves.
  const [unlocked, setUnlocked] = useState(false);
  const [confirmingUnlock, setConfirmingUnlock] = useState(false);
  useEffect(() => { setUnlocked(false); }, [codeowners?.status]);
  const isLocked = isUnderReview && !unlocked;

  // The editor follows the page's copy, keeping an unsaved edit unless the
  // file went under review meanwhile: it locks, showing what the PR carries.
  useEffect(() => {
    if (isDirtyRef.current && codeowners?.status !== 'under_review') return;
    setContent(codeowners?.content ?? '');
    setFilePath(codeowners?.file_path ?? DEFAULT_PATH);
    setIsDirty(false);
  }, [codeowners]);

  useEffect(() => {
    return () => {
      if (messageTimeoutRef.current) clearTimeout(messageTimeoutRef.current);
    };
  }, []);

  const showMessage = (text: string, type: 'success' | 'error') => {
    setMessage({ text, type });
    if (messageTimeoutRef.current) clearTimeout(messageTimeoutRef.current);
    messageTimeoutRef.current = setTimeout(() => setMessage(null), 5000);
  };

  const edit = (nextContent: string, nextPath: string) => {
    setContent(nextContent);
    setFilePath(nextPath);
    setIsDirty(true);
  };

  const loadFromGitHub = async (repo: string, key: string, options: { branch?: string; path?: string } = {}) => {
    setLoadingFrom(key);
    try {
      const data = await getCodeowners(repo, user, projectName, options);
      if (!data.github.exists) {
        showMessage(`${key} has no CODEOWNERS file`, 'error');
        return;
      }
      edit(data.github.content ?? '', data.github.path ?? filePath);
      showMessage(`Loaded the CODEOWNERS file from ${key}. Commit Locally to keep it.`, 'success');
    } catch (err) {
      showMessage(errorDetail(err, 'Failed to load CODEOWNERS from GitHub'), 'error');
    } finally {
      setLoadingFrom(null);
    }
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      const result = await saveProjectCodeowners(projectId, content, filePath);
      setWarnings(result.validation_warnings ?? []);
      // Before the page hands the saved copy back, so the editor takes it.
      isDirtyRef.current = false;
      setIsDirty(false);
      showMessage(
        result.message === NO_CHANGES
          ? NO_CHANGES
          : 'CODEOWNERS saved locally. Include it in a PR campaign to deliver it to every repository.',
        'success'
      );
      onSaved?.(result.codeowners, result.pr_state);
    } catch (err) {
      showMessage(errorDetail(err, 'Failed to save CODEOWNERS'), 'error');
    } finally {
      setSaving(false);
    }
  };

  // Saves, then adds the save to the open pull request; a failed push leaves it committed locally.
  const handleCommitAndUpdatePR = async () => {
    setSaving(true);
    try {
      const result = await saveProjectCodeowners(projectId, content, filePath);
      setWarnings(result.validation_warnings ?? []);
      isDirtyRef.current = false;
      setIsDirty(false);
      try {
        assertPushed(
          await updateWorkflows(projectName, selectedRepos, { includeCodeowners: true }),
          onReposDelivered
        );
      } catch (err) {
        showMessage(`CODEOWNERS committed locally, but its pull request was not updated: ${errorDetail(err, 'push failed')}`, 'error');
        onSaved?.(result.codeowners, result.pr_state);
        return;
      }
      setUnlocked(false);
      showMessage('CODEOWNERS committed and pull request updated.', 'success');
      onSaved?.({ ...result.codeowners, status: 'under_review' }, result.pr_state);
    } catch (err) {
      showMessage(errorDetail(err, 'Failed to save CODEOWNERS'), 'error');
    } finally {
      setSaving(false);
    }
  };

  const handleRevert = () => {
    setContent(codeowners?.content ?? '');
    setFilePath(codeowners?.file_path ?? DEFAULT_PATH);
    setIsDirty(false);
    setWarnings([]);
  };

  if (selectedRepos.length === 0) {
    return (
      <div className="rounded-md border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-900/20 dark:text-amber-200">
        📭 Select one or more repositories in <strong>Repositories &amp; Branches</strong> to manage the project's CODEOWNERS file.
      </div>
    );
  }

  const canEdit = !isReadOnly && !isLocked;
  const importFrom = selectedRepos.includes(importRepo) ? importRepo : selectedRepos[0];
  const editorLabel = (
    // Descriptive text, not a form label — PlainFileEditor is not a native control, labeled via aria-label instead.
    <div className="mb-1 block text-sm font-medium text-slate-700 dark:text-slate-300">
      CODEOWNERS content
    </div>
  );

  return (
    <div className="space-y-4">
      <p className="text-sm text-slate-600 dark:text-slate-400">
        One CODEOWNERS file for the project, delivered to all {selectedRepos.length}{' '}
        {selectedRepos.length === 1 ? 'repository' : 'repositories'} by its PR campaigns.
      </p>

      {/* Status badges + action buttons in one row */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          {drift.length > 0 && <FileDriftBadge drift={drift} />}
          {codeowners && (
            <WorkflowStatusBadge status={codeowners.status} label={STATUS_LABELS[codeowners.status] ?? codeowners.status} />
          )}
          {isDirty && (
            <span className="rounded-full bg-yellow-100 px-2.5 py-0.5 text-xs font-medium text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-300">
              Unsaved changes
            </span>
          )}
        </div>
        <div className="flex flex-wrap gap-2">
          {isDirty && (
            <button type="button" onClick={handleRevert} disabled={saving} className="btn btn-secondary">
              Revert
            </button>
          )}
          <PrimaryAction
            unlockedPR={isUnderReview && unlocked}
            locked={isLocked}
            saving={saving}
            disabled={saving || loadingFrom !== null || !canEdit || !isDirty}
            isDirty={isDirty}
            onCommitLocally={handleSave}
            onCommitAndUpdatePR={handleCommitAndUpdatePR}
          />
        </div>
      </div>

      {/* File path selector */}
      <div className="flex flex-wrap items-center gap-2">
        <label htmlFor="codeowners-path-select" className="text-sm font-medium text-slate-700 dark:text-slate-300">
          File path:
        </label>
        <select
          id="codeowners-path-select"
          value={filePath}
          onChange={(e) => edit(content, e.target.value)}
          disabled={!canEdit}
          className="rounded border border-slate-300 bg-white px-2 py-1 text-sm dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100 disabled:opacity-60"
        >
          {FILE_PATH_OPTIONS.map(o => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
      </div>

      {!codeowners && !isReadOnly && (
        <div
          className="flex flex-wrap items-center gap-2 rounded-md border border-slate-200 bg-slate-50 p-3 text-sm text-slate-700 dark:border-slate-700 dark:bg-slate-800/50 dark:text-slate-300"
          data-testid="codeowners-import"
        >
          <span>No CODEOWNERS file yet. Write one below, or start from a repository's:</span>
          <select
            aria-label="Repository to import CODEOWNERS from"
            value={importFrom}
            onChange={(e) => setImportRepo(e.target.value)}
            className="rounded border border-slate-300 bg-white px-2 py-1 text-sm dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100"
          >
            {selectedRepos.map(repo => <option key={repo} value={repo}>{repo}</option>)}
          </select>
          <button
            type="button"
            onClick={() => loadFromGitHub(importFrom, importFrom)}
            disabled={loadingFrom !== null}
            className="btn btn-secondary"
          >
            {loadingFrom === importFrom ? 'Loading…' : 'Load from repository'}
          </button>
        </div>
      )}

      {drift.length > 0 && (
        <FileDriftBanner
          drift={drift}
          subject="the CODEOWNERS file"
          restoreHint="include CODEOWNERS in a PR campaign"
          canLoad={canEdit}
          loadingFrom={loadingFrom}
          onLoad={(d) => loadFromGitHub(d.repo, driftTargetLabel(d), { branch: d.branch, path: d.file_path })}
          testId="codeowners-drift-banner"
        />
      )}

      {/* Editor — locked while under review, until unlocked */}
      {isLocked ? (
        <div>
          {editorLabel}
          <div className="workflow-editor-content workflow-editor-content--lockable relative h-[400px] overflow-hidden rounded-md">
            {!isReadOnly && (
              <UnlockOverlay
                title="CODEOWNERS has an open pull request. Click to unlock editing."
                onRequestUnlock={() => setConfirmingUnlock(true)}
                buttonTestId="unlock-codeowners-button"
              />
            )}
            <PlainFileEditor value={content} language="plain" readOnly height="400px" theme="dark" ariaLabel="CODEOWNERS content" />
          </div>
        </div>
      ) : (
        <div>
          {editorLabel}
          <PlainFileEditor
            value={content}
            onChange={(v) => edit(v, filePath)}
            language="plain"
            readOnly={saving || loadingFrom !== null || isReadOnly}
            height="400px"
            theme="dark"
            ariaLabel="CODEOWNERS content"
          />
        </div>
      )}

      {/* Validation warnings */}
      {warnings.length > 0 && (
        <div className="rounded-md border border-yellow-300 bg-yellow-50 p-3 text-sm dark:border-yellow-700 dark:bg-yellow-900/20">
          <p className="mb-1 font-medium text-yellow-900 dark:text-yellow-200">Validation warnings</p>
          <ul className="list-inside list-disc space-y-0.5 text-yellow-800 dark:text-yellow-200">
            {warnings.slice(0, 8).map((w, idx) => (
              <li key={`warn-${idx}`}>{w}</li>
            ))}
            {warnings.length > 8 && <li>…and {warnings.length - 8} more</li>}
          </ul>
        </div>
      )}

      {/* Status banner */}
      {message && (
        <div
          className={`rounded-md border p-3 text-sm ${
            message.type === 'success'
              ? 'border-green-300 bg-green-50 text-green-900 dark:border-green-700 dark:bg-green-900/20 dark:text-green-200'
              : 'border-red-300 bg-red-50 text-red-900 dark:border-red-700 dark:bg-red-900/20 dark:text-red-200'
          }`}
        >
          {message.text}
        </div>
      )}

      {/* Helper text */}
      <div className="rounded-md border border-slate-200 bg-slate-50 p-3 text-sm text-slate-700 dark:border-slate-700 dark:bg-slate-800/50 dark:text-slate-300">
        <p className="mb-1 font-medium">About CODEOWNERS</p>
        <ul className="list-inside list-disc space-y-0.5">
          <li>Each line is a glob followed by one or more <code>@user</code>, <code>@org/team</code>, or email-address owners.</li>
          <li>Lines starting with <code>#</code> are comments.</li>
          <li>The last matching line wins, so put more-specific rules at the bottom.</li>
          <li>
            See the official{' '}
            <a
              href="https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners"
              target="_blank"
              rel="noreferrer"
              className="text-blue-600 underline hover:text-blue-800 dark:text-blue-400"
            >
              GitHub CODEOWNERS docs
            </a>{' '}
            for the full syntax.
          </li>
        </ul>
      </div>

      {confirmingUnlock && (
        <UnlockUnderReviewDialog
          subject="CODEOWNERS file"
          onCancel={() => setConfirmingUnlock(false)}
          onUnlock={() => { setUnlocked(true); setConfirmingUnlock(false); }}
        />
      )}
    </div>
  );
};

export default CodeownersManager;
