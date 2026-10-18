/**
 * Regression: adopting a drifted custom file's GitHub copy must clear its drift
 * at once.
 *
 * Commit Locally resolves the file's stored drift server-side; the page owns
 * the file list and the drift, so it must re-read the drift once the save has
 * resolved. Renders the page with its real Project Files list, custom file
 * editor and drift panel, and never reloads the project or remounts the editor.
 */
import React from "react";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import "@testing-library/jest-dom";

const mockNavigate = vi.fn();
const mockUseParams = vi.fn();

vi.mock(
  "react-router",
  () => ({
    useNavigate: () => mockNavigate,
    useParams: () => mockUseParams(),
    useLocation: () => ({
      pathname: "/project/alice/proj-a",
      search: "",
      hash: "",
      state: null,
      key: "test",
    }),
  }));

vi.mock("./api/projects", () => ({
  __esModule: true,
  fetchProjects: vi.fn(),
  loadProject: vi.fn(),
  updateProjectName: vi.fn(),
  updateProjectColor: vi.fn(),
  exportProjectBackup: vi.fn(),
  linkReusableWorkflow: vi.fn(),
  unlinkReusableWorkflow: vi.fn(),
}));

vi.mock("./api/projectDeletion", () => ({
  deleteProjectEnhanced: vi.fn(),
}));

vi.mock("./api/handlers", () => ({
  handleSaveProjectWithModal: vi.fn(),
}));

vi.mock("./api/secrets", () => ({
  getSecrets: vi.fn(),
}));

vi.mock("./api/envVars", () => ({
  getEnvVars: vi.fn(),
}));

vi.mock("./api/pullRequests", () => ({
  getProjectPRStatus: vi.fn(),
}));

vi.mock("./api/actionsProjects", () => ({ listActionsProjects: vi.fn().mockResolvedValue([]) }));
vi.mock("./api/actionGroups", () => ({ listActionGroups: vi.fn().mockResolvedValue([]) }));

vi.mock("./api/codeowners", () => ({
  getProjectCodeowners: vi.fn().mockResolvedValue(null),
}));

vi.mock("./components/Sidebar", () => ({
  default: function Sidebar() {
    return <div data-testid="sidebar" />;
  },
}));

vi.mock("./components/ProjectList", () => ({
  default: function ProjectList(props: any) {
    return (
      <div data-testid="project-list">
        {(props.projects ?? []).map((p: any) => (
          <span
            key={p.id ?? p.project_id}
            data-testid="project-drift-in-list"
          >
            {p.drift_status ?? "unknown"}
          </span>
        ))}
      </div>
    );
  },
}));

vi.mock("./components/RepositoriesAndBranches", () => ({
  default: function RepositoriesAndBranches() {
    return <div data-testid="repositories-and-branches" />;
  },
}));

vi.mock("./components/DeployEnvironments", () => ({
  default: function DeployEnvironments() {
    return <div data-testid="deploy-environments" />;
  },
}));

vi.mock("./components/EnvVars", () => ({
  default: function EnvVars() {
    return <div data-testid="env-vars" />;
  },
}));

vi.mock("./components/Secrets", () => ({
  default: function Secrets() {
    return <div data-testid="secrets" />;
  },
}));

vi.mock("./components/RulesetManager", () => ({
  default: function RulesetManager() {
    return <div data-testid="rulesets" />;
  },
}));

vi.mock("./components/CodeownersManager", () => ({
  default: function CodeownersManager() {
    return <div data-testid="codeowners" />;
  },
}));

vi.mock("./components/UserAvatar", () => ({
  default: function UserAvatar() {
    return <div data-testid="user-avatar" />;
  },
}));

vi.mock("./components/PlanUsagePill", () => ({
  default: function PlanUsagePill() {
    return null;
  },
}));

vi.mock("./components/BrandLogo", () => ({
  default: function BrandLogo() {
    return null;
  },
}));

vi.mock("./components/SaveResultsModal", () => ({
  default: function SaveResultsModal() {
    return null;
  },
}));

vi.mock("./components/DeleteProjectModal", () => ({
  default: function DeleteProjectModal() {
    return null;
  },
}));

vi.mock("./components/DangerZone", () => ({
  default: function DangerZone() {
    return null;
  },
}));

