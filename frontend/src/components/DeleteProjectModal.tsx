import React, { useState, useEffect, useCallback } from "react";
import { getProjectDeletionSummary } from "../api/projectDeletion";
import { errorDetail } from "../api/errorDetail";
import type { DeleteProjectOptions, ProjectDeletionSummary, RemovalFile, RemovalTarget } from "../api/projectDeletion";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "./ui/dialog";
import { Button } from "./ui/button";

interface DeleteProjectModalProps {
  isOpen: boolean;
  onClose: () => void;
  onConfirmDelete: (options: Required<DeleteProjectOptions>) => void;
  projectName: string;
  githubUser: string;
}

const KEEP_FILES_LABEL = "Delete the project, keep its files in GitHub";
const REMOVE_FILES_LABEL =
  "Delete the project and remove its files from GitHub (opens a pull request per repository and branch)";
const DELETE_SECRETS_LABEL = "Also delete secrets and variables now";

const SUMMARY_ERROR = "Could not load what this project holds in GitHub. You can still delete it and keep its files.";

interface FileChoiceProps {
  label: string;
  selected: boolean;
  disabled?: boolean;
  onSelect: () => void;
  children: React.ReactNode;
}

const FileChoice: React.FC<FileChoiceProps> = ({ label, selected, disabled, onSelect, children }) => (
  <div className={`border rounded-lg border-slate-200 dark:border-slate-600 ${disabled ? "opacity-60" : ""}`}>
    <label className={`flex items-start p-4 ${disabled ? "cursor-not-allowed" : "cursor-pointer"}`}>
      {/* Named on the input: the label wraps an explanation that would otherwise become its name. */}
      <input
        type="radio"
        name="projectFiles"
        aria-label={label}
        checked={selected}
        disabled={disabled}
        onChange={onSelect}
        className="mt-1 mr-3 shrink-0"
      />
      <div className="flex-1 text-sm text-slate-600 dark:text-slate-400 space-y-2">
        <strong className="block text-base text-slate-900 dark:text-slate-100">{label}</strong>
        {children}
      </div>
    </label>
  </div>
);

const FileList: React.FC<{ files: RemovalFile[] }> = ({ files }) => (
  <ul className="ml-4 list-disc">
    {files.map((file) => (
      <li key={`${file.kind}:${file.path}`}>
        <code className="font-mono text-xs">{file.path}</code>
      </li>
    ))}
  </ul>
);

const RemovalPlan: React.FC<{ targets: RemovalTarget[]; unrecorded: RemovalFile[] }> = ({ targets, unrecorded }) => (
  <div className="space-y-2" data-testid="removal-plan">
    {targets.length === 0 ? (
      <p>
        ActionsManager has no record of delivering any of its files, so there is nothing to remove and the
        project is deleted now.
      </p>
    ) : (
      <>
        <p className="font-medium text-slate-800 dark:text-slate-200">Pull requests to open ({targets.length}):</p>
        <ul className="space-y-2">
          {targets.map((target) => (
            <li key={`${target.repository}@${target.branch}`}>
              <code className="font-mono text-xs text-slate-800 dark:text-slate-200">
                {target.repository}@{target.branch}
              </code>
              <FileList files={target.files} />
            </li>
          ))}
        </ul>
      </>
    )}
    {unrecorded.length > 0 && (
      <div>
        <p>Not removed, because ActionsManager has no record of where they were delivered:</p>
        <FileList files={unrecorded} />
      </div>
    )}
  </div>
);

interface SecretsOptionProps {
  summary: ProjectDeletionSummary;
  checked: boolean;
  onChange: (checked: boolean) => void;
  keepEnvironments: boolean;
  onKeepEnvironmentsChange: (keep: boolean) => void;
}

const SecretsOption: React.FC<SecretsOptionProps> = ({
  summary,
  checked,
  onChange,
  keepEnvironments,
  onKeepEnvironmentsChange,
}) => {
  const environments = summary.deployment_environments.length;
  const nothingFound = summary.secrets.length + summary.environment_variables.length + environments === 0;
  return (
    <div className="border rounded-lg border-slate-200 p-4 space-y-2 text-sm text-slate-600 dark:border-slate-600 dark:text-slate-400">
      <label className="flex items-start cursor-pointer">
        <input
          type="checkbox"
          aria-label={DELETE_SECRETS_LABEL}
          checked={checked}
          disabled={nothingFound}
          onChange={(e) => onChange(e.target.checked)}
          className="mt-1 mr-3 shrink-0"
        />
        <span>
          <strong className="block text-base text-slate-900 dark:text-slate-100">{DELETE_SECRETS_LABEL}</strong>
          {nothingFound ? (
            <span>No secrets, variables or deployment environments were found for this project.</span>
          ) : (
            <span>
              Deletes its {summary.secrets.length} secret(s) and {summary.environment_variables.length} variable(s)
              from its repositories immediately, without a pull request. This cannot be undone, and it is not
              undone if the deletion is called off.
            </span>
          )}
        </span>
      </label>
      {checked && environments > 0 && (
        <label className="flex items-center ml-7">
          <input
            type="checkbox"
            checked={keepEnvironments}
            onChange={(e) => onKeepEnvironmentsChange(e.target.checked)}
            className="mr-2"
          />
          Keep deployment environments (otherwise all {environments} in its repositories are deleted too)
        </label>
      )}
    </div>
  );
};

