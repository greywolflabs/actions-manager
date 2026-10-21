/**
 * Regression: Commit Locally on a drifted workflow left "changed in GitHub" up.
 *
 * The save resolves the workflow's stored drift server-side, but nothing asked
 * <DriftDetection> to re-read it, so the banner and the workflow's badge kept
 * the drift until the next background check. Each local save now bumps the
 * drift refresh signal once it has resolved. These tests render the real
 * DriftDetection and never reload the project.
 */
import React from "react";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
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

// The app header's tabs need a real router, and this file mocks react-router.
vi.mock("./components/AppHeader", () => import("./test/appHeaderStub"));

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

// Stands in for the editor: its save calls refreshDrift once the save resolves
// (useWorkflowOperations.test.ts), so the button calls it the same way.
vi.mock("./components/UnifiedWorkflows", () => ({
  default: function UnifiedWorkflows(props: any) {
    return (
      <div data-testid="unified-workflows">
        <span data-testid="drifted-names">{Array.from(props.driftedWorkflowNames ?? []).join(",")}</span>
        <button type="button" onClick={() => props.refreshDrift?.()}>workflow saved</button>
        <button type="button" onClick={() => props.onImportExisting?.()}>open import</button>
      </div>
    );
  },
}));

vi.mock("./components/WorkflowImportPanel", () => ({
  WorkflowImportPanel: function WorkflowImportPanel(props: any) {
    return <button type="button" onClick={() => props.onImportComplete?.("draft")}>import done</button>;
  },
}));

vi.mock("./components/PRStatusPanel", () => ({
  default: function PRStatusPanel() {
    return null;
  },
}));

vi.mock("./components/PRHistoryPanel", () => ({
  default: function PRHistoryPanel() {
    return null;
  },
}));

vi.mock("./components/CreatePRModal", () => ({
  default: function CreatePRModal() {
    return null;
  },
}));

vi.mock("./components/LinkedWorkflowsModal", () => ({
  default: function LinkedWorkflowsModal() {
    return null;
  },
}));

vi.mock("./components/ProjectColorSelector", () => ({
  default: function ProjectColorSelector() {
    return null;
  },
}));

import ProjectMgmt from "./ProjectMgmt";
import { fetchProjects, loadProject } from "./api/projects";
import { getSecrets } from "./api/secrets";
import { getEnvVars } from "./api/envVars";
import { getProjectPRStatus } from "./api/pullRequests";
import { getProjectDrift, type ProjectDriftSummary, type WorkflowDriftDetail } from "./api/drift";

import type { Mock } from "vitest";

const userDetails = {
  avatar_url: "https://example.com/avatar.png",
  github_user: "alice",
  account_type: "free",
  github_account_type: "User" as const,
};

const project = {
  project_name: "proj-a",
  project_id: 42,
  project_code: "PROJA",
  selected_repos: ["acme/proj-a"],
  workflows: [{ name: "CI", content: "name: CI\non: push\n", workflowStatus: "synced_with_github" }],
  rxworkflows: [],
  branch_regex: "",
  branch_option: "default",
  branch_max_age_days: 30,
  reusable_workflows_enabled: false,
  use_prefix: false,
  project_type: "standard",
  pr_state: "new",
  drifted_workflow_names: ["CI"],
};

const driftedCI: WorkflowDriftDetail = {
  workflow_id: 1,
  workflow_name: "CI",
  workflow_filename: "ci.yml",
  repo: "acme/proj-a",
  branch: "main",
  has_drift: true,
  actionsmanager_yaml: "name: CI\non: push\n",
  github_yaml: null,
  actionsmanager_sha: "a".repeat(40),
  github_sha: "b".repeat(40),
  last_checked: "2026-10-16T00:00:00+00:00",
  message: "Workflow content differs between local and acme/proj-a",
};

const summary = (drifted: WorkflowDriftDetail[]): ProjectDriftSummary => ({
  project_id: 42,
  project_name: "proj-a",
  drift_count: drifted.length,
  drifted_workflows: drifted,
  drifted_codeowners: [],
  last_checked: "2026-10-16T00:00:00+00:00",
  unchecked_count: 0,
  drift_status: drifted.length ? "drifted" : "clean",
});

async function renderDriftedProject() {
  render(<ProjectMgmt userDetails={userDetails} onLogout={vi.fn()} />);
  await waitFor(() => expect(screen.getByTestId("drift-banner")).toHaveTextContent("1 workflow changed in GitHub"));
  expect(screen.getByTestId("drifted-names")).toHaveTextContent("CI");
  // The save below resolves the drift on the server.
  (getProjectDrift as Mock).mockResolvedValue(summary([]));
}

async function expectDriftCleared() {
  await waitFor(() => expect(screen.queryByTestId("drift-banner")).not.toBeInTheDocument());
  expect(screen.getByTestId("drifted-names")).toBeEmptyDOMElement();
  // Re-read from the stored state, which costs no GitHub calls.
  expect(getProjectDrift).toHaveBeenLastCalledWith(42, "alice", { refresh: undefined });
}

describe("ProjectMgmt – a local workflow save clears its drift without a refresh", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUseParams.mockReturnValue({ user: "alice", projectName: "proj-a" });
    (fetchProjects as Mock).mockResolvedValue([]);
    (loadProject as Mock).mockResolvedValue(project);
    (getSecrets as Mock).mockResolvedValue([]);
    (getEnvVars as Mock).mockResolvedValue([]);
    (getProjectPRStatus as Mock).mockResolvedValue({ project_state: "new", open_prs: 0, merged_prs: 0, total_prs: 0 });
    (getProjectDrift as Mock).mockResolvedValue(summary([driftedCI]));
  });

  test("Commit Locally re-reads the drift, and the banner and badge clear", async () => {
    await renderDriftedProject();

    fireEvent.click(screen.getByRole("button", { name: "workflow saved" }));

    await expectDriftCleared();
    expect(loadProject).toHaveBeenCalledTimes(1);
  });

  test("importing over a drifted workflow clears its drift too", async () => {
    await renderDriftedProject();

    fireEvent.click(screen.getByRole("button", { name: "open import" }));
    fireEvent.click(await screen.findByRole("button", { name: "import done" }));

    await expectDriftCleared();
  });
});
