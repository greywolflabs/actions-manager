/* eslint-disable no-restricted-syntax -- Diff view uses inline pre/grid styling */
/**
 * Drift detection panel: surfaces files changed on GitHub outside ActionsManager
 * (workflows, custom files, CODEOWNERS) and puts each right the same way.
 *
 * Renders:
 *  • A compact alert at the top of the project when drift exists
 *    ("3 workflows changed in GitHub" + Review Drift button).
 *  • A modal listing every drifted (file, repo, branch) with a side-by-side
 *    diff and the same two actions for each: keep the ActionsManager version
 *    (a PR campaign carrying only that file) or use the GitHub version.
 *
 * Drift state is also reported up via the optional onDriftLoaded callback
 * so callers can render per-file badges and gate destructive actions.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "./ui/dialog";
import { Button } from "./ui/button";
import { DiffColumns, diffGridStyle } from "./DiffColumns";
import {
  getProjectDrift,
  getWorkflowDrift,
  adoptGithubVersion,
  bulkResolveWorkflowDrift,
  type WorkflowDriftDetail,
  type AdoptResolutionMode,
  type ProjectDriftSummary,
  type CodeownersDriftDetail,
  type CustomFileDriftDetail,
} from "../api/drift";
import ConfirmDialog from "./ConfirmDialog";
import { deleteWorkflowFromGitHub } from "../api/workflows";
import { driftRows, fileGithubCopy, githubVersionConflict, type DriftRow } from "../utils/driftRows";
import { getDocsUrl } from "../help/helpLinks";

/**
 * Prefer the backend's safe FastAPI error detail over the raw axios message
 * (which is just "Request failed with status code 500"). Same pattern used
 * across the app (ProjectMgmt, CustomFiles, workflowOperations).
 */
const driftErrorMessage = (err: unknown, fallback: string): string => {
  const r = err as { response?: { data?: { detail?: string } }; message?: string };
  return r?.response?.data?.detail || r?.message || fallback;
};

/**
 * The status row for a project with no drift rows to list. Driven by the saved
 * drift status — the value the Projects list badge reads — so the two agree.
 * ✅ only when the server says clean: an empty list alone can also mean some
 * repositories could not be checked.
 */
const getDriftStatusRow = (
  driftStatus: ProjectDriftSummary["drift_status"],
  uncheckedCount: number,
  lastChecked: string | null,
): { icon: string; message: string } => {
  if (uncheckedCount > 0) {
    return {
      icon: "❔",
      message: `Couldn't check ${uncheckedCount} workflow${uncheckedCount === 1 ? "" : "s"} — GitHub didn't respond. Drift status may be out of date.`,
    };
  }
  if (driftStatus === "check_failed") {
    return { icon: "❔", message: "Couldn't check some workflows — drift status may be out of date." };
  }
  if (driftStatus === "drifted") {
    // Drift found only outside the project's own repos has no stored row.
    return { icon: "⚠️", message: "Drift detected on the last check — run Check Now to see the details." };
  }
  if (!lastChecked) {
    return { icon: "❔", message: "Not checked yet" };
  }
  const when = new Date(lastChecked).toLocaleString();
  if (driftStatus === "clean") {
    return { icon: "✅", message: `No drift detected — last checked ${when}` };
  }
  return { icon: "❔", message: `Drift status unknown — last checked ${when}` };
};

interface DriftDetectionProps {
  user: string;
  projectId: number | null;
  projectName: string;
  selectedRepos: string[];
  /**
   * Called whenever drift state is (re)loaded so the parent can render
   * per-file drift badges and decide whether destructive actions need
   * confirmation.
   */
  onDriftLoaded?: (
    details: WorkflowDriftDetail[],
    codeowners: CodeownersDriftDetail[],
    customFiles: CustomFileDriftDetail[],
  ) => void;
  /**
   * External signal – when this number changes, drift is re-checked.
   * Use this to refresh after a save/PR completes.
   */
  refreshSignal?: number;
  /**
   * Called whenever a resolve action changes a workflow's status server-side
   * (synced_with_github after using GitHub's version). loadDrift() only
   * refreshes this component's own drift list - the parent owns the
   * workflows/rxworkflows state that status badges elsewhere on the page
   * render from, so it needs this callback to patch that sibling state itself.
   */
  onWorkflowStatusesChanged?: (workflowNames: string[], status: string) => void;
  /**
   * Workflow names with drift persisted by the last check, from the project
   * fetch. Renders the banner on first paint so it doesn't pop in and shift
   * the layout once the live check resolves. Superseded by live data as soon
   * as the check completes.
   */
  seededDriftNames?: string[];
  /** Whether the last check found the project's CODEOWNERS drifted, from the project fetch, for the same reason. */
  seededCodeownersDrift?: boolean;
  /** How many custom files the last check found drifted, from the project fetch, for the same reason. */
  seededCustomFileDriftCount?: number;
  /** "Keep the ActionsManager version": opens a PR campaign carrying exactly these rows' files to their repositories. */
  onKeepActionsManagerVersion?: (rows: DriftRow[]) => void;
  /**
   * "Use the GitHub version" of custom files and CODEOWNERS: the parent saves
   * each row's GitHub copy as the project's file, as Commit Locally would, and
   * says what it did. Workflows are adopted here, through their own modal.
   */
  onUseGithubVersionOfFiles?: (rows: DriftRow[]) => Promise<string>;
  /** The project's own content for a custom-file or CODEOWNERS row, the left side of its diff. */
  contentOf?: (row: DriftRow) => string | null;
  /** Called after an action here changed the project itself, e.g. a workflow marked for removal. */
  onProjectChanged?: () => void;
}

const countOf = (count: number, noun: string) => (count === 1 ? `1 ${noun}` : `${count} ${noun}s`);

/** "2 workflows changed in GitHub", naming custom files and CODEOWNERS when they drifted too. */
export const driftSummaryText = (workflowCount: number, codeownersDrifted: boolean, customFileCount = 0): string => {
  const parts = [
    workflowCount > 0 ? countOf(workflowCount, "workflow") : null,
    customFileCount > 0 ? countOf(customFileCount, "custom file") : null,
    codeownersDrifted ? "CODEOWNERS" : null,
  ].filter((part): part is string => part !== null);
  if (parts.length === 0) return "";
  const named = parts.length === 1 ? parts[0] : `${parts.slice(0, -1).join(", ")} and ${parts.at(-1)}`;
  return `${named} changed in GitHub`;
};

