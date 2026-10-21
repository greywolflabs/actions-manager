import { describe, expect, it, vi } from "vitest";
import { getCodeowners } from "../api/codeowners";
import { getCustomFileOnGitHub } from "../api/customFiles";
import type { CodeownersDriftDetail, CustomFileDriftDetail, WorkflowDriftDetail } from "../api/drift";
import { campaignSelectionOf, driftRows, fileGithubCopy, githubVersionConflict, oneRowPerFile } from "./driftRows";

vi.mock("../api/codeowners", () => ({ getCodeowners: vi.fn() }));
vi.mock("../api/customFiles", () => ({ getCustomFileOnGitHub: vi.fn() }));

const fileDrift = (overrides: Partial<CodeownersDriftDetail> = {}): CodeownersDriftDetail => ({
  file_id: 3, file_path: ".github/CODEOWNERS", repo: "org/a", branch: "main", has_drift: true,
  actionsmanager_sha: "am", github_sha: "gh", last_checked: "", message: "", ...overrides,
});
const customFileDrift = (overrides: Partial<CustomFileDriftDetail> = {}): CustomFileDriftDetail => ({
  ...fileDrift({ file_id: 7, file_path: "scripts/build.sh" }), custom_file_id: 7, ...overrides,
});
const workflowDrift = (overrides: Partial<WorkflowDriftDetail> = {}): WorkflowDriftDetail => ({
  workflow_id: 1, workflow_name: "ci", workflow_filename: "ci.yml", repo: "org/a", branch: "main", has_drift: true,
  actionsmanager_yaml: "a", github_yaml: null, actionsmanager_sha: "am", github_sha: "gh", last_checked: "",
  message: "", ...overrides,
});

describe("driftRows", () => {
  it("lists every kind in one shape, a workflow's key as it always was", () => {
    const rows = driftRows([workflowDrift()], [fileDrift()], [customFileDrift({ deleted_in_github: true })]);

    expect(rows.map((row) => [row.key, row.kind, row.fileId, row.name, row.deleted])).toEqual([
      ["1::org/a::main", "workflow", 1, "ci.yml", false],
      ["custom_file:7::org/a::main", "custom_file", 7, "scripts/build.sh", true],
      ["codeowners:3::org/a::main", "codeowners", 3, ".github/CODEOWNERS", false],
    ]);
  });

  it("names exactly the drifted files for the campaign, a reusable workflow as one", () => {
    const rows = driftRows(
      [workflowDrift(), workflowDrift({ workflow_id: 2, workflow_name: "shared", repo: "org/b" }),
        workflowDrift({ repo: "org/b" })],
      [fileDrift()],
      [customFileDrift(), customFileDrift({ repo: "org/b" })],
    );

    expect(campaignSelectionOf(rows, new Set(["shared"]))).toEqual({
      workflows: ["ci"], reusableWorkflows: ["shared"], customFileIds: [7], codeowners: true,
    });
  });

  it("has a GitHub version to use only with one copy per file, and none for a deleted file", () => {
    const same = driftRows([workflowDrift(), workflowDrift({ repo: "org/b" })], [], []);
    const differ = driftRows([workflowDrift(), workflowDrift({ repo: "org/b", github_sha: "other" })], [], []);
    const deleted = driftRows([], [fileDrift({ deleted_in_github: true, github_sha: null })], []);

    expect(githubVersionConflict(same)).toBeNull();
    expect(githubVersionConflict(differ)).toMatch(/different GitHub versions of the same file/);
    expect(githubVersionConflict(deleted)).toMatch(/no GitHub version/);
  });

  it("acts on one row per file", () => {
    const rows = driftRows([workflowDrift(), workflowDrift({ repo: "org/b" })], [fileDrift()], []);

    expect(oneRowPerFile(rows).map((row) => row.key)).toEqual(["1::org/a::main", "codeowners:3::org/a::main"]);
  });
});

describe("fileGithubCopy", () => {
  const github = (content: string, sha: string) => ({
    repo_id: 1, repo_name: "org/a", branch: "main", github: { exists: true, content, sha, path: "scripts/build.sh" },
  });
  const [row] = driftRows([], [], [customFileDrift({ github_sha: "checked" })]);

  it("reads the file's copy where its drift check reads it", async () => {
    vi.mocked(getCustomFileOnGitHub).mockResolvedValue(github("echo hi\n", "moved-on"));

    await expect(fileGithubCopy(row, "u", 42, "proj")).resolves.toBe("echo hi\n");
    expect(getCustomFileOnGitHub).toHaveBeenCalledWith(42, 7, "org/a", "main");
  });

  it("refuses a copy GitHub changed since the check, when it must be the one checked", async () => {
    vi.mocked(getCustomFileOnGitHub).mockResolvedValue(github("echo newer\n", "moved-on"));

    await expect(fileGithubCopy(row, "u", 42, "proj", true)).rejects.toThrow(/changed since drift was checked/);
  });

  it("takes the copy the check saw", async () => {
    vi.mocked(getCustomFileOnGitHub).mockResolvedValue(github("echo hi\n", "checked"));

    await expect(fileGithubCopy(row, "u", 42, "proj", true)).resolves.toBe("echo hi\n");
  });

  it("reads CODEOWNERS at the path it was delivered to", async () => {
    vi.mocked(getCodeowners).mockResolvedValue({
      success: true, repo_id: 1, repo_name: "org/a", branch: "main", local: null,
      github: { exists: true, content: "* @o\n", sha: "gh", path: ".github/CODEOWNERS" },
    });
    const [codeowners] = driftRows([], [fileDrift()], []);

    await expect(fileGithubCopy(codeowners, "u", 42, "proj", true)).resolves.toBe("* @o\n");
    expect(getCodeowners).toHaveBeenCalledWith("org/a", "u", "proj", { branch: "main", path: ".github/CODEOWNERS" });
  });
});
