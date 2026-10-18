/**
 * The page owns the project's one CODEOWNERS file and its drift.
 *
 * - A CODEOWNERS-only edit can be delivered on its own: Create Pull Requests
 *   only enables while the project is a draft, and the save says whether it is.
 * - A save resolves the file's stored drift, so the page re-reads drift at once
 *   rather than leaving "Drift detected" up until a refresh (the reported bug).
 * - Drift reaches the editor and the file list, and the drift panel opens the editor.
 */
import React from "react";
import { render, screen, waitFor, act } from "@testing-library/react";
import "@testing-library/jest-dom";

const mockNavigate = vi.fn();
const mockUseParams = vi.fn();

// Captured so tests can drive the section nav the way a user would, without
// rendering the real Sidebar.
let capturedOnSectionChange: ((section: string) => void) | null = null;
let capturedWorkflowsProps: any = null;
let capturedDriftProps: any = null;
let capturedPRModalProps: any = null;

vi.mock("react-router", () => ({
  useNavigate: () => mockNavigate,
  useParams: () => mockUseParams(),
  Link: function Link(props: any) {
    return <a href={props.to}>{props.children}</a>;
  },
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
  updateProjectDriftConfig: vi.fn(),
  updateProjectOrder: vi.fn(),
  exportProjectBackup: vi.fn(),
  linkReusableWorkflow: vi.fn(),
  unlinkReusableWorkflow: vi.fn(),
}));

vi.mock("./api/driftSettings", () => ({
  fetchDriftSettings: vi.fn().mockResolvedValue({ recheck_interval_minutes: 30 }),
  formatDriftInterval: () => "every 30 minutes",
  DEFAULT_DRIFT_SETTINGS: { recheck_interval_minutes: 30 },
  DRIFT_INTERVAL_OPTIONS: [],
}));

vi.mock("./api/projectDeletion", () => ({ deleteProjectEnhanced: vi.fn() }));
vi.mock("./api/handlers", () => ({ handleSaveProjectWithModal: vi.fn() }));
vi.mock("./api/secrets", () => ({ getSecrets: vi.fn() }));
vi.mock("./api/envVars", () => ({ getEnvVars: vi.fn() }));
vi.mock("./api/pullRequests", () => ({ getProjectPRStatus: vi.fn() }));
vi.mock("./api/actionsProjects", () => ({ listActionsProjects: vi.fn().mockResolvedValue([]) }));
vi.mock("./api/actionGroups", () => ({ listActionGroups: vi.fn().mockResolvedValue([]) }));
vi.mock("./api/codeowners", () => ({
  getProjectCodeowners: vi.fn(),
}));

vi.mock("./components/Sidebar", () => ({
  default: function Sidebar(props: any) {
    capturedOnSectionChange = props.onSectionChange;
    return <div data-testid="sidebar" />;
  },
}));

vi.mock("./components/RepositoriesAndBranches", () => ({
  default: function RepositoriesAndBranches() {
    return <div data-testid="repositories-and-branches" />;
  },
}));

vi.mock("./components/CreatePRModal", () => ({
  default: function CreatePRModal(props: any) {
    capturedPRModalProps = props;
    return <div data-testid="create-pr-modal" />;
  },
}));

