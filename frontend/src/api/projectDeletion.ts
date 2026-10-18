import apiClient from "./apiClient";
import config from "../config";
import { errorDetail } from "./errorDetail";
import type { ProjectPRState } from "../types/workflow";

const BACKEND_URL = config.BACKEND_URL;

// Mirrors the backend ProjectDeletionSummary model in
// backend/project_deletion.py. Declared here rather than in the modal that
// renders it so the request and its shape stay in one place.
export interface DeletionSummaryWorkflow {
  name: string;
  is_reusable: boolean;
  created_at?: string;
  updated_at?: string;
}

export interface DeletionSummarySecret {
  name: string;
  repository: string;
  created_at?: string;
  updated_at?: string;
}

export interface DeletionSummaryEnvironmentVariable {
  name: string;
  repository: string;
  environment: string;
  value?: string;
}

export interface DeletionSummaryDeploymentEnvironment {
  name: string;
  repository: string;
  url?: string;
}

/** One file a removal pull request takes off a branch: *name* is what the project calls it now, *path* where it is there. */
export interface RemovalFile {
  kind: "workflow" | "custom_file" | "codeowners";
  name: string;
  path: string;
}

/** One (repository, branch) a removal pull request would be opened on. */
export interface RemovalTarget {
  repository: string;
  branch: string;
  files: RemovalFile[];
}

export interface ProjectDeletionSummary {
  project_name: string;
  project_code: string;
  workflows: DeletionSummaryWorkflow[];
  reusable_workflows: DeletionSummaryWorkflow[];
  secrets: DeletionSummarySecret[];
  environment_variables: DeletionSummaryEnvironmentVariable[];
  deployment_environments: DeletionSummaryDeploymentEnvironment[];
  /** Where removing the project's files would open a pull request, from the delivery record. */
  removal_targets?: RemovalTarget[];
  /** Files that may be in GitHub where the record cannot say: no pull request removes them. */
  unrecorded_files?: RemovalFile[];
  /** Why the project cannot be deleted with its files now; null when it can. */
  removal_blocked_reason?: string | null;
}

export interface DeleteProjectOptions {
  /** Remove the project's files from GitHub by a pull request per repository and branch before it goes. */
  removeFiles?: boolean;
  /** Delete its secrets and variables directly and at once. */
  deleteSecrets?: boolean;
  /** With deleteSecrets, delete the deployment environments too. */
  deleteDeploymentEnvironments?: boolean;
}

export interface EnhancedDeletionResult {
  message?: string;
  /** False while its removal pull requests are open: the project is then "deleting". */
  project_deleted: boolean;
  pr_state?: ProjectPRState | null;
  campaign_id?: number | null;
  prs_created?: number;
  /** Each (repository, branch) the removal reached, keyed "org/repo on branch". */
  results?: Record<string, { status: string; error?: string; pr_url?: string }>;
  details?: {
    project_deleted: boolean;
    /** Human-readable lines, e.g. "Repository Secret: FOO from org/repo". */
    github_resources_deleted: string[];
    /** Secrets that could not be deleted, and targets no removal pull request was opened on. */
    errors: string[];
  };
}

/** What the server said a refused or failed deletion was about, for the user. */
export const deletionErrorMessage = (error: unknown): string => {
  const data = errorDetail(error) as { detail?: string | { message?: string } } | undefined;
  const detail = data?.detail;
  if (typeof detail === "string") return detail;
  return detail?.message ?? "Error deleting project. Please try again.";
};


// Get project deletion summary (list of resources that would be deleted)
export const getProjectDeletionSummary = async (
  user: string,
  projectName: string
): Promise<ProjectDeletionSummary> => {
  try {
    console.log(`📌 Debug: Getting deletion summary for project '${projectName}', user '${user}'`);

    const response = await apiClient.get(`${BACKEND_URL}/api/projects/${encodeURIComponent(projectName)}/deletion-summary`, {
      params: { github_user: user },
    });

    return response.data;
  } catch (error) {
    console.error(
      "❌ Error getting project deletion summary:",
      errorDetail(error)
    );
    throw error;
  }
};

// Delete a project, keeping its files in GitHub or removing them by pull request.
export const deleteProjectEnhanced = async (
  projectName: string,
  { removeFiles = false, deleteSecrets = false, deleteDeploymentEnvironments = true }: DeleteProjectOptions = {}
): Promise<EnhancedDeletionResult> => {
  try {
    const response = await apiClient.delete(`${BACKEND_URL}/api/projects/${encodeURIComponent(projectName)}/enhanced`, {
      data: {
        project_name: projectName,
        remove_files: removeFiles,
        delete_github_resources: deleteSecrets,
        delete_deployment_environments: deleteDeploymentEnvironments,
      }
    });

    return response.data;
  } catch (error) {
    console.error(
      "❌ Error deleting project:",
      errorDetail(error)
    );
    throw error;
  }
};
