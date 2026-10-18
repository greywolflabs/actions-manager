/**
 * A save whose trigger change gave a workflow the other kind moves it to that
 * section at once (#2318).
 *
 * The server reports it in `kind_changes`; ProjectMgmt moves its own lists
 * rather than leaving it to UnifiedWorkflows, which is not mounted when the
 * save is made from another section. While it is mounted, its selection
 * follower is called first, so the editor stays on the workflow it showed.
 */
import React from "react";
import { render, screen, waitFor, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "@testing-library/jest-dom";

const mockNavigate = vi.fn();
const mockUseParams = vi.fn();

// Captured so tests can drive the section nav and the repo selection the way a
// user would, without rendering the real Sidebar / picker trees.
let capturedOnSectionChange: ((section: string) => void) | null = null;
let capturedSetSelectedRepos: ((repos: string[]) => void) | null = null;
let capturedPRModalProps: any = null;
// The badge/notice inputs, so a save can be asserted to update them in place.
let capturedPendingDeliveryRepos: string[] | undefined;
let capturedSidebarPendingCount: number | undefined;
let capturedWorkflowsProps: any = null;
const mockFollower = vi.fn();

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
  getProjectCodeowners: vi.fn().mockResolvedValue(null),
}));

vi.mock("./components/Sidebar", () => ({
  default: function Sidebar(props: any) {
    capturedOnSectionChange = props.onSectionChange;
    capturedSidebarPendingCount = props.pendingDeliveryCount;
    return <div data-testid="sidebar" />;
  },
}));

vi.mock("./components/RepositoriesAndBranches", () => ({
  default: function RepositoriesAndBranches(props: any) {
    capturedSetSelectedRepos = props.setSelectedRepos;
    capturedPendingDeliveryRepos = props.pendingDeliveryRepos;
    return <div data-testid="repositories-and-branches" />;
  },
}));

// Captures the props the campaign modal is opened with, so tests can assert on
// the repo scope without driving the real 1100-line modal.
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

