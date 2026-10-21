/**
 * A Managed Files project on the project page (#2367).
 *
 * The page hosts it with what it holds: its one file, in the editor its
 * kind's definition names, with the kind's paths; Repositories & Branches,
 * with the repositories other projects of the kind use locked; PR Campaigns;
 * and the Project Configs a project has. No workflows, build metrics,
 * environments, variables, secrets, rulesets, naming mode or color. It goes
 * back to the Managed Files section, and its file is delivered and updated
 * the way any project's is, with no reload.
 */
import React from "react";
import { render, screen, waitFor, act, fireEvent } from "@testing-library/react";
import "@testing-library/jest-dom";

const mockNavigate = vi.fn();
const mockUseParams = vi.fn();

// Captured so tests can drive the section nav the way a user would, without
// rendering the real Sidebar.
let capturedOnSectionChange: ((section: string) => void) | null = null;
let capturedSidebarProps: any = null;
let capturedEditorProps: any = null;
let capturedReposProps: any = null;
let capturedPRModalProps: any = null;
let capturedHistoryProps: any = null;

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
  fetchPendingDelivery: vi.fn().mockResolvedValue({ pending_delivery_repos: [], pending_delivery_known: true }),
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
    capturedSidebarProps = props;
    return <div data-testid="sidebar" />;
  },
}));