/** How many custom files drifted: from the live check once it has loaded, else the project fetch's seed. */
const driftedCustomFileCount = (liveLoaded: boolean, drift: CustomFileDriftDetail[], seeded: number) =>
  liveLoaded ? new Set(drift.map((d) => d.custom_file_id)).size : seeded;

const KIND_LABEL: Record<DriftRow["kind"], string> = {
  workflow: "Workflow",
  custom_file: "Custom file",
  codeowners: "CODEOWNERS",
};

/** What "Use GitHub version" does for this row's kind of file. */
const githubVersionHint = (row: DriftRow): string => (row.kind === "workflow"
  ? `Import the GitHub version from ${row.repo}: for this repository only, for the project, or for the project and its other repositories.`
  : `Make the GitHub version from ${row.repo} the project's ${row.name}, as a local change. Deliver it to the other repositories with a campaign afterwards.`);

interface RowActionsProps {
  readonly row: DriftRow;
  readonly busy: boolean;
  readonly onKeep: () => void;
  readonly onUseGithub: () => void;
}

/** The two ways to put a drifted file right, the same for every kind. */
const RowActions: React.FC<RowActionsProps> = ({ row, busy, onKeep, onUseGithub }) => (
  <>
    <div className="border-t border-r border-slate-200 dark:border-slate-700 px-2 py-3 bg-slate-50/50 dark:bg-slate-800/50 flex flex-col gap-1">
      <Button size="sm" variant="default" disabled={busy} onClick={onKeep} className="w-full justify-start"
        data-testid="keep-actionsmanager-button">
        Keep ActionsManager Version
      </Button>
      <p className="text-xs text-slate-600 dark:text-slate-400 mt-1 whitespace-normal">
        Opens a PR campaign for <strong>{row.repo}</strong> carrying only this file, to review before
        the pull requests open.{" "}
        <span className="px-1.5 py-0.5 text-[0.65rem] rounded-full bg-emerald-100 text-emerald-800 dark:bg-emerald-900 dark:text-emerald-200 align-middle">
          Recommended
        </span>
      </p>
    </div>
    <div className="border-t border-slate-200 dark:border-slate-700 px-2 py-3 bg-slate-50/50 dark:bg-slate-800/50 flex flex-col gap-1">
      <Button size="sm" variant="secondary" disabled={busy} onClick={onUseGithub} className="w-full justify-start"
        data-testid="adopt-github-version-button">
        Use GitHub Version
      </Button>
      <p className="text-xs text-slate-600 dark:text-slate-400 mt-1 whitespace-normal">{githubVersionHint(row)}</p>
    </div>
  </>
);

interface DeletedInGithubPanelProps {
  readonly row: DriftRow;
  readonly busy: boolean;
  readonly onKeep: () => void;
  /** Workflows only: accept the deletion everywhere. */
  readonly onDeleteEverywhere?: () => void;
}

/**
 * Shown instead of a diff when the file no longer exists in GitHub.
 *
 * A side-by-side diff renders the missing side as a single blank line, which
 * reads as "the file is empty" rather than "the file is gone". Using GitHub's
 * version is impossible here — there is none — so the actions are putting the
 * file back, or (for a workflow) accepting the deletion everywhere.
 */
const DeletedInGithubPanel: React.FC<DeletedInGithubPanelProps> = ({ row, busy, onKeep, onDeleteEverywhere }) => (
  <div className="rounded-md border border-red-200 bg-red-50 p-4 dark:border-red-800 dark:bg-red-900/20"
    data-testid="deleted-in-github-panel">
    <p className="text-sm font-medium text-red-900 dark:text-red-200">
      This file no longer exists in {row.repo}
    </p>
    <p className="mt-1 text-xs text-red-800 dark:text-red-300">
      It was removed from GitHub outside ActionsManager. ActionsManager still manages it, so it
      keeps being reported as drifted until you put it back{onDeleteEverywhere ? " or remove it here too" : ""}.
    </p>
    <div className="mt-4 flex flex-col gap-3 sm:flex-row">
      <div className="flex-1">
        <Button size="sm" variant="default" disabled={busy} onClick={onKeep} className="w-full justify-start"
          data-testid="keep-actionsmanager-button">
          Keep ActionsManager Version
        </Button>
        <p className="mt-1 text-xs text-slate-600 dark:text-slate-400">
          Opens a PR campaign for <strong>{row.repo}</strong> that adds the file back.
        </p>
      </div>
      {onDeleteEverywhere && (
        <div className="flex-1">
          <Button size="sm" variant="destructive" disabled={busy} onClick={onDeleteEverywhere}
            className="w-full justify-start" data-testid="delete-everywhere-button">
            Delete Everywhere
          </Button>
          <p className="mt-1 text-xs text-slate-600 dark:text-slate-400">
            Accepts the deletion: opens pull requests removing this workflow from the other
            repositories. It leaves ActionsManager when they merge.
          </p>
        </div>
      )}
    </div>
  </div>
);

interface DriftRowDetailProps {
  readonly row: DriftRow;
  readonly managed: string;
  readonly github: string;
  readonly busy: boolean;
  readonly onKeep: () => void;
  readonly onUseGithub: () => void;
  readonly onDeleteEverywhere: () => void;
}

/** An opened row: where it drifted, and the diff with its two actions, or the deleted panel. */
const DriftRowDetail: React.FC<DriftRowDetailProps> = ({
  row, managed, github, busy, onKeep, onUseGithub, onDeleteEverywhere,
}) => (
  <div className="space-y-2">
    <p className="text-sm text-slate-700 dark:text-slate-300">
      Repository: <strong>{row.repo}</strong>
      {" · "}Target branch: <strong>{row.branch}</strong>
    </p>
    {row.message && <p className="text-xs text-slate-500 dark:text-slate-400">{row.message}</p>}
    {row.deleted ? (
      <DeletedInGithubPanel row={row} busy={busy} onKeep={onKeep}
        onDeleteEverywhere={row.kind === "workflow" ? onDeleteEverywhere : undefined} />
    ) : (
      <div className="border border-slate-200 dark:border-slate-700 rounded-md overflow-hidden" style={diffGridStyle}>
        <DiffColumns left={managed} right={github}
          leftLabel="ActionsManager managed version" rightLabel="Current GitHub version" />
        <RowActions row={row} busy={busy} onKeep={onKeep} onUseGithub={onUseGithub} />
      </div>
    )}
  </div>
);

