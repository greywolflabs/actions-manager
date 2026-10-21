import apiClient from "./apiClient";
import type { RemovalDelivery, RemovalScope } from "../components/RemovalScopeDialog";

export interface CustomFile {
  id: number;
  project_id: number;
  display_name: string | null;
  file_path: string;
  file_content: string;
  git_hash: string | null;
  file_status: string;
  pending_delete: boolean;
  last_modified_by: string | null;
  description: string | null;
  created_at: string | null;
  updated_at: string | null;
}

/** A custom file with a change no campaign has delivered yet: an edit, or its removal.
 * The server locks only these for review when a campaign carries them. */
export const hasLocalChange = (file: Pick<CustomFile, "file_status" | "pending_delete">): boolean =>
  file.file_status !== "synced_with_github" || file.pending_delete;

export interface CreateCustomFilePayload {
  github_user?: string;
  display_name?: string;
  file_path: string;
  file_content: string;
  description?: string;
}

export interface UpdateCustomFilePayload {
  github_user?: string;
  display_name?: string;
  file_path?: string;
  file_content?: string;
  description?: string;
}

export const createCustomFile = (projectId: number, data: CreateCustomFilePayload): Promise<{ custom_file: CustomFile }> =>
  apiClient.post(`/api/projects/${projectId}/custom-files`, data).then((r) => r.data);

export const updateCustomFile = (projectId: number, id: number, data: UpdateCustomFilePayload): Promise<{ custom_file: CustomFile }> =>
  apiClient.put(`/api/projects/${projectId}/custom-files/${id}`, data).then((r) => r.data);

/**
 * `scope` is required on purpose. The server defaults to `project_and_github`
 * for wire compatibility, so an omitted argument here would silently delete the
 * file from GitHub — the opposite of what the UI preselects.
 */
/** One (repo, branch) the deletion reached, as the server reported it. */
export interface CustomFileDeleteTarget {
  repo: string;
  branch: string | null;
  status: "deleted" | "absent" | "error";
  error?: string | null;
  pr_url?: string;
  pr_number?: number;
}

export interface DeleteCustomFileResponse {
  deleted: boolean;
  hard_deleted?: boolean;
  pending_delete?: boolean;
  custom_file?: CustomFile;
  targets?: CustomFileDeleteTarget[];
  /** Set when the removal was delivered as a PR Campaign. */
  campaign_id?: number | null;
  prs_created?: number;
  /** The project's pr_state after the removal, so the caller need not reload to get it. */
  pr_state?: string | null;
}

export const deleteCustomFile = (
  projectId: number,
  id: number,
  scope: RemovalScope,
  delivery: RemovalDelivery = "campaign"
): Promise<DeleteCustomFileResponse> =>
  apiClient
    .delete(`/api/projects/${projectId}/custom-files/${id}`, { params: { scope, delivery } })
    .then((r) => r.data);

export const restoreCustomFile = (projectId: number, id: number): Promise<{ custom_file: CustomFile }> =>
  apiClient.post(`/api/projects/${projectId}/custom-files/${id}/restore`).then((r) => r.data);

export interface CustomFileOnGitHub {
  repo_id: number;
  repo_name: string;
  branch: string;
  github: { exists: boolean; content: string | null; sha: string | null; path: string };
}

/** The file's copy on one of the project's (repo, branch), read where its drift check reads it. */
export const getCustomFileOnGitHub = (
  projectId: number,
  id: number,
  repo: string,
  branch: string
): Promise<CustomFileOnGitHub> =>
  apiClient
    .get(`/api/projects/${projectId}/custom-files/${id}/github`, { params: { repo, branch } })
    .then((r) => r.data);

/** Client-side path validation (mirrors server-side rules). Returns error string or null. */
export const validateFilePath = (path: string): string | null => {
  if (!path?.trim()) return "File path is required";
  const p = path.trim();
  if (p.startsWith("/")) return "Absolute paths are not allowed";
  const parts = p.replaceAll("\\", "/").split("/");
  if (parts.includes("..")) return "Path traversal (..) is not allowed";
  if (parts[0] === ".git" || parts.slice(1).includes(".git"))
    return ".git/ paths are not allowed";
  const basename = (parts.at(-1) ?? "").toLowerCase();
  if (basename === ".env" || basename.startsWith(".env."))
    return ".env files are not allowed";
  const blocked = [".pem", ".key", ".p12", ".pfx", ".crt", ".cert", ".jks"];
  const lower = p.toLowerCase();
  for (const ext of blocked) {
    if (lower.endsWith(ext)) return `${ext} files are not allowed`;
  }
  return null;
};