vi.mock("./components/RepositoriesAndBranches", () => ({
  default: function RepositoriesAndBranches(props: any) {
    capturedReposProps = props;
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

vi.mock("./components/UnifiedWorkflows", () => ({
  default: function UnifiedWorkflows() {
    return <div data-testid="unified-workflows" />;
  },
}));

vi.mock("./components/RulesetManager", () => ({
  default: function RulesetManager() {
    return <div data-testid="rulesets" />;
  },
}));

// The kind's editor, as FILE_KIND_UI names it: captured so a save can be
// reported the way the real editor reports it.
vi.mock("./components/CodeownersManager", () => ({
  default: function CodeownersManager(props: any) {
    capturedEditorProps = props;
    return <div data-testid="codeowners" />;
  },
}));

vi.mock("./api/managedFiles", () => ({
  fetchManagedFiles: vi.fn(),
  fetchTakenRepos: vi.fn().mockResolvedValue({}),
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
  default: function PRHistoryPanel(props: any) {
    capturedHistoryProps = props;
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
    return <div data-testid="project-color-selector" />;
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
import { fetchManagedFiles } from "./api/managedFiles";

import type { Mock } from "vitest";

const userDetails = {
  avatar_url: "https://example.com/avatar.png",
  github_user: "alice",
  account_type: "free",
  github_account_type: "User" as const,
  workspace_role: "admin",
};

const PATHS = [".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"];

const record = (overrides: Record<string, unknown> = {}) => ({
  id: 1, project_id: 42, content: "* @acme/owners\n", file_path: ".github/CODEOWNERS", git_hash: "a".repeat(40),
  status: "synced_with_github", last_modified_by: "alice", created_at: null, updated_at: null, ...overrides,
});

function projectPayload(prState: string) {
  return {
    project_name: "owners",
    project_id: 42,
    project_code: "OWNERS",
    selected_repos: ["acme/api", "acme/web"],
    workflows: [],
    rxworkflows: [],
    branch_regex: "",
    branch_option: "default",
    branch_max_age_days: 30,
    reusable_workflows_enabled: false,
    use_prefix: true,
    project_type: "managed_files",
    file_kind: "codeowners",
    project_color: null,
    pr_state: prState,
    codeowners_drifted: false,
  };
}

async function renderManagedProject(prState: string, codeowners = record()) {
  vi.clearAllMocks();
  capturedOnSectionChange = null;
  capturedSidebarProps = null;
  capturedEditorProps = null;
  capturedReposProps = null;
  capturedPRModalProps = null;
  mockUseParams.mockReturnValue({ user: "alice", projectName: "owners" });
  (fetchProjects as Mock).mockResolvedValue([]);
  (loadProject as Mock).mockResolvedValue(projectPayload(prState));
  (getSecrets as Mock).mockResolvedValue([]);
  (getEnvVars as Mock).mockResolvedValue([]);
  (getProjectPRStatus as Mock).mockResolvedValue({ project_state: prState, open_prs: 0, merged_prs: 0, total_prs: 0 });
  (getProjectCodeowners as Mock).mockResolvedValue(codeowners);
  (fetchManagedFiles as Mock).mockResolvedValue({
    kinds: [{ kind: "codeowners", label: "CODEOWNERS", paths: PATHS }],
    owned_count: 1,
    limit: 3,
    limit_message: null,
  });

  render(<ProjectMgmt userDetails={userDetails} onLogout={vi.fn()} />);
  await waitFor(() => expect(loadProject).toHaveBeenCalled());
  await waitFor(() => expect(capturedEditorProps?.codeowners).toEqual(codeowners));
}

describe("ProjectMgmt – a Managed Files project", () => {
  test("opens on its file, in its kind's editor with the kind's paths", async () => {
    await renderManagedProject("synced");

    expect(screen.getByTestId("file-kind-editor")).toBeInTheDocument();
    expect(screen.queryByTestId("unified-workflows")).not.toBeInTheDocument();
    expect(capturedEditorProps.paths).toEqual(PATHS);
    expect(capturedEditorProps.projectId).toBe(42);
    expect(capturedEditorProps.selectedRepos).toEqual(["acme/api", "acme/web"]);
  });

  test("the sidebar names the project's file and knows its type", async () => {
    await renderManagedProject("synced");

    await waitFor(() => expect(capturedSidebarProps.fileKindLabel).toBe("CODEOWNERS"));
    expect(capturedSidebarProps.projectType).toBe("managed_files");
    expect(capturedSidebarProps.fileKind).toBe("codeowners");
  });

  // The app header's Managed Files tab is the way back (ProjectMgmt.appHeader.test.tsx).
  test("has no Back button of its own", async () => {
    await renderManagedProject("synced");

    expect(screen.queryByRole("button", { name: /Back to/ })).not.toBeInTheDocument();
  });

  test("Project Info shows the file, and no naming mode or color", async () => {
    await renderManagedProject("synced");

    act(() => capturedOnSectionChange!("project-info"));

    expect(await screen.findByTestId("project-info-file-kind")).toHaveTextContent("CODEOWNERS");
    expect(screen.getByText("Managed Files Project")).toBeInTheDocument();
    expect(screen.queryByText(/Resource Naming Mode/)).not.toBeInTheDocument();
    expect(screen.queryByTestId("project-color-selector")).not.toBeInTheDocument();
  });

  test("Repositories & Branches knows the kind and leaves out what serves workflows", async () => {
    await renderManagedProject("synced");

    act(() => capturedOnSectionChange!("repos-and-branches"));

    await waitFor(() => expect(capturedReposProps?.fileKind).toBe("codeowners"));
    for (const panel of ["deploy-environments", "env-vars", "secrets", "rulesets"]) {
      expect(screen.queryByTestId(panel)).not.toBeInTheDocument();
    }
  });

  test("Build Metrics, reached directly, falls back to the file", async () => {
    await renderManagedProject("synced");

    act(() => capturedOnSectionChange!("build-metrics"));

    expect(await screen.findByTestId("file-kind-editor")).toBeInTheDocument();
  });

  test("Commit Locally enables a campaign at once, with no reload", async () => {
    await renderManagedProject("synced");
    const button = await screen.findByTestId("create-pull-requests-button");
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("title", "CODEOWNERS is synced with GitHub – commit a change locally first");

    act(() => capturedEditorProps.onSaved(record({ status: "committed_locally" }), "draft"));

    await waitFor(() => expect(button).toBeEnabled());
    await waitFor(() => expect(capturedEditorProps.codeowners.status).toBe("committed_locally"));
    expect(loadProject).toHaveBeenCalledTimes(1);
  });

  test("a campaign carries only the file, and shows it under review once it opens", async () => {
    await renderManagedProject("draft", record({ status: "committed_locally" }));
    act(() => screen.getByTestId("create-pull-requests-button").click());
    await waitFor(() => expect(capturedPRModalProps).not.toBeNull());
    expect(capturedPRModalProps.workflows).toEqual([]);
    expect(capturedPRModalProps.reusableWorkflows).toEqual([]);
    expect(capturedPRModalProps.codeowners).toEqual({ filePath: ".github/CODEOWNERS", status: "committed_locally" });
    (getProjectCodeowners as Mock).mockResolvedValue(record({ status: "under_review" }));

    act(() => capturedPRModalProps.onSuccess([], [], [], true, ["acme/api", "acme/web"]));

    await waitFor(() => expect(capturedEditorProps.codeowners.status).toBe("under_review"));
    expect(loadProject).toHaveBeenCalledTimes(1);
  });

  test("found deleted, it goes back to the Managed Files section", async () => {
    // Its last removal pull request merged before the page opened: the load's PR status read finds it gone.
    (getProjectPRStatus as Mock).mockResolvedValue({ project_state: "deleted", open_prs: 0, merged_prs: 1, total_prs: 1 });
    mockUseParams.mockReturnValue({ user: "alice", projectName: "owners" });
    (loadProject as Mock).mockResolvedValue(projectPayload("deleting"));
    (fetchManagedFiles as Mock).mockResolvedValue({ kinds: [], owned_count: 0, limit: 3, limit_message: null });
    mockNavigate.mockClear();

    render(<ProjectMgmt userDetails={userDetails} onLogout={vi.fn()} />);

    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith("/project/alice/managed-files"));
  });

  test("says the file could not be read, and reads it again on request", async () => {
    mockUseParams.mockReturnValue({ user: "alice", projectName: "owners" });
    (loadProject as Mock).mockResolvedValue(projectPayload("synced"));
    (getProjectPRStatus as Mock).mockResolvedValue({ project_state: "synced", open_prs: 0, merged_prs: 0, total_prs: 0 });
    (fetchManagedFiles as Mock).mockResolvedValue({
      kinds: [{ kind: "codeowners", label: "CODEOWNERS", paths: PATHS }], owned_count: 1, limit: 3, limit_message: null,
    });
    (getProjectCodeowners as Mock).mockReset().mockRejectedValueOnce(new Error("offline")).mockResolvedValue(record());
    capturedEditorProps = null;

    render(<ProjectMgmt userDetails={userDetails} onLogout={vi.fn()} />);

    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't load this project's file.");
    expect(capturedEditorProps).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(capturedEditorProps?.codeowners).toEqual(record()));
  });

  test("a merge in PR Campaigns shows the file synced, with no reload", async () => {
    await renderManagedProject("open", record({ status: "under_review" }));
    act(() => capturedOnSectionChange!("pr-history"));
    await waitFor(() => expect(capturedHistoryProps).not.toBeNull());
    (getProjectCodeowners as Mock).mockResolvedValue(record({ status: "synced_with_github" }));
    (getProjectPRStatus as Mock).mockResolvedValue({ project_state: "synced", open_prs: 0, merged_prs: 1, total_prs: 1 });

    await act(async () => { await capturedHistoryProps.onCampaignStateRefresh(); });
    act(() => capturedOnSectionChange!("workflows"));

    await waitFor(() => expect(capturedEditorProps.codeowners.status).toBe("synced_with_github"));
  });

  test("reads no secrets or variables: it has neither", async () => {
    await renderManagedProject("synced");

    expect(getSecrets).not.toHaveBeenCalled();
    expect(getEnvVars).not.toHaveBeenCalled();
  });

  test("says when its kind cannot be read, and reads it again on request", async () => {
    mockUseParams.mockReturnValue({ user: "alice", projectName: "owners" });
    (loadProject as Mock).mockResolvedValue(projectPayload("synced"));
    (getProjectPRStatus as Mock).mockResolvedValue({ project_state: "synced", open_prs: 0, merged_prs: 0, total_prs: 0 });
    (getProjectCodeowners as Mock).mockResolvedValue(record());
    (fetchManagedFiles as Mock).mockReset().mockRejectedValueOnce(new Error("offline")).mockResolvedValue({
      kinds: [{ kind: "codeowners", label: "CODEOWNERS", paths: PATHS }], owned_count: 1, limit: 3, limit_message: null,
    });
    capturedEditorProps = null;

    render(<ProjectMgmt userDetails={userDetails} onLogout={vi.fn()} />);

    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't load this project's file.");
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(capturedEditorProps?.paths).toEqual(PATHS));
  });
});
