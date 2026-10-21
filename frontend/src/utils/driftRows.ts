import { getCodeowners } from "../api/codeowners";
import { getCustomFileOnGitHub } from "../api/customFiles";
import type { CodeownersDriftDetail, CustomFileDriftDetail, WorkflowDriftDetail } from "../api/drift";

export type DriftKind = "workflow" | "custom_file" | "codeowners";

/** One drifted (file, repo, branch), whatever the file is: a workflow, a custom file or CODEOWNERS. */
export interface DriftRow {
  /** Unique per (file, repo, branch). A workflow's is "id::repo::branch", as it always was. */
  key: string;
  kind: DriftKind;
  fileId: number;
  /** What the row shows: the workflow's filename, or the file's path. */
  name: string;
  repo: string;
  branch: string;
  deleted: boolean;
  githubSha: string | null;
  lastChecked: string;
  message: string;
  /** A workflow row's own detail: its YAML, whether it is shared, its repo override. */
  workflow?: WorkflowDriftDetail;
}

const workflowRow = (d: WorkflowDriftDetail): DriftRow => ({
  key: `${d.workflow_id}::${d.repo}::${d.branch}`,
  kind: "workflow",
  fileId: d.workflow_id,
  name: d.workflow_filename,
  repo: d.repo,
  branch: d.branch,
  deleted: Boolean(d.deleted_in_github),
  githubSha: d.github_sha ?? null,
  lastChecked: d.last_checked,
  message: d.message,
  workflow: d,
});

const fileRow = (kind: Exclude<DriftKind, "workflow">, fileId: number, d: CodeownersDriftDetail): DriftRow => ({
  key: `${kind}:${fileId}::${d.repo}::${d.branch}`,
  kind,
  fileId,
  name: d.file_path,
  repo: d.repo,
  branch: d.branch,
  deleted: Boolean(d.deleted_in_github),
  githubSha: d.github_sha ?? null,
  lastChecked: d.last_checked,
  message: d.message,
});

/** Every drifted (file, repo, branch) of a project, in the one list the review shows. */
export const driftRows = (
  workflows: WorkflowDriftDetail[],
  codeowners: CodeownersDriftDetail[],
  customFiles: CustomFileDriftDetail[],
): DriftRow[] => [
  ...workflows.map(workflowRow),
  ...customFiles.map((d) => fileRow("custom_file", d.custom_file_id, d)),
  ...codeowners.map((d) => fileRow("codeowners", d.file_id ?? 0, d)),
];

/**
 * Why "Use GitHub version" cannot take *rows* at once, or null when it can. A
 * deleted file has no GitHub version, and a file picked in two copies that
 * differ has no single one.
 */
export const githubVersionConflict = (rows: DriftRow[]): string | null => {
  if (rows.some((row) => row.deleted)) return "A file deleted on GitHub has no GitHub version to use.";
  const copies = new Map<string, Set<string>>();
  for (const row of rows) {
    const file = `${row.kind}:${row.fileId}`;
    copies.set(file, (copies.get(file) ?? new Set()).add(row.githubSha ?? row.key));
  }
  return [...copies.values()].some((shas) => shas.size > 1)
    ? "These rows hold different GitHub versions of the same file: pick the one to use."
    : null;
};

const readFileCopy = async (row: DriftRow, user: string, projectId: number, projectName: string) => {
  if (row.kind === "custom_file") {
    return (await getCustomFileOnGitHub(projectId, row.fileId, row.repo, row.branch)).github;
  }
  return (await getCodeowners(row.repo, user, projectName, { branch: row.branch, path: row.name })).github;
};

/**
 * A custom file's or the CODEOWNERS file's copy on the row's (repo, branch),
 * read where its drift check reads it. With *sameAsChecked*, refused when
 * GitHub moved on since the check, as a workflow's adoption is refused (409):
 * otherwise "Use GitHub version" could take content the user never saw.
 */
export const fileGithubCopy = async (
  row: DriftRow, user: string, projectId: number, projectName: string, sameAsChecked = false,
): Promise<string> => {
  const found = await readFileCopy(row, user, projectId, projectName);
  if (sameAsChecked && row.githubSha && found.sha !== row.githubSha) {
    throw new Error(
      `${row.name} in ${row.repo} @ ${row.branch} changed since drift was checked. `
      + "Run Check Now and review the current difference before using it.",
    );
  }
  return found.content ?? "";
};

/** The first of *rows* for each file: one copy to act on per file. */
export const oneRowPerFile = (rows: DriftRow[]): DriftRow[] => {
  const seen = new Set<string>();
  return rows.filter((row) => {
    const file = `${row.kind}:${row.fileId}`;
    if (seen.has(file)) return false;
    seen.add(file);
    return true;
  });
};

/** Exactly the files *rows* name, as the campaign modal takes them; *reusable* names the project's reusable workflows. */
export const campaignSelectionOf = (rows: DriftRow[], reusable: ReadonlySet<string>) => {
  const names = [...new Set(rows.flatMap((row) => (row.workflow ? [row.workflow.workflow_name] : [])))];
  return {
    workflows: names.filter((name) => !reusable.has(name)),
    reusableWorkflows: names.filter((name) => reusable.has(name)),
    customFileIds: [...new Set(rows.filter((row) => row.kind === "custom_file").map((row) => row.fileId))],
    codeowners: rows.some((row) => row.kind === "codeowners"),
  };
};