vi.mock("./components/ProjectMembers", () => ({
  default: function ProjectMembers() {
    return null;
  },
}));

vi.mock("./api/drift", () => ({
  getProjectDrift: vi.fn(),
  getWorkflowDrift: vi.fn(),
  resolveWorkflowDrift: vi.fn(),
  adoptGithubVersion: vi.fn(),
  bulkResolveWorkflowDrift: vi.fn(),
}));

vi.mock("./components/UnifiedWorkflowEditor", () => ({ default: () => null }));
vi.mock("./components/WorkflowCreationDialog", () => ({ default: () => null }));
vi.mock("./components/TemplateSelectionModal", () => ({ default: () => null }));
// One object for every render, as the real hook's callbacks are stable: the
// parent-callback effects re-run whenever these change.
vi.mock("./hooks/useWorkflowOperations", () => {
  const operations = {
    fetchWorkflowsCount: vi.fn(),
    openWorkflowCreationDialog: vi.fn(),
    selectWorkflowType: vi.fn(),
    handleGenerateTemplates: vi.fn(),
    handleSaveDraftWorkflow: vi.fn(),
    handleSaveDraftLinkedWorkflow: vi.fn(),
    handleCommitAndUpdatePR: vi.fn(),
    handleCommitAndUpdatePRLinked: vi.fn(),
    handleDeleteWorkflow: vi.fn(),
    handleUnlinkWorkflow: vi.fn(),
  };
  return { useWorkflowOperations: () => operations };
});

vi.mock("./components/PlainFileEditor", () => ({
  default: ({ value, onChange }: { value: string; onChange?: (v: string) => void }) => (
    <textarea data-testid="file-content-input" value={value ?? ""} onChange={(e) => onChange?.(e.target.value)} />
  ),
}));

vi.mock("./api/customFiles", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./api/customFiles")>()),
  updateCustomFile: vi.fn(),
  getCustomFileOnGitHub: vi.fn(),
}));

vi.mock("./components/PRStatusPanel", () => ({ default: () => null }));
vi.mock("./components/PRHistoryPanel", () => ({ default: () => null }));
vi.mock("./components/CreatePRModal", () => ({ default: () => null }));
vi.mock("./components/LinkedWorkflowsModal", () => ({ default: () => null }));
vi.mock("./components/ProjectColorSelector", () => ({ default: () => null }));

import ProjectMgmt from "./ProjectMgmt";
import { fetchProjects, loadProject } from "./api/projects";
import { getSecrets } from "./api/secrets";
import { getEnvVars } from "./api/envVars";
import { getProjectPRStatus } from "./api/pullRequests";
import { getProjectDrift, type CustomFileDriftDetail, type ProjectDriftSummary } from "./api/drift";
import { getCustomFileOnGitHub, updateCustomFile, type CustomFile } from "./api/customFiles";

import type { Mock } from "vitest";

const userDetails = {
  avatar_url: "https://example.com/avatar.png",
  github_user: "alice",
  account_type: "free",
  github_account_type: "User" as const,
};

const SAVED = "#!/bin/sh\necho build\n";
const GITHUB_COPY = "#!/bin/sh\necho hotfix\n";

const buildScript: CustomFile = {
  id: 9,
  project_id: 42,
  display_name: null,
  file_path: "scripts/build.sh",
  file_content: SAVED,
  git_hash: "a".repeat(40),
  file_status: "synced_with_github",
  pending_delete: false,
  last_modified_by: "alice",
  description: null,
  created_at: null,
  updated_at: null,
};

const project = {
  project_name: "proj-a",
  project_id: 42,
  project_code: "PROJA",
  selected_repos: ["acme/proj-a"],
  workflows: [],
  rxworkflows: [],
  branch_regex: "",
  branch_option: "default",
  branch_max_age_days: 30,
  reusable_workflows_enabled: false,
  use_prefix: false,
  project_type: "standard",
  pr_state: "synced",
  custom_files: [buildScript],
  drifted_workflow_names: [],
  drifted_custom_file_ids: [9],
};