const RemovalDetail: React.FC<{ summary: ProjectDeletionSummary }> = ({ summary }) => {
  if (summary.removal_blocked_reason) {
    return (
      <p className="text-amber-800 dark:text-amber-200" data-testid="removal-blocked-reason">
        Not available: {summary.removal_blocked_reason}
      </p>
    );
  }
  return <RemovalPlan targets={summary.removal_targets ?? []} unrecorded={summary.unrecorded_files ?? []} />;
};

const confirmLabel = (removing: boolean, summary: ProjectDeletionSummary | null): string => {
  if (removing && (summary?.removal_targets?.length ?? 0) > 0) return "Delete and open pull requests";
  return "Delete project";
};

const DeleteProjectModal: React.FC<DeleteProjectModalProps> = ({
  isOpen,
  onClose,
  onConfirmDelete,
  projectName,
  githubUser,
}) => {
  const [summary, setSummary] = useState<ProjectDeletionSummary | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [removeFiles, setRemoveFiles] = useState<boolean>(false);
  const [deleteSecrets, setDeleteSecrets] = useState<boolean>(false);
  const [keepDeploymentEnvironments, setKeepDeploymentEnvironments] = useState<boolean>(false);

  const fetchDeletionSummary = useCallback(async (): Promise<void> => {
    setLoading(true);
    setError(null);
    try {
      setSummary(await getProjectDeletionSummary(githubUser, projectName));
    } catch (err) {
      setError(SUMMARY_ERROR);
      console.error("❌ Error fetching deletion summary:", errorDetail(err));
    } finally {
      setLoading(false);
    }
  }, [projectName, githubUser]);

  // Each opening starts from the choice that changes nothing in GitHub.
  useEffect(() => {
    if (isOpen && projectName && githubUser) {
      setSummary(null);
      setRemoveFiles(false);
      setDeleteSecrets(false);
      setKeepDeploymentEnvironments(false);
      fetchDeletionSummary();
    }
  }, [isOpen, projectName, githubUser, fetchDeletionSummary]);

  // Never while blocked, nor before the summary says what it would remove.
  const removing = removeFiles && !!summary && !summary.removal_blocked_reason;

  const handleDeleteConfirm = (): void => {
    onConfirmDelete({
      removeFiles: removing,
      deleteSecrets,
      // The dialog asks whether to keep them; the API takes whether to delete them.
      deleteDeploymentEnvironments: !keepDeploymentEnvironments,
    });
  };

  return (
    <Dialog open={isOpen} onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent className="max-w-3xl max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="text-red-600 dark:text-red-400">Delete project {projectName}</DialogTitle>
          <DialogDescription>Choose what happens to the project's files in GitHub.</DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          {loading && <p className="text-slate-600 dark:text-slate-400">Loading what the project holds…</p>}

          {error && (
            <p className="bg-amber-50 border border-amber-200 rounded-lg p-3 text-amber-800 dark:bg-amber-950 dark:border-amber-800 dark:text-amber-200">
              {error}
            </p>
          )}

          <FileChoice label={KEEP_FILES_LABEL} selected={!removing} onSelect={() => setRemoveFiles(false)}>
            <p>
              ActionsManager deletes the project now and stops managing it. The files it delivered stay in your
              repositories as they are.
            </p>
          </FileChoice>

          {summary && (
            <>
              <FileChoice
                label={REMOVE_FILES_LABEL}
                selected={removing}
                disabled={!!summary.removal_blocked_reason}
                onSelect={() => setRemoveFiles(true)}
              >
                <p>
                  Each pull request removes every file ActionsManager delivered to that branch. The project is
                  read-only while they are open, and is deleted once all of them have merged. Closing any of them
                  calls the deletion off and keeps the project.
                </p>
                <RemovalDetail summary={summary} />
              </FileChoice>
              <SecretsOption
                summary={summary}
                checked={deleteSecrets}
                onChange={setDeleteSecrets}
                keepEnvironments={keepDeploymentEnvironments}
                onKeepEnvironmentsChange={setKeepDeploymentEnvironments}
              />
            </>
          )}
        </div>

        <DialogFooter>
          <div className="flex justify-end gap-3 w-full">
            <Button type="button" variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button
              type="button"
              variant="destructive"
              onClick={handleDeleteConfirm}
              disabled={loading}
              className="min-w-[160px]"
            >
              {confirmLabel(removing, summary)}
            </Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
};

export default DeleteProjectModal;