const StatusBadge: React.FC<{ readonly deleted: boolean }> = ({ deleted }) => (deleted ? (
  <span className="inline-block px-2 py-0.5 rounded-full text-xs font-medium bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200"
    data-testid="drift-status-deleted">
    Deleted in GitHub
  </span>
) : (
  <span className="inline-block px-2 py-0.5 rounded-full text-xs font-medium bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-200">
    Drift detected
  </span>
));

/** The badges beside a row's name: its kind for a file, sharing and override for a workflow. */
const RowBadges: React.FC<{ readonly row: DriftRow }> = ({ row }) => (
  <>
    {row.kind !== "workflow" && (
      <span className="px-2 py-0.5 text-xs rounded-full bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300"
        data-testid="drift-kind-badge">
        {KIND_LABEL[row.kind]}
      </span>
    )}
    {row.workflow?.is_shared_workflow && (
      <span className="px-2 py-0.5 text-xs rounded-full bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200"
        data-testid="shared-workflow-badge">
        Shared Workflow
      </span>
    )}
    {row.workflow?.has_repo_override && (
      <span className="px-2 py-0.5 text-xs rounded-full bg-purple-100 text-purple-800 dark:bg-purple-900 dark:text-purple-200"
        data-testid="repo-override-badge"
        title="This repository uses a workflow override and is no longer following the shared project workflow for this file.">
        Repo Override
      </span>
    )}
  </>
);

interface DriftTableRowProps {
  readonly row: DriftRow;
  readonly selected: boolean;
  readonly groupSize: number;
  readonly open: boolean;
  readonly loading: boolean;
  readonly onToggleSelected: () => void;
  readonly onSelectGroup: () => void;
  readonly onToggleDiff: () => void;
}

const DriftTableRow: React.FC<DriftTableRowProps> = ({
  row, selected, groupSize, open, loading, onToggleSelected, onSelectGroup, onToggleDiff,
}) => {
  const diffLabel = open ? "Hide Diff" : "View Diff";
  return (
    <tr className="border-b border-slate-100 dark:border-slate-800">
      <td className="py-2 pr-2">
        <input type="checkbox" data-testid={`select-drift-${row.key}`} checked={selected} onChange={onToggleSelected}
          aria-label={`Select ${row.name} in ${row.repo} on ${row.branch}`} />
      </td>
      <td className="py-2 pr-4 font-medium">
        <div className="flex flex-wrap items-center gap-2">
          <span className={row.kind === "workflow" ? undefined : "font-mono"}>{row.name}</span>
          {groupSize > 1 && (
            <button type="button" className="text-xs text-blue-600 hover:underline dark:text-blue-400"
              data-testid={`select-group-${row.key}`} onClick={onSelectGroup}>
              {groupSize} identical — select all
            </button>
          )}
          <RowBadges row={row} />
        </div>
      </td>
      <td className="py-2 pr-4">{row.repo}</td>
      <td className="py-2 pr-4">{row.branch}</td>
      <td className="py-2 pr-4"><StatusBadge deleted={row.deleted} /></td>
      <td className="py-2 pr-4 text-xs text-slate-500 dark:text-slate-400">
        {row.lastChecked ? new Date(row.lastChecked).toLocaleString() : "—"}
      </td>
      <td className="py-2">
        <Button variant="outline" size="sm" onClick={onToggleDiff} disabled={loading}>
          {loading ? "Loading…" : diffLabel}
        </Button>
      </td>
    </tr>
  );
};

interface SelectionBarProps {
  readonly total: number;
  readonly selected: DriftRow[];
  readonly busy: boolean;
  readonly onSelectAll: () => void;
  readonly onDeselectAll: () => void;
  readonly onKeep: () => void;
  readonly onUseGithub: () => void;
}

/** Selecting rows and putting them right together, the same two actions as one row's. */
const SelectionBar: React.FC<SelectionBarProps> = ({ total, selected, busy, onSelectAll, onDeselectAll, onKeep, onUseGithub }) => {
  const conflict = githubVersionConflict(selected);
  return (
    <div className="flex flex-wrap items-center gap-3 px-1" data-testid="drift-selection-bar">
      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          data-testid="select-all-drifts"
          checked={selected.length > 0 && selected.length === total}
          ref={(el) => {
            if (el) el.indeterminate = selected.length > 0 && selected.length < total;
          }}
          onChange={(e) => (e.target.checked ? onSelectAll() : onDeselectAll())}
        />
        <span className="text-slate-600 dark:text-slate-400">{selected.length} of {total} selected</span>
      </label>
      <Button variant="outline" size="sm" onClick={onSelectAll}>Select All</Button>
      <Button variant="outline" size="sm" onClick={onDeselectAll}>Deselect All</Button>
      {selected.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 ml-auto" data-testid="bulk-action-toolbar">
          <Button variant="outline" size="sm" disabled={busy} onClick={onKeep} data-testid="bulk-keep-button">
            {`Keep ActionsManager Version (${selected.length})`}
          </Button>
          <Button variant="outline" size="sm" disabled={busy || conflict !== null} title={conflict ?? undefined}
            onClick={onUseGithub} data-testid="bulk-use-github-button">
            {busy ? "Working…" : `Use GitHub Version (${selected.length})`}
          </Button>
        </div>
      )}
    </div>
  );
};

/**
 * Scope-aware drift resolution modal for a workflow: lets the user choose
 * between adopting the GitHub version for the whole project (and opening pull
 * requests to the other repos), adopting locally only, or creating a per-repo
 * override so this repo can intentionally diverge.
 */
