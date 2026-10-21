import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "@testing-library/jest-dom";

// Deleting a project from its Danger Zone. Keeping its files, or removing
// files ActionsManager has no record of delivering, deletes it at once and the
// page leaves it. Removing delivered files opens their pull requests instead:
// the project stays, Deleting and read-only, and the page shows that and the
// pull requests at once. Neither needs a refresh.

const mockNavigate = vi.fn();
const mockUseParams = vi.fn();

vi.mock("react-router", () => ({
  useNavigate: () => mockNavigate,
  useParams: () => mockUseParams(),
  useLocation: () => ({ pathname: "/", search: "", hash: "", state: null, key: "test" }),
  Link: ({ to, children, ...rest }: any) => (
    <a href={to} {...rest}>
      {children}
    </a>
  ),
}));

vi.mock("./api/projects", () => ({
  __esModule: true,
  fetchProjects: vi.fn(),
  loadProject: vi.fn(),
  linkReusableWorkflow: vi.fn(),
  unlinkReusableWorkflow: vi.fn(),
  updateProjectColor: vi.fn(),
  exportProjectBackup: vi.fn(),
}));

vi.mock("./api/projectDeletion", () => ({
  deleteProjectEnhanced: vi.fn(),
  deletionErrorMessage: () => "refused",
}));
vi.mock("./api/handlers", () => ({ handleSaveProjectWithModal: vi.fn() }));
vi.mock("./api/secrets", () => ({ getSecrets: vi.fn() }));
vi.mock("./api/envVars", () => ({ getEnvVars: vi.fn() }));
vi.mock("./api/pullRequests", () => ({
  getProjectPRStatus: vi.fn(),
  getPRCampaigns: vi.fn(),
  mergePullRequest: vi.fn(),
  closePullRequest: vi.fn(),
  previewCampaignRollback: vi.fn(),
  createCampaignRollback: vi.fn(),
}));
vi.mock("./api/actionsProjects", () => ({ listActionsProjects: vi.fn().mockResolvedValue([]) }));
vi.mock("./api/actionGroups", () => ({ listActionGroups: vi.fn().mockResolvedValue([]) }));

vi.mock("./components/Sidebar", () => ({
  default: function Sidebar(props: any) {
    return (
      <div data-testid="sidebar" data-read-only={String(props.isReadOnly)}>
        <button type="button" onClick={() => props.onSectionChange("danger-zone")}>
          Open Danger Zone
        </button>
      </div>
    );
  },
}));
// The dialog's own choices are DeleteProjectModal.test's; here it confirms one.
vi.mock("./components/DeleteProjectModal", () => ({
  default: function DeleteProjectModal(props: any) {
    if (!props.isOpen) return null;
    return (
      <button
        type="button"
        onClick={() => props.onConfirmDelete({ removeFiles: true, deleteSecrets: false, deleteDeploymentEnvironments: true })}
      >
        Delete and remove files
      </button>
    );
  },
}));
vi.mock("./components/UnifiedWorkflows", () => ({
  default: function UnifiedWorkflows(props: any) {
    return <div data-testid="unified-workflows" data-read-only={String(props.isReadOnly)} />;
  },
}));
vi.mock("./components/ProjectList", () => ({
  default: function ProjectList() {
    return <div data-testid="project-list">ProjectList</div>;
  },
}));

const { stub, nothing } = vi.hoisted(() => ({
  stub: (name: string) => () => ({ default: function Stub() { return <div data-testid={name} />; } }),
  nothing: () => ({ default: function Nothing() { return null; } }),
}));
vi.mock("./components/RepositoriesAndBranches", stub("repositories-and-branches"));
vi.mock("./components/DeployEnvironments", stub("deploy-environments"));
vi.mock("./components/EnvVars", stub("env-vars"));
vi.mock("./components/Secrets", stub("secrets"));
vi.mock("./components/RulesetManager", stub("rulesets"));
vi.mock("./components/CodeownersManager", stub("codeowners"));
vi.mock("./components/UserAvatar", stub("user-avatar"));
vi.mock("./components/PlanUsagePill", stub("plan-usage-pill"));
// The app header's tabs need a real router, and this file mocks react-router.
vi.mock("./components/AppHeader", () => import("./test/appHeaderStub"));
vi.mock("./components/SaveResultsModal", nothing);
vi.mock("./components/ProjectMembers", nothing);
vi.mock("./components/DriftDetection", nothing);
vi.mock("./components/PRStatusPanel", nothing);
vi.mock("./components/CreatePRModal", nothing);
vi.mock("./components/LinkedWorkflowsModal", nothing);
vi.mock("./components/ProjectColorSelector", nothing);