const driftedScript: CustomFileDriftDetail = {
  custom_file_id: 9,
  file_path: "scripts/build.sh",
  repo: "acme/proj-a",
  repo_id: 1,
  branch: "main",
  has_drift: true,
  actionsmanager_sha: "a".repeat(40),
  github_sha: "b".repeat(40),
  last_checked: "2026-10-16T00:00:00+00:00",
  message: "scripts/build.sh content differs between local and acme/proj-a",
};

const summary = (drifted: CustomFileDriftDetail[]): ProjectDriftSummary => ({
  project_id: 42,
  project_name: "proj-a",
  drift_count: drifted.length,
  drifted_workflows: [],
  drifted_codeowners: [],
  drifted_custom_files: drifted,
  last_checked: "2026-10-16T00:00:00+00:00",
  unchecked_count: 0,
  drift_status: drifted.length ? "drifted" : "clean",
});

const scriptRow = () => screen.getByTestId("custom-file-row");

describe("ProjectMgmt – adopting a custom file's GitHub copy clears its drift without a refresh", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUseParams.mockReturnValue({ user: "alice", projectName: "proj-a" });
    (fetchProjects as Mock).mockResolvedValue([]);
    (loadProject as Mock).mockResolvedValue(project);
    (getSecrets as Mock).mockResolvedValue([]);
    (getEnvVars as Mock).mockResolvedValue([]);
    (getProjectPRStatus as Mock).mockResolvedValue({ project_state: "synced", open_prs: 0, merged_prs: 0, total_prs: 0 });
    (getProjectDrift as Mock).mockResolvedValue(summary([driftedScript]));
    (getCustomFileOnGitHub as Mock).mockResolvedValue({
      repo_id: 1, repo_name: "acme/proj-a", branch: "main",
      github: { exists: true, content: GITHUB_COPY, sha: "b".repeat(40), path: "scripts/build.sh" },
    });
    (updateCustomFile as Mock).mockImplementation(async (_projectId: number, id: number, data: any) => ({
      custom_file: { ...buildScript, id, file_content: data.file_content, file_status: "committed_locally", git_hash: null },
    }));
  });

  test("the list seeds the file's drift from the project load", async () => {
    (getProjectDrift as Mock).mockReturnValue(new Promise(() => {}));
    render(<ProjectMgmt userDetails={userDetails} onLogout={vi.fn()} />);

    await waitFor(() => expect(within(scriptRow()).getByTestId("drift-badge")).toBeInTheDocument());
    expect(screen.getByTestId("drift-banner")).toHaveTextContent("1 custom file changed in GitHub");
  });

  test("Load GitHub version, then Commit Locally: the banner, the editor's drift and the row's badge clear", async () => {
    render(<ProjectMgmt userDetails={userDetails} onLogout={vi.fn()} />);
    await waitFor(() => expect(screen.getByTestId("drift-banner")).toHaveTextContent("1 custom file changed in GitHub"));
    fireEvent.click(scriptRow());
    const banner = await screen.findByTestId("custom-file-drift-banner");
    expect(banner).toHaveTextContent("acme/proj-a @ main");

    fireEvent.click(within(banner).getByRole("button", { name: "Load GitHub version" }));
    await waitFor(() => expect(screen.getByTestId("file-content-input")).toHaveValue(GITHUB_COPY));
    // The save resolves the drift on the server.
    (getProjectDrift as Mock).mockResolvedValue(summary([]));
    const editor = screen.getByTestId("file-content-input");
    fireEvent.click(screen.getByTestId("save-button"));

    await waitFor(() => expect(screen.queryByTestId("custom-file-drift-banner")).not.toBeInTheDocument());
    expect(updateCustomFile).toHaveBeenCalledWith(42, 9, expect.objectContaining({ file_content: GITHUB_COPY }));
    expect(screen.queryByTestId("file-drift-badge")).not.toBeInTheDocument();
    expect(within(scriptRow()).queryByTestId("drift-badge")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.queryByTestId("drift-banner")).not.toBeInTheDocument());
    // Re-read from the stored state, which costs no GitHub calls; the project is never reloaded.
    expect(getProjectDrift).toHaveBeenLastCalledWith(42, "alice", { refresh: undefined });
    expect(loadProject).toHaveBeenCalledTimes(1);
    // The same editor, still showing what was saved.
    expect(screen.getByTestId("file-content-input")).toBe(editor);
    expect(editor).toHaveValue(GITHUB_COPY);
  });
});