const AdoptGithubVersionModal: React.FC<{
  open: boolean;
  onClose: () => void;
  detail: WorkflowDriftDetail | null;
  user: string;
  onResolved: (message: string) => void;
}> = ({ open, onClose, detail, user, onResolved }) => {
  const [mode, setMode] = useState<AdoptResolutionMode>("adopt_project_and_sync");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setMode("adopt_project_and_sync");
      setSubmitting(false);
      setError(null);
    }
  }, [open]);

  if (!detail) return null;
  const projectId = detail.project_id ?? null;
  const repoId = detail.repo_id ?? null;
  const affected = detail.affected_repos ?? [];
  const isShared = !!detail.is_shared_workflow;

  const handleConfirm = async () => {
    if (!projectId || !detail.workflow_id) {
      setError("Missing project/workflow context for this drift.");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const response = await adoptGithubVersion({
        github_user: user,
        project_id: projectId,
        workflow_id: detail.workflow_id,
        repo_id: repoId ?? undefined,
        repo_name: detail.repo,
        branch: detail.branch,
        resolution_mode: mode,
      });
      // Branch on the structured response: keep modal open on partial-failure
      // so the user sees what happened and can retry or pick a different mode.
      if (response.success === false) {
        setError(response.message || "Adoption did not fully succeed.");
        return;
      }
      onResolved(response.message || "GitHub version adopted.");
      onClose();
    } catch (err: unknown) {
      setError(driftErrorMessage(err, "Failed to adopt GitHub version"));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(v) => { if (!v) onClose(); }}>
      <DialogContent className="max-w-2xl" data-testid="adopt-github-version-modal">
        <DialogHeader>
          <DialogTitle>Use GitHub Version</DialogTitle>
          <DialogDescription>
            {isShared ? (
              <>
                This workflow is shared across multiple repositories in this project.
                The GitHub version from <strong>{detail.repo}</strong> is different from
                the ActionsManager version. Choose how this change should be handled.
              </>
            ) : (
              <>
                The GitHub version of <strong>{detail.workflow_filename}</strong> in
                {" "}<strong>{detail.repo}</strong> differs from the ActionsManager
                version. Choose how this change should be handled.
              </>
            )}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          {/* Option 1: adopt_project_and_sync */}
          <label
            className={`block p-3 rounded-lg border cursor-pointer ${
              mode === "adopt_project_and_sync"
                ? "border-emerald-500 bg-emerald-50 dark:bg-emerald-900/20"
                : "border-slate-200 dark:border-slate-700 hover:border-slate-400"
            }`}
            data-testid="adopt-mode-project-and-sync"
            aria-label="Adopt for project and sync other repositories"
          >
            <div className="flex items-start gap-2">
              <input
                type="radio"
                name="adopt-mode"
                checked={mode === "adopt_project_and_sync"}
                onChange={() => setMode("adopt_project_and_sync")}
                className="mt-1"
              />
              <div className="flex-1">
                <div className="flex items-center gap-2">
                  <span className="font-medium">
                    Adopt for project and sync other repositories
                  </span>
                  <span className="px-2 py-0.5 text-xs rounded-full bg-emerald-100 text-emerald-800 dark:bg-emerald-900 dark:text-emerald-200">
                    Recommended
                  </span>
                </div>
                <p className="text-sm text-slate-600 dark:text-slate-400 mt-1">
                  Use the GitHub version from {detail.repo} as the new project workflow,
                  then open pull requests delivering it to {affected.length} other
                  {" "}repositor{affected.length === 1 ? "y" : "ies"} in this project.
                </p>
              </div>
            </div>
          </label>

          {/* Option 2: adopt_local_only */}
          <label
            className={`block p-3 rounded-lg border cursor-pointer ${
              mode === "adopt_local_only"
                ? "border-amber-500 bg-amber-50 dark:bg-amber-900/20"
                : "border-slate-200 dark:border-slate-700 hover:border-slate-400"
            }`}
            data-testid="adopt-mode-local-only"
            aria-label="Adopt locally only"
          >
            <div className="flex items-start gap-2">
              <input
                type="radio"
                name="adopt-mode"
                checked={mode === "adopt_local_only"}
                onChange={() => setMode("adopt_local_only")}
                className="mt-1"
              />
              <div className="flex-1">
                <span className="font-medium">Adopt locally only</span>
                <p className="text-sm text-slate-600 dark:text-slate-400 mt-1">
                  Update the ActionsManager draft using the GitHub version from
                  {" "}{detail.repo}, but do not sync the other repositories.
                  {" "}<strong>No GitHub change</strong> — review and deploy the updated
                  draft normally afterward.
                </p>
                {mode === "adopt_local_only" && affected.length > 0 && (
                  <div className="mt-2 px-2 py-1 text-xs rounded bg-amber-100 text-amber-900 dark:bg-amber-900/40 dark:text-amber-200">
                    ⚠️ The other repositories keep the previous version until it is delivered to them.
                  </div>
                )}
              </div>
            </div>
          </label>

          {/* Option 3: create_repo_override */}
          <label
            className={`block p-3 rounded-lg border cursor-pointer ${
              mode === "create_repo_override"
                ? "border-purple-500 bg-purple-50 dark:bg-purple-900/20"
                : "border-slate-200 dark:border-slate-700 hover:border-slate-400"
            }`}
            data-testid="adopt-mode-repo-override"
            aria-label="Create repo-specific override"
          >
            <div className="flex items-start gap-2">
              <input
                type="radio"
                name="adopt-mode"
                checked={mode === "create_repo_override"}
                onChange={() => setMode("create_repo_override")}
                className="mt-1"
              />
              <div className="flex-1">
                <div className="flex items-center gap-2">
                  <span className="font-medium">Create repo-specific override</span>
                  <span className="px-2 py-0.5 text-xs rounded-full bg-purple-100 text-purple-800 dark:bg-purple-900 dark:text-purple-200">
                    Repo Override
                  </span>
                </div>
                <p className="text-sm text-slate-600 dark:text-slate-400 mt-1">
                  Pin {detail.repo} to its GitHub version as a repo-specific workflow
                  override. <strong>No GitHub change</strong> — the shared project
                  workflow and the other repositories are left untouched.
                </p>
              </div>
            </div>
          </label>
        </div>

        {error && (
          <div className="px-3 py-2 text-sm rounded bg-red-50 text-red-800 border border-red-200 dark:bg-red-900/20 dark:text-red-200 dark:border-red-800">
            {error}
          </div>
        )}

        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={submitting}>
            Cancel
          </Button>
          <Button
            variant="default"
            onClick={handleConfirm}
            disabled={submitting}
            data-testid="adopt-confirm-button"
          >
            {submitting ? "Working…" : "Confirm"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
};

interface DriftSummaryBarProps {
  readonly hasDrift: boolean;
  readonly statusRow: { icon: string; message: string };
  readonly uncheckedCount: number;
  readonly staleReason: string | null;
  readonly summaryText: string;
  readonly checking: boolean;
  readonly canReviewDrift: boolean;
  readonly onCheckNow: () => void;
  readonly onReview: () => void;
}

/**
 * The banner naming what drifted, or, with nothing to review, the status row.
 * The status row is always there for a clean or never-checked project, so it
 * always has a way to trigger a live check: Review Drift only exists once
 * drift is already known.
 */
const DriftSummaryBar: React.FC<DriftSummaryBarProps> = ({
  hasDrift, statusRow, uncheckedCount, staleReason, summaryText, checking, canReviewDrift, onCheckNow, onReview,
}) => (hasDrift ? (
  <div
    className="max-w-full px-3 py-1.5 rounded-lg inline-flex items-center gap-3 text-sm bg-amber-50 border border-amber-200 text-amber-900 dark:bg-amber-900/20 dark:border-amber-800 dark:text-amber-200"
    role="alert"
    aria-live="polite"
    data-testid="drift-banner"
  >
    <span aria-hidden="true">⚠️</span>
    <span className="font-medium">{summaryText}</span>
    <Button variant="outline" size="sm" onClick={onReview} disabled={!canReviewDrift} data-testid="review-drift-button">
      {canReviewDrift ? "Review Drift" : "Checking…"}
    </Button>
  </div>
) : (
  <output
    className="max-w-full px-3 py-1.5 rounded-lg inline-flex items-center gap-3 text-sm bg-slate-100 border border-slate-300 text-slate-800 dark:bg-slate-800/60 dark:border-slate-600 dark:text-slate-200"
    data-testid="drift-status-row"
  >
    <span aria-hidden="true">{statusRow.icon}</span>
    <span className="font-medium">{statusRow.message}</span>
    {uncheckedCount === 0 && staleReason && (
      <span className="text-xs text-slate-500 dark:text-slate-400">{staleReason}</span>
    )}
    <Button variant="outline" size="sm" onClick={onCheckNow} disabled={checking} data-testid="drift-inline-check-now-button">
      {checking ? "Checking…" : "Check Now"}
    </Button>
  </output>
));

interface DriftModalHeaderProps {
  readonly lastChecked: string | null;
  readonly staleReason: string | null;
  readonly checking: boolean;
  readonly onCheckNow: () => void;
}

const DriftModalHeader: React.FC<DriftModalHeaderProps> = ({ lastChecked, staleReason, checking, onCheckNow }) => (
  <DialogHeader>
    <DialogTitle className="text-amber-700 dark:text-amber-400">⚠️ Drift Detected</DialogTitle>
    <DialogDescription>
      The following files were changed directly in GitHub. Review each diff, then keep the
      ActionsManager version through a pull request, or use the GitHub version.
    </DialogDescription>
    <div className="flex items-center gap-3 text-xs text-slate-500 dark:text-slate-400">
      <span data-testid="drift-last-checked">
        {lastChecked ? `Last checked ${new Date(lastChecked).toLocaleString()}` : "Never checked"}
      </span>
      <Button variant="outline" size="sm" onClick={onCheckNow} disabled={checking} data-testid="drift-check-now-button">
        {checking ? "Checking…" : "Check Now"}
      </Button>
    </div>
    {staleReason && (
      // Without this the timestamp simply stops advancing and the
      // feature reads as broken rather than blocked on a token.
      <p
        className="rounded-md bg-amber-50 px-3 py-2 text-xs text-amber-900 dark:bg-amber-900/20 dark:text-amber-200"
        data-testid="drift-stale-reason"
      >
        {staleReason}
      </p>
    )}
    <a
      className="text-xs font-medium text-blue-600 hover:underline dark:text-blue-400 self-start"
      href={getDocsUrl("driftDetection")}
      rel="noreferrer"
      target="_blank"
    >
      Learn about drift detection →
    </a>
  </DialogHeader>
);

/** What the last action did: done, or why it failed. */
const ResultNotices: React.FC<{ readonly success: string | null; readonly error: string | null }> = ({ success, error }) => (
  <>
    {success && (
      <div className="px-3 py-2 text-sm rounded bg-green-50 text-green-800 border border-green-200 dark:bg-green-900/20 dark:text-green-200 dark:border-green-800">
        ✓ {success}
      </div>
    )}
    {error && (
      <div className="px-3 py-2 text-sm rounded bg-red-50 text-red-800 border border-red-200 dark:bg-red-900/20 dark:text-red-200 dark:border-red-800">
        {error}
      </div>
    )}
  </>
);

/** "Use the GitHub version" of the selected custom files and CODEOWNERS, worded for one or several. */
const githubVersionConfirmText = (rows: DriftRow[]): string => {
  if (rows.length === 1) {
    const [row] = rows;
    return `${row.name} from ${row.repo} @ ${row.branch} becomes the project's version, saved as a local change. `
      + "Drift clears wherever GitHub holds this copy; open a PR campaign to deliver it to the other repositories.";
  }
  return `The GitHub versions of ${rows.length} files become the project's versions, saved as local changes. `
    + "Drift clears wherever GitHub holds those copies; open a PR campaign to deliver them to the other repositories.";
};

/** The outcome of a bulk "Use GitHub version" across workflows and files. */
const bulkOutcome = (okCount: number, failures: string[]): { ok: boolean; message: string } => (failures.length === 0
  ? { ok: true, message: `${okCount} of ${okCount} resolved successfully.` }
  : { ok: false, message: `${okCount} resolved, ${failures.length} failed: ${failures.join("; ")}` });

const DriftDetection: React.FC<DriftDetectionProps> = ({
  user,
  projectId,
  projectName,
  selectedRepos,
  onDriftLoaded,
  refreshSignal,
  onWorkflowStatusesChanged,
  seededDriftNames,
  seededCodeownersDrift = false,
  seededCustomFileDriftCount = 0,
  onKeepActionsManagerVersion,
  onUseGithubVersionOfFiles,
  contentOf,
  onProjectChanged,
}) => {
  const [drifts, setDrifts] = useState<WorkflowDriftDetail[]>([]);
  const [codeownersDrifts, setCodeownersDrifts] = useState<CodeownersDriftDetail[]>([]);
  const [customFileDrifts, setCustomFileDrifts] = useState<CustomFileDriftDetail[]>([]);
  const [uncheckedCount, setUncheckedCount] = useState(0);
  const [liveLoaded, setLiveLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [showModal, setShowModal] = useState(false);
  const [openDiffKey, setOpenDiffKey] = useState<string | null>(null);
  // When the shown state was established. null = never checked, which must not
  // render as "clean" — see the banner below.
  const [lastChecked, setLastChecked] = useState<string | null>(null);
  // Why the state may be older than it looks (e.g. the background sweep can't
  // check this project). Null when nothing is wrong.
  const [staleReason, setStaleReason] = useState<string | null>(null);
  const [driftStatus, setDriftStatus] = useState<ProjectDriftSummary["drift_status"]>(null);
  const [checking, setChecking] = useState(false);
  // GitHub's side of a diff, fetched when a row is expanded. The cached list
  // omits it deliberately: a stored snapshot may no longer match GitHub.
  const [githubCopies, setGithubCopies] = useState<Record<string, string>>({});
  const [diffLoadingKey, setDiffLoadingKey] = useState<string | null>(null);
  const [adoptDetail, setAdoptDetail] = useState<WorkflowDriftDetail | null>(null);
  // "Use GitHub version" for custom files and CODEOWNERS replaces the project's copy, so it is confirmed first.
  const [confirmUseGithub, setConfirmUseGithub] = useState<DriftRow[] | null>(null);
  // "Delete Everywhere" for a workflow already removed from GitHub: opens
  // pull requests dropping the file from the project's other repos.
  const [confirmDeleteEverywhere, setConfirmDeleteEverywhere] = useState<DriftRow | null>(null);
  const [busy, setBusy] = useState(false);

  // Bulk-fix: select multiple drifted files and put them right together.
  const [selectedKeys, setSelectedKeys] = useState<Set<string>>(new Set());

  const rows = useMemo(
    () => driftRows(drifts, codeownersDrifts, customFileDrifts),
    [drifts, codeownersDrifts, customFileDrifts],
  );
  const selectedRows = useMemo(() => rows.filter((row) => selectedKeys.has(row.key)), [rows, selectedKeys]);

  // Groups rows of the same file that hold the identical GitHub-side change,
  // so the user can select a whole group in one click instead of eyeballing
  // diffs one at a time. github_sha is a content hash of that file's GitHub
  // blob, so it's a cheap and reliable equality key. __solo__-prefixed keys
  // guarantee rows without one never falsely group together.
  const groupKeyFor = useCallback(
    (row: DriftRow) => row.githubSha ?? `__solo__${row.key}`,
    [],
  );
  const driftGroups = useMemo(() => {
    const groups = new Map<string, DriftRow[]>();
    rows.forEach((row) => {
      const gk = groupKeyFor(row);
      groups.set(gk, [...(groups.get(gk) ?? []), row]);
    });
    return groups;
  }, [rows, groupKeyFor]);

  const toggleSelected = useCallback((k: string) => {
    setSelectedKeys((prev) => {
      const next = new Set(prev);
      if (next.has(k)) next.delete(k);
      else next.add(k);
      return next;
    });
  }, []);

  const selectAll = useCallback(() => setSelectedKeys(new Set(rows.map((row) => row.key))), [rows]);
  const deselectAll = useCallback(() => setSelectedKeys(new Set()), []);

  const selectGroup = useCallback((row: DriftRow) => {
    const group = driftGroups.get(groupKeyFor(row)) ?? [row];
    setSelectedKeys((prev) => new Set([...prev, ...group.map((g) => g.key)]));
  }, [driftGroups, groupKeyFor]);

  // Mirrors the row count so a failed background check can fall back to the
  // last known state without adding the lists as loadDrift dependencies (which
  // would re-trigger the load-on-mount effect on every successful load).
  const rowCountRef = useRef(0);
  // Discards out-of-order responses, e.g. the user navigates to a different
  // project before an in-flight request resolves. Same idiom as
  // ProjectMgmt.tsx's loadRequestCounterRef.
  const requestIdRef = useRef(0);

  /** Reads the drift lists; resolves to how many drifted rows there are now. */
  const loadDrift = useCallback(async (opts?: { refresh?: boolean }): Promise<number> => {
    const requestId = ++requestIdRef.current;
    if (!user || !projectId || !projectName || selectedRepos.length === 0) {
      rowCountRef.current = 0;
      setDrifts([]);
      setCodeownersDrifts([]);
      setCustomFileDrifts([]);
      // Otherwise these carry over from whatever project was last loaded,
      // e.g. showing this project as "last checked 3pm" using another
      // project's timestamp.
      setLastChecked(null);
      setStaleReason(null);
      setDriftStatus(null);
      setUncheckedCount(0);
      // A loaded project with no repos can't drift, so treat that as a
      // completed check and let the seeded banner clear. Before the project
      // loads there is no projectId yet — stay pending so the seed still shows.
      if (projectId) setLiveLoaded(true);
      onDriftLoaded?.([], [], []);
      return 0;
    }
    setError(null);
    try {
      const summary = await getProjectDrift(projectId, user, { refresh: opts?.refresh });
      if (requestId !== requestIdRef.current) return rowCountRef.current;
      const codeowners = summary.drifted_codeowners ?? [];
      const customFiles = summary.drifted_custom_files ?? [];
      rowCountRef.current = summary.drifted_workflows.length + codeowners.length + customFiles.length;
      setDrifts(summary.drifted_workflows);
      setCodeownersDrifts(codeowners);
      setCustomFileDrifts(customFiles);
      setLastChecked(summary.last_checked ?? null);
      setStaleReason(summary.stale_reason ?? null);
      setDriftStatus(summary.drift_status ?? null);
      // >0 means GitHub couldn't be queried for some repos, so an empty drift
      // list is not evidence that everything is in sync.
      setUncheckedCount(summary.unchecked_count ?? 0);
      setLiveLoaded(true);
      onDriftLoaded?.(summary.drifted_workflows, codeowners, customFiles);
      return rowCountRef.current;
    } catch (err: unknown) {
      if (requestId !== requestIdRef.current) return rowCountRef.current;
      setError(driftErrorMessage(err, "Failed to check drift"));
      // A failed background check must not erase a previously known drift
      // state, so liveLoaded stays as-is: on a first-load failure the seeded
      // banner keeps rendering rather than blanking out.
      return rowCountRef.current;
    }
  }, [user, projectId, projectName, selectedRepos, onDriftLoaded]);

  /**
   * Read the stored state and stop there — opening a project costs no GitHub
   * calls at all.
   *
   * This used to fire a live check behind the render whenever the stored state
   * looked old, because nothing else kept it fresh. The background sweep does
   * that now, so re-checking on mount would spend rate limit re-answering a
   * question already answered on a timer. When the sweep genuinely cannot
   * check a project, `stale_reason` says so, which is more honest than a
   * silent refresh that may also fail.
   */
  useEffect(() => {
    loadDrift();
  }, [loadDrift, refreshSignal]);

  // A row that no longer exists cannot stay selected out of sight.
  useEffect(() => {
    setSelectedKeys((prev) => {
      const kept = new Set([...prev].filter((k) => rows.some((row) => row.key === k)));
      return kept.size === prev.size ? prev : kept;
    });
  }, [rows]);

  /** Run a live check on demand. */
  const handleCheckNow = useCallback(async () => {
    setChecking(true);
    try {
      await loadDrift({ refresh: true });
      // Anything fetched for a diff describes the previous check.
      setGithubCopies({});
    } finally {
      setChecking(false);
    }
  }, [loadDrift]);

  /** GitHub's content on the row's (repo, branch): one read per opened diff. */
  const readGithubCopy = useCallback(async (row: DriftRow): Promise<string> => {
    if (row.kind !== "workflow") return fileGithubCopy(row, user, projectId ?? 0, projectName);
    const live = await getWorkflowDrift(row.fileId, user);
    const match = live.drift_details.find((d) => d.repo === row.repo && d.branch === row.branch);
    return match?.github_yaml ?? "";
  }, [user, projectId, projectName]);

  /** Expand a row, fetching GitHub's current content the stored list leaves out. */
  const handleToggleDiff = useCallback(async (row: DriftRow) => {
    if (openDiffKey === row.key) {
      setOpenDiffKey(null);
      return;
    }
    setOpenDiffKey(row.key);
    const known = row.workflow?.github_yaml ?? githubCopies[row.key];
    if (known != null || row.deleted || !user) return;

    setDiffLoadingKey(row.key);
    try {
      const content = await readGithubCopy(row);
      setGithubCopies((prev) => ({ ...prev, [row.key]: content }));
    } catch (err: unknown) {
      setError(driftErrorMessage(err, "Failed to load the GitHub version"));
    } finally {
      setDiffLoadingKey(null);
    }
  }, [openDiffKey, githubCopies, user, readGithubCopy]);

  /** "Keep the ActionsManager version": the campaign modal with exactly these files, to their repositories. */
  const handleKeep = useCallback((picked: DriftRow[]) => {
    setShowModal(false);
    setSelectedKeys(new Set());
    onKeepActionsManagerVersion?.(picked);
  }, [onKeepActionsManagerVersion]);

  /** After an action: re-read the list, and close the review once nothing is left. */
  const settle = useCallback(async (message: string, ok = true) => {
    const remaining = await loadDrift();
    if (ok && remaining === 0) {
      setSuccess("All drift resolved.");
      setTimeout(() => setShowModal(false), 2000);
      return;
    }
    // loadDrift() clears the error as soon as it starts, so the outcome is set after it.
    if (ok) setSuccess(message);
    else setError(message);
  }, [loadDrift]);

  /** Workflows' rows through the bulk resolve; resolves to the failures it reports. */
  const adoptGithubForWorkflows = useCallback(async (picked: DriftRow[]): Promise<string[]> => {
    if (picked.length === 0 || !projectId) return [];
    const response = await bulkResolveWorkflowDrift(projectId, {
      github_user: user,
      items: picked.map((row) => ({
        workflow_id: row.fileId,
        repo: row.repo,
        branch: row.branch,
        // Lets the backend reject an item whose file moved on since the check.
        expected_github_sha: row.githubSha,
      })),
      resolution: "use_github",
    });
    const succeeded = response.results.filter((r) => r.success);
    const names = succeeded
      .map((r) => picked.find((row) => row.fileId === r.workflow_id && row.repo === r.repo)?.workflow?.workflow_name)
      .filter((name): name is string => Boolean(name));
    // This component refreshes its own list; the parent's workflow state, and
    // the status badges it drives, need the same explicit patch.
    if (names.length > 0) onWorkflowStatusesChanged?.(names, "synced_with_github");
    return response.results.filter((r) => !r.success).map((r) => `${r.repo} (${r.message})`);
  }, [projectId, user, onWorkflowStatusesChanged]);

  /** "Use the GitHub version" of several rows: workflows here, files through the parent. */
  const handleBulkUseGithub = useCallback(async () => {
    const files = selectedRows.filter((row) => row.kind !== "workflow");
    const workflows = selectedRows.filter((row) => row.kind === "workflow");
    setBusy(true);
    setError(null);
    setSuccess(null);
    try {
      const failures = await adoptGithubForWorkflows(workflows);
      if (files.length > 0) await onUseGithubVersionOfFiles?.(files);
      setSelectedKeys(new Set());
      const outcome = bulkOutcome(selectedRows.length - failures.length, failures);
      await settle(outcome.message, outcome.ok);
    } catch (err: unknown) {
      setError(driftErrorMessage(err, "Using the GitHub version failed"));
    } finally {
      setBusy(false);
    }
  }, [selectedRows, adoptGithubForWorkflows, onUseGithubVersionOfFiles, settle]);

  /** One row's "Use GitHub version": a workflow's scope modal, or a file's confirmation. */
  const handleUseGithub = useCallback((row: DriftRow) => {
    if (row.workflow) setAdoptDetail(row.workflow);
    else setConfirmUseGithub([row]);
  }, []);

  const handleUseGithubOfFiles = useCallback(async (picked: DriftRow[]) => {
    setConfirmUseGithub(null);
    setBusy(true);
    setError(null);
    setSuccess(null);
    try {
      const message = await onUseGithubVersionOfFiles?.(picked);
      setOpenDiffKey(null);
      await settle(message ?? "GitHub version used.");
    } catch (err: unknown) {
      setError(driftErrorMessage(err, "Using the GitHub version failed"));
    } finally {
      setBusy(false);
    }
  }, [onUseGithubVersionOfFiles, settle]);

  const handleDeleteEverywhere = useCallback(async (row: DriftRow) => {
    setConfirmDeleteEverywhere(null);
    setBusy(true);
    setError(null);
    setSuccess(null);
    try {
      // The file is already gone from row.repo; affected_repos is the rest of
      // the project's repos that still share this workflow. A pull request
      // removes it from each, and the workflow leaves ActionsManager when they
      // merge: drift is put right through pull requests only.
      const repos = Array.from(new Set([row.repo, ...(row.workflow?.affected_repos ?? [])]));
      await deleteWorkflowFromGitHub(user, repos, row.workflow?.workflow_name ?? row.name, "", projectName, "campaign");
      setOpenDiffKey(null);
      onProjectChanged?.();
      await settle(`Pull requests opened to remove '${row.name}'. It leaves ActionsManager when they merge.`);
    } catch (err: unknown) {
      setError(driftErrorMessage(err, "Failed to remove the workflow"));
    } finally {
      setBusy(false);
    }
  }, [user, projectName, onProjectChanged, settle]);

  const driftCount = drifts.length;
  // Until the live check has succeeded once, drive the banner from the drift
  // the last check persisted. Otherwise the banner is absent on first paint
  // and pops in when the check resolves, shifting the page layout.
  const bannerCount = liveLoaded ? driftCount : (seededDriftNames?.length ?? 0);
  const codeownersDrifted = liveLoaded ? codeownersDrifts.length > 0 : seededCodeownersDrift;
  const customFileCount = driftedCustomFileCount(liveLoaded, customFileDrifts, seededCustomFileDriftCount);
  const hasDrift = bannerCount > 0 || codeownersDrifted || customFileCount > 0;
  // The seed carries names only, so the modal's rows/resolve actions aren't
  // usable yet. An error still enables it — that's how the user reaches the
  // failure message inside the modal.
  const canReviewDrift = liveLoaded || error !== null;
  const summaryText = driftSummaryText(bannerCount, codeownersDrifted, customFileCount);
  const statusRow = getDriftStatusRow(driftStatus, uncheckedCount, lastChecked);

  return (
    <>
      <DriftSummaryBar
        hasDrift={hasDrift}
        statusRow={statusRow}
        uncheckedCount={uncheckedCount}
        staleReason={staleReason}
        summaryText={summaryText}
        checking={checking}
        canReviewDrift={canReviewDrift}
        onCheckNow={handleCheckNow}
        onReview={() => setShowModal(true)}
      />

      <Dialog open={showModal} onOpenChange={setShowModal}>
        <DialogContent
          className="max-w-5xl max-h-[85vh] overflow-y-auto"
          data-testid="drift-modal"
        >
          <DriftModalHeader
            lastChecked={lastChecked}
            staleReason={staleReason}
            checking={checking}
            onCheckNow={handleCheckNow}
          />

          <ResultNotices success={success} error={error} />

          {rows.length > 0 && (
            <SelectionBar
              total={rows.length}
              selected={selectedRows}
              busy={busy}
              onSelectAll={selectAll}
              onDeselectAll={deselectAll}
              onKeep={() => handleKeep(selectedRows)}
              onUseGithub={() => (selectedRows.some((row) => row.kind !== "workflow")
                ? setConfirmUseGithub(selectedRows)
                : handleBulkUseGithub())}
            />
          )}

          <div className="overflow-x-auto">
            <table className="w-full text-sm border-collapse">
              <thead>
                <tr className="text-left border-b border-slate-200 dark:border-slate-700">
                  <th className="py-2 pr-2" />
                  <th className="py-2 pr-4">File</th>
                  <th className="py-2 pr-4">Repo</th>
                  <th className="py-2 pr-4">Branch</th>
                  <th className="py-2 pr-4">Status</th>
                  <th className="py-2 pr-4">Last Checked</th>
                  <th className="py-2">Actions</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <React.Fragment key={row.key}>
                    <DriftTableRow
                      row={row}
                      selected={selectedKeys.has(row.key)}
                      groupSize={(driftGroups.get(groupKeyFor(row)) ?? [row]).length}
                      open={openDiffKey === row.key}
                      loading={diffLoadingKey === row.key}
                      onToggleSelected={() => toggleSelected(row.key)}
                      onSelectGroup={() => selectGroup(row)}
                      onToggleDiff={() => handleToggleDiff(row)}
                    />
                    {openDiffKey === row.key && (
                      <tr>
                        <td colSpan={7} className="py-2">
                          <DriftRowDetail
                            row={row}
                            managed={row.workflow ? (row.workflow.actionsmanager_yaml ?? "") : (contentOf?.(row) ?? "")}
                            github={row.workflow?.github_yaml ?? githubCopies[row.key] ?? ""}
                            busy={busy}
                            onKeep={() => handleKeep([row])}
                            onUseGithub={() => handleUseGithub(row)}
                            onDeleteEverywhere={() => setConfirmDeleteEverywhere(row)}
                          />
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                ))}
              </tbody>
            </table>
          </div>

          <DialogFooter>
            <Button variant="outline" onClick={() => setShowModal(false)}>
              Cancel
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <AdoptGithubVersionModal
        open={adoptDetail !== null}
        onClose={() => setAdoptDetail(null)}
        detail={adoptDetail}
        user={user}
        onResolved={(message) => settle(message)}
      />

      <ConfirmDialog
        open={confirmUseGithub !== null}
        title="Use the GitHub version?"
        description={confirmUseGithub ? githubVersionConfirmText(confirmUseGithub) : ""}
        confirmLabel="Use GitHub version"
        onCancel={() => setConfirmUseGithub(null)}
        onConfirm={() => {
          const picked = confirmUseGithub ?? [];
          if (picked.some((row) => row.kind === "workflow")) {
            setConfirmUseGithub(null);
            handleBulkUseGithub();
          } else {
            handleUseGithubOfFiles(picked);
          }
        }}
      />

      <ConfirmDialog
        open={confirmDeleteEverywhere !== null}
        title="Delete this workflow everywhere?"
        description={
          confirmDeleteEverywhere
            ? `Pull requests will remove '${confirmDeleteEverywhere.name}' from `
              + `${Array.from(new Set([confirmDeleteEverywhere.repo, ...(confirmDeleteEverywhere.workflow?.affected_repos ?? [])])).join(", ")}. `
              + "It is removed from ActionsManager, version history included, when they merge."
            : ""
        }
        confirmLabel={busy ? "Opening pull requests…" : "Delete everywhere"}
        destructive
        onCancel={() => setConfirmDeleteEverywhere(null)}
        onConfirm={() => {
          if (confirmDeleteEverywhere) handleDeleteEverywhere(confirmDeleteEverywhere);
        }}
      />
    </>
  );
};

export default DriftDetection;
