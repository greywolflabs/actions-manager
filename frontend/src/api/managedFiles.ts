import apiClient from "./apiClient";

/** A file kind a Managed Files project can manage, as the server defines it. */
export interface ManagedFileKind {
  kind: string;
  label: string;
  /** Every path GitHub reads the file from, in the order it looks. */
  paths: string[];
}

/** GET /api/managed-files: what the section page needs on top of its project list. */
export interface ManagedFilesSection {
  kinds: ManagedFileKind[];
  /** Managed Files projects the caller owns: what the limit counts. */
  owned_count: number;
  /** null when the plan has no limit. */
  limit: number | null;
  limit_message: string | null;
}

/** The project holding a repository, or nulls when the caller cannot see it. */
export interface TakenRepo {
  project_id: number | null;
  project_name: string | null;
}

// The most names POST /api/managed-files/{kind}/taken-repos answers at once.
const TAKEN_REPOS_BATCH = 1000;

export const fetchManagedFiles = async (): Promise<ManagedFilesSection> => {
  const response = await apiClient.get<ManagedFilesSection>("/api/managed-files");
  return response.data;
};

/**
 * Which of *repoNames* another Managed Files project of *kind* holds. Names
 * no project holds are left out. *excludeProjectId* is the project being
 * edited, whose own repositories are not taken from it.
 */
export const fetchTakenRepos = async (
  kind: string,
  repoNames: string[],
  excludeProjectId?: number,
): Promise<Record<string, TakenRepo>> => {
  const taken: Record<string, TakenRepo> = {};
  for (let start = 0; start < repoNames.length; start += TAKEN_REPOS_BATCH) {
    const response = await apiClient.post<{ taken: Record<string, TakenRepo> }>(
      `/api/managed-files/${encodeURIComponent(kind)}/taken-repos`,
      {
        repo_names: repoNames.slice(start, start + TAKEN_REPOS_BATCH),
        ...(excludeProjectId === undefined ? {} : { exclude_project_id: excludeProjectId }),
      },
    );
    Object.assign(taken, response.data.taken);
  }
  return taken;
};