import ProjectMgmt from "./ProjectMgmt";
import { fetchProjects, loadProject } from "./api/projects";
import { deleteProjectEnhanced } from "./api/projectDeletion";
import { getSecrets } from "./api/secrets";
import { getEnvVars } from "./api/envVars";
import { getPRCampaigns, getProjectPRStatus } from "./api/pullRequests";

const PROJECT = "doomed";
const CAMPAIGN = `Delete project ${PROJECT}`;

// What the server holds: the project's pr_state, the caller's role on it
// (capped at project_viewer while it is being deleted) and its campaigns.
let server: { prState: string; role: string; deleting: boolean };

const removalPR = {
  pr_id: 1,
  repo_name: "org/app",
  pr_number: 7,
  pr_url: "https://github.com/org/app/pull/7",
  branch_name: "actions-manager/dmd/7-main",
  target_branch: "main",
  pr_state: "open",
  title: CAMPAIGN,
  author: "testuser",
  actor: "testuser",
  body: null,
  workflow_names: "ci",
  created_at: "2026-10-01T06:00:00+00:00",
  updated_at: "2026-10-01T06:00:00+00:00",
  merged_at: null,
  closed_at: null,
  source_project_name: null,
};

const campaigns = () => {
  const removal = server.deleting
    ? [{
        campaign_id: "campaign-1",
        campaign_name: CAMPAIGN,
        campaign_status: "open",
        project_name: PROJECT,
        project_code: "DMD",
        created_by: "testuser",
        created_at: "2026-10-01T06:00:00+00:00",
        updated_at: "2026-10-01T06:00:00+00:00",
        completed_at: null,
        target_branches: ["main"],
        workflow_names: ["ci"],
        repositories: ["org/app"],
        open_count: 1,
        merged_count: 0,
        closed_count: 0,
        failed_count: 0,
        completion_percentage: 0,
        pull_requests: [removalPR],
      }]
    : [];
  return {
    campaigns: removal,
    pull_requests: removal.flatMap((c) => c.pull_requests),
    total_campaigns: removal.length,
    active_campaigns: removal.length,
    completed_campaigns: 0,
    open_prs: removal.length,
    merged_prs: 0,
    closed_prs: 0,
    repositories_affected: removal.length,
  };
};

const renderProjectMgmt = () =>
  render(
    <ProjectMgmt
      userDetails={{
        avatar_url: "https://example.com/avatar.png",
        github_user: "testuser",
        account_type: "free",
        github_account_type: "User",
        workspace_role: "admin",
      }}
      onLogout={vi.fn()}
    />
  );

const confirmDeletion = async (user: ReturnType<typeof userEvent.setup>) => {
  await user.click(await screen.findByRole("button", { name: "Open Danger Zone" }));
  await user.click(await screen.findByRole("button", { name: /Delete Project/ }));
  await user.click(await screen.findByRole("button", { name: "Delete and remove files" }));
};