vi.mock("./components/ProjectList", () => ({
  default: function ProjectList() {
    return <div data-testid="project-list" />;
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

// Exposes the callbacks ProjectMgmt hands down, so a CODEOWNERS save can be
// reported the way the real editor reports it.
vi.mock("./components/UnifiedWorkflows", () => ({
  default: function UnifiedWorkflows(props: any) {
    capturedWorkflowsProps = props;
    return <div data-testid="unified-workflows" />;
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

vi.mock("./components/DriftDetection", () => ({
  default: function DriftDetection(props: any) {
    capturedDriftProps = props;
    return null;
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

vi.mock("./components/BuildMetricsPanel", () => ({
  default: function BuildMetricsPanel() {
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

vi.mock("./components/WorkflowImportPanel", () => ({
  WorkflowImportPanel: function WorkflowImportPanel() {
    return null;
  },
}));

import ProjectMgmt from "./ProjectMgmt";
import { fetchProjects, loadProject } from "./api/projects";
import { getSecrets } from "./api/secrets";
import { getEnvVars } from "./api/envVars";
import { getProjectPRStatus } from "./api/pullRequests";
import { getProjectCodeowners } from "./api/codeowners";

import type { Mock } from "vitest";

const userDetails = {
  avatar_url: "https://example.com/avatar.png",
  github_user: "alice",
  account_type: "free",
  github_account_type: "User" as const,
};

const record = (overrides: Record<string, unknown> = {}) => ({
  id: 1, project_id: 42, content: "* @acme/owners\n", file_path: ".github/CODEOWNERS", git_hash: "a".repeat(40),
  status: "synced_with_github", last_modified_by: "alice", created_at: null, updated_at: null, ...overrides,
});

const drifted = {
  file_path: ".github/CODEOWNERS", repo: "acme/existing", repo_id: 3, branch: "main", has_drift: true,
  actionsmanager_sha: "a".repeat(40), github_sha: "b".repeat(40), last_checked: "",
  message: "CODEOWNERS content differs between local and acme/existing",
};

function projectPayload(prState: string, codeownersDrifted = false) {
  return {
    project_name: "proj-a",
    project_id: 42,
    project_code: "PROJA",
    selected_repos: ["acme/existing"],
    workflows: [{ name: "ci", content: "name: CI\non: push\n", workflow_status: "synced_with_github" }],
    rxworkflows: [],
    branch_regex: "",
    branch_option: "default",
    branch_max_age_days: 30,
    reusable_workflows_enabled: false,
    use_prefix: false,
    project_type: "standard",
    pr_state: prState,
    codeowners_drifted: codeownersDrifted,
  };
}

describe("ProjectMgmt – the project's CODEOWNERS file", () => {
  async function renderWorkflows(prState: string, { codeowners = record(), codeownersDrifted = false } = {}) {
    vi.clearAllMocks();
    capturedOnSectionChange = null;
    capturedWorkflowsProps = null;
    capturedDriftProps = null;
    capturedPRModalProps = null;
    mockUseParams.mockReturnValue({ user: "alice", projectName: "proj-a" });
    (fetchProjects as Mock).mockResolvedValue([
      { project_id: 42, project_name: "proj-a", project_code: "PROJA" },
    ]);
    (loadProject as Mock).mockResolvedValue(projectPayload(prState, codeownersDrifted));
    (getSecrets as Mock).mockResolvedValue([]);
    (getEnvVars as Mock).mockResolvedValue([]);
    (getProjectPRStatus as Mock).mockResolvedValue({
      project_state: prState, open_prs: 0, merged_prs: 0, total_prs: 0,
    });
    (getProjectCodeowners as Mock).mockResolvedValue(codeowners);

    render(<ProjectMgmt userDetails={userDetails} onLogout={vi.fn()} />);
    await waitFor(() => expect(capturedOnSectionChange).not.toBeNull());
    await waitFor(() => expect(loadProject).toHaveBeenCalled());
    act(() => capturedOnSectionChange!("workflows"));
    await waitFor(() => expect(capturedWorkflowsProps?.codeowners).toEqual(codeowners));
  }

  test("loads the project's file once, by project id, for the editor and the list", async () => {
    await renderWorkflows("synced");

    expect(getProjectCodeowners).toHaveBeenCalledWith(42);
    expect(capturedWorkflowsProps.codeownersDrift).toEqual([]);
  });

  test("a synced project enables the button as soon as CODEOWNERS is saved, with no reload", async () => {
    await renderWorkflows("synced");
    const button = await screen.findByTestId("create-pull-requests-button");
    expect(button).toBeDisabled();

    act(() => capturedWorkflowsProps.onCodeownersSaved(record({ status: "committed_locally" }), "draft"));

    await waitFor(() => expect(button).toBeEnabled());
    expect(loadProject).toHaveBeenCalledTimes(1);
  });

  test("the state comes from the save response, not a guess from the page's copy", async () => {
    // The page loaded 'synced', but PRs were opened elsewhere since: the server
    // keeps 'open', and the button must not unlock a second campaign.
    await renderWorkflows("synced");
    const button = await screen.findByTestId("create-pull-requests-button");

    act(() => capturedWorkflowsProps.onCodeownersSaved(record({ status: "committed_locally" }), "open"));

    await waitFor(() =>
      expect(button).toHaveAttribute("title", "Pull requests are already open and under review"),
    );
    expect(button).toBeDisabled();
  });

  test("a save hands the saved file to the editor and list without refetching it", async () => {
    await renderWorkflows("draft");
    const saved = record({ content: "* @acme/new\n", status: "committed_locally" });

    act(() => capturedWorkflowsProps.onCodeownersSaved(saved, "draft"));

    await waitFor(() => expect(capturedWorkflowsProps.codeowners).toEqual(saved));
    expect(getProjectCodeowners).toHaveBeenCalledTimes(1);
  });

  test("a save re-reads drift, so CODEOWNERS stops showing as drifted without a refresh", async () => {
    // The reported bug: "Drift detected" stayed up after Commit Locally.
    await renderWorkflows("draft");
    act(() => capturedDriftProps.onDriftLoaded([], [drifted]));
    await waitFor(() => expect(capturedWorkflowsProps.codeownersDrift).toEqual([drifted]));
    const signal = capturedDriftProps.refreshSignal;

    act(() => capturedWorkflowsProps.onCodeownersSaved(record({ status: "committed_locally" }), "draft"));

    await waitFor(() => expect(capturedDriftProps.refreshSignal).toBe(signal + 1));
    // What the re-read returns once the save has resolved the file's drift.
    act(() => capturedDriftProps.onDriftLoaded([], []));
    await waitFor(() => expect(capturedWorkflowsProps.codeownersDrift).toEqual([]));
    expect(loadProject).toHaveBeenCalledTimes(1);
  });

  test("the drift banner counts CODEOWNERS from the project load on first paint", async () => {
    await renderWorkflows("synced", { codeownersDrifted: true });

    await waitFor(() => expect(capturedDriftProps.seededCodeownersDrift).toBe(true));
  });

  test("the drift panel opens the CODEOWNERS editor", async () => {
    await renderWorkflows("synced");
    act(() => capturedOnSectionChange!("secrets"));

    act(() => capturedDriftProps.onOpenCodeowners());

    await waitFor(() => expect(capturedWorkflowsProps.codeownersOpenRequested).toBe(true));
    act(() => capturedWorkflowsProps.onCodeownersOpened());
    await waitFor(() => expect(capturedWorkflowsProps.codeownersOpenRequested).toBe(false));
  });

  test("a campaign can carry the file, and one that did reloads it to show it under review", async () => {
    await renderWorkflows("draft", { codeowners: record({ status: "committed_locally" }) });
    act(() => screen.getByTestId("create-pull-requests-button").click());
    await waitFor(() => expect(capturedPRModalProps).not.toBeNull());
    expect(capturedPRModalProps.codeowners).toEqual({ filePath: ".github/CODEOWNERS", status: "committed_locally" });
    (getProjectCodeowners as Mock).mockResolvedValue(record({ status: "under_review" }));

    act(() => capturedPRModalProps.onSuccess([], [], [], true, ["acme/existing"]));

    await waitFor(() => expect(capturedWorkflowsProps.codeowners.status).toBe("under_review"));
  });

  test("a project without CODEOWNERS offers none to a campaign", async () => {
    await renderWorkflows("draft", { codeowners: null as any });
    act(() => screen.getByTestId("create-pull-requests-button").click());

    await waitFor(() => expect(capturedPRModalProps).not.toBeNull());
    expect(capturedPRModalProps.codeowners).toBeNull();
  });

  test("a CODEOWNERS change that moves pr_state refreshes the projects list too", async () => {
    await renderWorkflows("synced");
    const listFetches = (fetchProjects as Mock).mock.calls.length;

    act(() => capturedWorkflowsProps.onCodeownersSaved(record({ status: "committed_locally" }), "draft"));

    await waitFor(() => expect(fetchProjects).toHaveBeenCalledTimes(listFetches + 1));
  });
});
