import apiClient from "./apiClient";

// ===== Type Definitions =====

/** The project's CODEOWNERS file, delivered to every repository by its PR campaigns. */
export interface CodeownersRecord {
  id: number;
  project_id: number;
  content: string;
  file_path: string;
  git_hash: string | null;
  status: string;
  last_modified_by: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface CodeownersGitHubInfo {
  exists: boolean;
  content: string | null;
  sha: string | null;
  path: string | null;
}

export interface CodeownersGetResponse {
  success: boolean;
  repo_id: number;
  repo_name: string;
  branch: string | null;
  github: CodeownersGitHubInfo;
  local: CodeownersRecord | null;
}

export interface CodeownersSaveResponse {
  success: boolean;
  message: string;
  codeowners: CodeownersRecord;
  validation_warnings: string[];
  /** The project's pr_state after the save, so the page need not guess it. */
  pr_state?: string;
}

// ===== API Functions =====

const repoSegment = (repoRef: string | number): string =>
  typeof repoRef === "number" ? String(repoRef) : encodeURI(repoRef);

/** The project's CODEOWNERS file, or null when it has none (no GitHub calls). */
export const getProjectCodeowners = async (projectId: number): Promise<CodeownersRecord | null> => {
  const response = await apiClient.get<{ codeowners: CodeownersRecord | null }>(
    `/api/projects/${projectId}/codeowners`
  );
  return response.data.codeowners;
};

/**
 * Save the project's CODEOWNERS locally (Commit Locally). A change is a local
 * edit waiting to be delivered by a PR campaign, never drift.
 */
export const saveProjectCodeowners = async (
  projectId: number,
  content: string,
  filePath: string = ".github/CODEOWNERS"
): Promise<CodeownersSaveResponse> => {
  const response = await apiClient.put<CodeownersSaveResponse>(
    `/api/projects/${projectId}/codeowners`,
    { content, file_path: filePath }
  );
  return response.data;
};

/**
 * A repository's CODEOWNERS on GitHub, to import it or to load the version a
 * branch drifted to. `branch` defaults to the repository's default branch and
 * `path` to the first standard location that exists.
 */
export const getCodeowners = async (
  repoRef: string | number,
  githubUser: string,
  projectName: string,
  options: { branch?: string; path?: string } = {}
): Promise<CodeownersGetResponse> => {
  const response = await apiClient.get<CodeownersGetResponse>(
    `/api/repos/${repoSegment(repoRef)}/codeowners`,
    {
      params: {
        github_user: githubUser,
        project_name: projectName,
        branch: options.branch,
        path: options.path,
      },
    }
  );
  return response.data;
};