describe("ProjectMgmt deleting its project", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    Element.prototype.scrollIntoView = vi.fn() as unknown as Element["scrollIntoView"];
    server = { prState: "synced", role: "project_admin", deleting: false };
    mockUseParams.mockReturnValue({ user: "testuser", projectName: PROJECT });
    vi.mocked(fetchProjects).mockResolvedValue([]);
    vi.mocked(loadProject).mockImplementation(async () => ({
      project_name: PROJECT,
      updated_at: "2026-10-01T00:00:00Z",
      selected_repos: ["org/app"],
      workflows: [],
      rxworkflows: [],
      branch_regex: "",
      project_code: "DMD",
      project_id: 123,
      project_type: "standard",
      use_prefix: true,
      repository_visibility_scope: "public",
      pr_state: server.prState as "synced",
      caller_project_role: server.role,
    }));
    vi.mocked(getSecrets).mockResolvedValue([]);
    vi.mocked(getEnvVars).mockResolvedValue([]);
    vi.mocked(getProjectPRStatus).mockImplementation(async () => ({
      project_state: server.prState,
      pull_requests: [],
      closed_prs: 0,
      open_prs: server.deleting ? 1 : 0,
      merged_prs: 0,
      total_prs: server.deleting ? 1 : 0,
    }));
    vi.mocked(getPRCampaigns).mockImplementation(async () => campaigns() as any);
  });

  test("starting removal shows Deleting state without refresh", async () => {
    const user = userEvent.setup();
    vi.mocked(deleteProjectEnhanced).mockImplementation(async () => {
      server = { prState: "deleting", role: "project_viewer", deleting: true };
      return {
        message: "Project is being deleted",
        project_deleted: false,
        pr_state: "deleting",
        campaign_id: 1,
        prs_created: 1,
        results: {},
        details: { project_deleted: false, github_resources_deleted: [], errors: [] },
      };
    });
    renderProjectMgmt();
    await waitFor(() => expect(screen.getByTestId("sidebar")).toHaveAttribute("data-read-only", "false"));
    const listLoadsBefore = vi.mocked(fetchProjects).mock.calls.length;

    await confirmDeletion(user);

    expect(vi.mocked(deleteProjectEnhanced)).toHaveBeenCalledWith(PROJECT, {
      removeFiles: true,
      deleteSecrets: false,
      deleteDeploymentEnvironments: true,
    });
    expect(await screen.findByTestId("project-deleting-banner")).toBeInTheDocument();
    expect(await screen.findByText(`Campaign: ${CAMPAIGN}`)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("sidebar")).toHaveAttribute("data-read-only", "true"));
    expect(screen.queryByTestId("project-list")).not.toBeInTheDocument();
    // The list's badge is re-read too, so leaving for the list shows it Deleting.
    await waitFor(() => expect(vi.mocked(fetchProjects).mock.calls.length).toBeGreaterThan(listLoadsBefore));
  });

  test("nothing delivered leaves the page", async () => {
    const user = userEvent.setup();
    vi.mocked(deleteProjectEnhanced).mockResolvedValue({
      message: "✅ Project deletion completed",
      project_deleted: true,
      pr_state: null,
      campaign_id: null,
      prs_created: 0,
      results: {},
      details: { project_deleted: true, github_resources_deleted: [], errors: [] },
    });
    renderProjectMgmt();
    await screen.findByTestId("sidebar");
    const loadsBefore = vi.mocked(loadProject).mock.calls.length;

    await confirmDeletion(user);

    expect(await screen.findByTestId("project-list")).toBeInTheDocument();
    expect(screen.queryByTestId("project-deleting-banner")).not.toBeInTheDocument();
    expect(vi.mocked(loadProject).mock.calls.length).toBe(loadsBefore);
    await waitFor(() => expect(vi.mocked(fetchProjects).mock.calls.length).toBeGreaterThan(1));
  });

  test("a refused deletion leaves the project as it was", async () => {
    const user = userEvent.setup();
    vi.mocked(deleteProjectEnhanced).mockRejectedValue({ response: { status: 409 } });
    renderProjectMgmt();
    await screen.findByTestId("sidebar");

    await confirmDeletion(user);

    expect(screen.queryByTestId("project-deleting-banner")).not.toBeInTheDocument();
    expect(screen.queryByTestId("project-list")).not.toBeInTheDocument();
    expect(screen.getByTestId("sidebar")).toHaveAttribute("data-read-only", "false");
  });
});
