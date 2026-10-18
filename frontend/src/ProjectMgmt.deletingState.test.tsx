import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "@testing-library/jest-dom";

// A project being deleted waits in pr_state "deleting" while its removal pull
// requests are open, read-only to everyone. Merging the last one deletes it;
// closing one calls the deletion off. Each must show without a refresh: the
// page leaves a deleted project, and an editable one stops being read-only.

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

vi.mock("./api/projectDeletion", () => ({ deleteProjectEnhanced: vi.fn() }));
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

// Each stub shows whether the page treats the project as read-only.
vi.mock("./components/Sidebar", () => ({
  default: function Sidebar(props: any) {
    return <div data-testid="sidebar" data-read-only={String(props.isReadOnly)} />;
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
vi.mock("./components/BrandLogo", stub("brand-logo"));
vi.mock("./components/SaveResultsModal", nothing);
vi.mock("./components/DeleteProjectModal", nothing);
vi.mock("./components/ProjectMembers", nothing);
vi.mock("./components/DriftDetection", nothing);
vi.mock("./components/PRStatusPanel", nothing);
vi.mock("./components/CreatePRModal", nothing);
vi.mock("./components/LinkedWorkflowsModal", nothing);
vi.mock("./components/ProjectColorSelector", nothing);

import ProjectMgmt from "./ProjectMgmt";
import { fetchProjects, loadProject } from "./api/projects";
import { getSecrets } from "./api/secrets";
import { getEnvVars } from "./api/envVars";
import { closePullRequest, getPRCampaigns, getProjectPRStatus, mergePullRequest } from "./api/pullRequests";

const PROJECT = "doomed";
const DELETED_DETAIL = "Project was deleted: its removal pull requests merged";

// What the server holds: the project's pr_state, the caller's role on it (capped
// at project_viewer while the project is being deleted) and its removal PR's state.
let server: { prState: string; role: string; removalPRState: string };

const removalPR = {
  pr_id: 1,
  repo_name: "org/repo",
  pr_number: 7,
  pr_url: "https://github.com/org/repo/pull/7",
  branch_name: "actions-manager/dmd/7-main",
  target_branch: "main",
  title: `Remove ${PROJECT}`,
  author: "testuser",
  actor: "testuser",
  body: null,
  workflow_names: "ci.yml",
  created_at: "2026-10-01T06:00:00+00:00",
  updated_at: "2026-10-01T06:00:00+00:00",
  merged_at: null,
  closed_at: null,
  source_project_name: null,
};

const campaigns = (prState: string) => ({
  campaigns: [{
    campaign_id: "campaign-1",
    campaign_name: `Delete ${PROJECT}`,
    campaign_status: prState === "open" ? "open" : "cancelled",
    project_name: PROJECT,
    project_code: "DMD",
    created_by: "testuser",
    created_at: "2026-10-01T06:00:00+00:00",
    updated_at: "2026-10-01T06:00:00+00:00",
    completed_at: null,
    target_branches: ["main"],
    workflow_names: ["ci.yml"],
    repositories: ["org/repo"],
    open_count: prState === "open" ? 1 : 0,
    merged_count: 0,
    closed_count: prState === "closed" ? 1 : 0,
    failed_count: 0,
    completion_percentage: 0,
    pull_requests: [{ ...removalPR, pr_state: prState }],
  }],
  pull_requests: [{ ...removalPR, pr_state: prState }],
  total_campaigns: 1,
  active_campaigns: prState === "open" ? 1 : 0,
  completed_campaigns: 0,
  open_prs: prState === "open" ? 1 : 0,
  merged_prs: 0,
  closed_prs: prState === "closed" ? 1 : 0,
  repositories_affected: 1,
});

const renderProjectMgmt = () =>
  render(
    <ProjectMgmt
      userDetails={{
        avatar_url: "https://example.com/avatar.png",
        github_user: "testuser",
        account_type: "free",
        github_account_type: "User",
        workspace_role: "member",
      }}
      onLogout={vi.fn()}
    />
  );

const openRemovalCampaign = async (user: ReturnType<typeof userEvent.setup>) => {
  await user.click(await screen.findByRole("button", { name: /Open PR Campaigns/i }));
  await screen.findByText(`Campaign: Delete ${PROJECT}`);
};

describe("ProjectMgmt while its project is being deleted", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    Element.prototype.scrollIntoView = vi.fn() as unknown as Element["scrollIntoView"];
    server = { prState: "deleting", role: "project_viewer", removalPRState: "open" };
    mockUseParams.mockReturnValue({ user: "testuser", projectName: PROJECT });
    vi.mocked(fetchProjects).mockResolvedValue([]);
    vi.mocked(loadProject).mockImplementation(async () => ({
      project_name: PROJECT,
      updated_at: "2026-10-01T00:00:00Z",
      selected_repos: ["org/repo"],
      workflows: [],
      rxworkflows: [],
      branch_regex: "",
      project_code: "DMD",
      project_id: 123,
      project_type: "standard",
      use_prefix: true,
      repository_visibility_scope: "public",
      pr_state: server.prState as "deleting",
      caller_project_role: server.role,
    }));
    vi.mocked(getSecrets).mockResolvedValue([]);
    vi.mocked(getEnvVars).mockResolvedValue([]);
    vi.mocked(getProjectPRStatus).mockImplementation(async () => ({
      project_state: server.prState,
      pull_requests: [],
      closed_prs: 0,
      open_prs: server.removalPRState === "open" ? 1 : 0,
      merged_prs: 0,
      total_prs: 1,
    }));
    vi.mocked(getPRCampaigns).mockImplementation(async () => campaigns(server.removalPRState) as any);
  });

  test("a load shows the Deleting banner and the project read-only", async () => {
    renderProjectMgmt();

    expect(await screen.findByTestId("project-deleting-banner")).toHaveTextContent(
      "closing one calls the deletion off"
    );
    expect(screen.getByTestId("unified-workflows")).toHaveAttribute("data-read-only", "true");
    // Not the viewer banner: the caller is read-only because of the deletion, not their role.
    expect(screen.queryByText(/Editing is disabled/i)).not.toBeInTheDocument();
  });

  test("merging the last removal PR leaves the deleted project without a refresh", async () => {
    const user = userEvent.setup();
    vi.mocked(mergePullRequest).mockImplementation(async () => {
      server.removalPRState = "merged";
      return { merged: true, project_deleted: true } as any;
    });
    renderProjectMgmt();
    await openRemovalCampaign(user);
    const loadsBefore = vi.mocked(loadProject).mock.calls.length;

    await user.click(screen.getByRole("button", { name: "Merge" }));
    await user.click(screen.getAllByRole("button", { name: "Merge Open PRs" }).pop()!);

    expect(await screen.findByTestId("project-list")).toBeInTheDocument();
    expect(screen.queryByTestId("project-deleting-banner")).not.toBeInTheDocument();
    expect(vi.mocked(loadProject).mock.calls.length).toBe(loadsBefore);
    await waitFor(() => expect(vi.mocked(fetchProjects).mock.calls.length).toBeGreaterThan(1));
  });

  test("closing a removal PR makes the project editable again without a refresh", async () => {
    const user = userEvent.setup();
    vi.mocked(closePullRequest).mockImplementation(async () => {
      server = { prState: "draft", role: "project_editor", removalPRState: "closed" };
      return { closed: true } as any;
    });
    renderProjectMgmt();
    await openRemovalCampaign(user);
    expect(screen.getByTestId("sidebar")).toHaveAttribute("data-read-only", "true");

    await user.click(screen.getByRole("button", { name: "Close" }));
    await user.click(screen.getAllByRole("button", { name: "Close Open PRs" }).pop()!);

    await waitFor(() => expect(screen.getByTestId("sidebar")).toHaveAttribute("data-read-only", "false"));
    expect(screen.queryByTestId("project-deleting-banner")).not.toBeInTheDocument();
  });

  test("a load that finds the project gone leaves it", async () => {
    vi.mocked(loadProject).mockRejectedValue({ response: { status: 404, data: { detail: DELETED_DETAIL } } });

    renderProjectMgmt();

    expect(await screen.findByTestId("project-list")).toBeInTheDocument();
  });

  test("a PR status refresh that finishes the deletion leaves the project", async () => {
    vi.mocked(getProjectPRStatus).mockResolvedValue({
      project_state: "deleted", pull_requests: [], closed_prs: 0, open_prs: 0, merged_prs: 0, total_prs: 0,
    });

    renderProjectMgmt();

    expect(await screen.findByTestId("project-list")).toBeInTheDocument();
    expect(vi.mocked(getProjectPRStatus)).toHaveBeenCalledWith("testuser", PROJECT, true);
  });
});