// Exposes the lists ProjectMgmt hands down, and registers a selection follower
// the way the real component does while it is mounted.
vi.mock("./components/UnifiedWorkflows", () => ({
  default: function UnifiedWorkflows(props: any) {
    capturedWorkflowsProps = props;
    const { onRegisterSelectionFollower } = props;
    React.useEffect(() => {
      onRegisterSelectionFollower?.(mockFollower);
      return () => onRegisterSelectionFollower?.(null);
    }, [onRegisterSelectionFollower]);
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
  default: function DriftDetection() {
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
import { handleSaveProjectWithModal } from "./api/handlers";

import type { Mock } from "vitest";

const userDetails = {
  avatar_url: "https://example.com/avatar.png",
  github_user: "alice",
  account_type: "free",
  github_account_type: "User" as const,
};

const SAVED_REPO = "acme/existing";
const NEW_REPO = "acme/added";
const KIND_CHANGE = { name: "deploy", is_reusable: true, workflow_status: "committed_locally" };

function projectPayload() {
  return {
    project_name: "proj-a",
    project_id: 42,
    project_code: "PROJA",
    selected_repos: [SAVED_REPO],
    workflows: [
      { name: "ci", content: "name: CI\non: push\n", workflow_status: "synced_with_github" },
      { name: "deploy", content: "on: workflow_call\n", workflow_status: "synced_with_github" },
    ],
    rxworkflows: [],
    branch_regex: "",
    branch_option: "default",
    branch_max_age_days: 30,
    reusable_workflows_enabled: false,
    use_prefix: false,
    project_type: "standard",
    pr_state: "synced",
  };
}

const names = (list: { name: string }[]) => list.map(w => w.name);

describe("ProjectMgmt – a save that changes a workflow's kind (#2318)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    capturedOnSectionChange = null;
    capturedSetSelectedRepos = null;
    capturedWorkflowsProps = null;

    mockUseParams.mockReturnValue({ user: "alice", projectName: "proj-a" });
    (fetchProjects as Mock).mockResolvedValue([
      { project_id: 42, project_name: "proj-a", project_code: "PROJA" },
    ]);
    (loadProject as Mock).mockResolvedValue(projectPayload());
    (getSecrets as Mock).mockResolvedValue([]);
    (getEnvVars as Mock).mockResolvedValue([]);
    (getProjectPRStatus as Mock).mockResolvedValue({
      project_state: "synced", open_prs: 0, merged_prs: 0, total_prs: 0,
    });
    (handleSaveProjectWithModal as Mock).mockResolvedValue({
      success: true,
      results: ["ok"],
      projectId: 42,
      projectCode: "PROJA",
      prState: "draft",
      kindChanges: [KIND_CHANGE],
    });
  });

  async function renderAt(section: string) {
    render(<ProjectMgmt userDetails={userDetails} onLogout={vi.fn()} />);
    await waitFor(() => expect(capturedOnSectionChange).not.toBeNull());
    await waitFor(() => expect(loadProject).toHaveBeenCalled());
    act(() => capturedOnSectionChange!(section));
  }

  test("a save made from another section moves it, with no reload", async () => {
    await renderAt("repos-and-branches");
    await waitFor(() => expect(capturedSetSelectedRepos).not.toBeNull());
    act(() => capturedSetSelectedRepos!([SAVED_REPO, NEW_REPO]));
    const save = await screen.findByRole("button", { name: /💾 Save/ });
    await waitFor(() => expect(save).toBeEnabled());
    await userEvent.click(save);
    await waitFor(() => expect(handleSaveProjectWithModal).toHaveBeenCalled());

    act(() => capturedOnSectionChange!("workflows"));

    await waitFor(() => expect(names(capturedWorkflowsProps.rxworkflows)).toEqual(["deploy"]));
    expect(names(capturedWorkflowsProps.workflows)).toEqual(["ci"]);
    expect(capturedWorkflowsProps.rxworkflows[0]).toMatchObject({
      isReusable: true, isModified: false, workflowStatus: "committed_locally",
    });
    expect(loadProject).toHaveBeenCalledTimes(1);
  });

  test("with the Workflows section open, the editor's selection follows it first", async () => {
    await renderAt("workflows");
    await waitFor(() => expect(capturedWorkflowsProps?.workflows).toHaveLength(2));
    act(() => capturedWorkflowsProps.setWorkflows((prev: any[]) =>
      prev.map(w => ({ ...w, isModified: true }))));

    await userEvent.click(await screen.findByRole("button", { name: /Commit 2 Workflows/ }));

    await waitFor(() => expect(names(capturedWorkflowsProps.rxworkflows)).toEqual(["deploy"]));
    expect(mockFollower).toHaveBeenCalledWith([KIND_CHANGE]);
    // Called while the lists it resolves the selection against were unmoved.
    expect(names(capturedWorkflowsProps.workflows)).toEqual(["ci"]);
  });

  test("an edit typed while the save was in flight moves with the workflow", async () => {
    let finishSave: (value: unknown) => void = () => {};
    (handleSaveProjectWithModal as Mock).mockReturnValue(
      new Promise(resolve => { finishSave = resolve; }),
    );
    await renderAt("workflows");
    await waitFor(() => expect(capturedWorkflowsProps?.workflows).toHaveLength(2));
    act(() => capturedWorkflowsProps.setWorkflows((prev: any[]) =>
      prev.map(w => ({ ...w, isModified: true }))));
    await userEvent.click(await screen.findByRole("button", { name: /Commit 2 Workflows/ }));
    await waitFor(() => expect(handleSaveProjectWithModal).toHaveBeenCalled());

    act(() => capturedWorkflowsProps.setWorkflows((prev: any[]) =>
      prev.map(w => (w.name === "deploy" ? { ...w, content: "on: workflow_call\n# typed meanwhile\n" } : w))));
    await act(async () => finishSave({
      success: true, results: ["ok"], projectId: 42, projectCode: "PROJA",
      prState: "draft", kindChanges: [KIND_CHANGE],
    }));

    await waitFor(() => expect(names(capturedWorkflowsProps.rxworkflows)).toEqual(["deploy"]));
    expect(capturedWorkflowsProps.rxworkflows[0].content).toContain("# typed meanwhile");
  });
});
