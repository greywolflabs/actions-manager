/**
 * Every ProjectMgmt view renders under the one app header. The Projects list
 * sits in its container, with its plan usage beside New Project rather than in
 * the header. A project workspace gets the same header, in the same place,
 * with no logo, account menu or Back button of its own, and marks the section
 * the project belongs to; that tab is the way back.
 */
import React from "react";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "@testing-library/jest-dom";
import { Link, MemoryRouter, Route, Routes } from "react-router";
import type { Mock } from "vitest";

vi.mock("./api/projects", () => ({
  __esModule: true,
  fetchPendingDelivery: vi.fn().mockResolvedValue({ pending_delivery_repos: [], pending_delivery_known: true }),
  fetchProjects: vi.fn().mockResolvedValue([]),
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
vi.mock("./api/secrets", () => ({ getSecrets: vi.fn().mockResolvedValue([]) }));
vi.mock("./api/envVars", () => ({ getEnvVars: vi.fn().mockResolvedValue([]) }));
vi.mock("./api/pullRequests", () => ({
  getProjectPRStatus: vi.fn().mockResolvedValue({ project_state: "draft", open_prs: 0, merged_prs: 0, total_prs: 0 }),
}));
vi.mock("./api/codeowners", () => ({ getProjectCodeowners: vi.fn().mockResolvedValue(null) }));
vi.mock("./api/managedFiles", () => ({
  fetchManagedFiles: vi.fn().mockResolvedValue({ kinds: [], owned_count: 0, limit: 3, limit_message: null }),
  fetchTakenRepos: vi.fn().mockResolvedValue({}),
}));
vi.mock("./api/actionsProjects", () => ({ listActionsProjects: vi.fn().mockResolvedValue([]) }));
vi.mock("./api/actionGroups", () => ({ listActionGroups: vi.fn().mockResolvedValue([]) }));

vi.mock("./components/ProjectList", () => ({
  default: function ProjectList({ planUsage }: { planUsage?: React.ReactNode }) {
    return (
      <div data-testid="project-list">
        {planUsage}
        <Link to="/project/alice/payments">Open payments</Link>
      </div>
    );
  },
}));

const { stub, sidebar } = vi.hoisted(() => ({
  stub: (testId: string) => () => ({ default: function Stub() { return <div data-testid={testId} />; } }),
  // The section the project opens on, and the way a user switches it.
  sidebar: { props: null as { activeSection?: string; onSectionChange?: (section: string) => void } | null },
}));
vi.mock("./components/UserAvatar", stub("user-avatar"));
vi.mock("./components/PlanUsagePill", stub("plan-usage-pill"));
vi.mock("./components/Sidebar", () => ({
  default: function Sidebar(props: NonNullable<typeof sidebar.props>) {
    sidebar.props = props;
    return <div data-testid="sidebar" />;
  },
}));
vi.mock("./components/RepositoriesAndBranches", stub("repositories-and-branches"));
vi.mock("./components/DeployEnvironments", stub("deploy-environments"));
vi.mock("./components/EnvVars", stub("env-vars"));
vi.mock("./components/Secrets", stub("secrets"));
vi.mock("./components/UnifiedWorkflows", stub("unified-workflows"));
vi.mock("./components/RulesetManager", stub("rulesets"));
vi.mock("./components/CodeownersManager", stub("codeowners"));
vi.mock("./components/SaveResultsModal", stub("save-results-modal"));
vi.mock("./components/DeleteProjectModal", stub("delete-project-modal"));
vi.mock("./components/DangerZone", stub("danger-zone"));
vi.mock("./components/ProjectMembers", stub("project-members"));
vi.mock("./components/DriftDetection", stub("drift-detection"));
vi.mock("./components/PRStatusPanel", stub("pr-status-panel"));
vi.mock("./components/PRHistoryPanel", stub("pr-history-panel"));
vi.mock("./components/CreatePRModal", stub("create-pr-modal"));
vi.mock("./components/LinkedWorkflowsModal", stub("linked-workflows-modal"));
vi.mock("./components/ProjectColorSelector", stub("project-color-selector"));

import ProjectMgmt from "./ProjectMgmt";
import { loadProject } from "./api/projects";
import type { UserDetails } from "./api/user";

const TAB_IDS = ["projects-nav-button", "managed-files-nav-button", "actions-projects-nav-button"];

const userDetails: UserDetails = {
  avatar_url: "https://example.com/avatar.png",
  github_user: "alice",
  account_type: "free",
};

function payments(projectType: "standard" | "managed_files") {
  return {
    project_name: "payments",
    project_id: 7,
    project_code: "PAY",
    selected_repos: [],
    workflows: [],
    rxworkflows: [],
    project_type: projectType,
    file_kind: projectType === "managed_files" ? "codeowners" : null,
    pr_state: "draft",
  };
}

// null: the user's details haven't loaded yet.
function page(path: string, details: UserDetails | null = userDetails): React.ReactElement {
  const element = <ProjectMgmt userDetails={details ?? undefined} onLogout={vi.fn()} />;
  return (
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/project/:user" element={element} />
        <Route path="/project/:user/managed-files" element={<div data-testid="managed-files-page" />} />
        <Route path="/project/:user/:projectName" element={element} />
      </Routes>
    </MemoryRouter>
  );
}

function currentTabs(header: HTMLElement): string[] {
  return TAB_IDS.filter((id) => within(header).getByTestId(id).hasAttribute("aria-current"));
}

describe("ProjectMgmt – the Projects list in the app header", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  test("renders the list in the app header, with Projects as the current tab and the account menu", async () => {
    render(page("/project/alice"));

    const header = await screen.findByTestId("app-header");
    expect(within(header).getByTestId("projects-nav-button")).toHaveAttribute("aria-current", "page");
    expect(within(header).getByTestId("user-avatar")).toBeInTheDocument();
    expect(header.firstElementChild).not.toHaveClass("max-w-[1400px]");
    expect(screen.getByRole("main")).toContainElement(await screen.findByTestId("project-list"));
  });

  test("hands the plan usage to the list, beside New Project, once the user's details load", async () => {
    const { rerender } = render(page("/project/alice", null));

    const list = await screen.findByTestId("project-list");
    expect(screen.queryByTestId("plan-usage-pill")).not.toBeInTheDocument();
    expect(screen.queryByTestId("user-avatar")).not.toBeInTheDocument();

    rerender(page("/project/alice"));

    expect(within(list).getByTestId("plan-usage-pill")).toBeInTheDocument();
    expect(within(screen.getByTestId("app-header")).queryByTestId("plan-usage-pill")).not.toBeInTheDocument();
  });
});

describe("ProjectMgmt – a project workspace in the app header", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  test("a workflow project has the same header, Projects current, and one account menu", async () => {
    (loadProject as Mock).mockResolvedValue(payments("standard"));
    render(page("/project/alice/payments"));

    const header = await screen.findByTestId("app-header");
    await waitFor(() => expect(currentTabs(header)).toEqual(["projects-nav-button"]));
    expect(within(header).getByTestId("projects-nav-button")).toHaveAttribute("aria-current", "true");
    expect(header.firstElementChild).not.toHaveClass("max-w-[1400px]");

    expect(screen.getAllByTestId("app-header")).toHaveLength(1);
    expect(screen.getAllByTestId("user-avatar")).toHaveLength(1);
    expect(header).toContainElement(screen.getByTestId("user-avatar"));
    expect(screen.queryByRole("button", { name: /Back to/ })).not.toBeInTheDocument();
  });

  test("a Managed Files project marks Managed Files current, and no tab is current while it loads", async () => {
    let finishLoad!: (project: ReturnType<typeof payments>) => void;
    (loadProject as Mock).mockReturnValue(new Promise((resolve) => { finishLoad = resolve; }));
    render(page("/project/alice/payments"));

    const header = await screen.findByTestId("app-header");
    await waitFor(() => expect(loadProject).toHaveBeenCalled());
    expect(currentTabs(header)).toEqual([]);

    finishLoad(payments("managed_files"));

    await waitFor(() => expect(currentTabs(header)).toEqual(["managed-files-nav-button"]));
    expect(screen.queryByRole("button", { name: /Back to/ })).not.toBeInTheDocument();

    // The current tab is the way back to the project's section.
    await userEvent.setup().click(within(header).getByTestId("managed-files-nav-button"));
    expect(await screen.findByTestId("managed-files-page")).toBeInTheDocument();
  });

  test("the header stays mounted, and in place, from the list into a project", async () => {
    (loadProject as Mock).mockResolvedValue(payments("standard"));
    render(page("/project/alice"));
    const header = await screen.findByTestId("app-header");
    const row = header.firstElementChild?.className;

    await userEvent.setup().click(await screen.findByRole("link", { name: "Open payments" }));

    await waitFor(() => expect(currentTabs(header)).toEqual(["projects-nav-button"]));
    expect(screen.getByTestId("sidebar")).toBeInTheDocument();
    expect(screen.getByTestId("app-header")).toBe(header);
    // Same size and place: the header doesn't jump when a project opens.
    expect(header.firstElementChild?.className).toBe(row);
  });

  // The Back button used to clear the project on the way out; the header's tabs
  // must too, or the next project opens where this one was left.
  test("leaving through the header clears the project, so the next one opens fresh", async () => {
    const user = userEvent.setup();
    let finishSecondLoad!: (project: ReturnType<typeof payments>) => void;
    (loadProject as Mock)
      .mockResolvedValueOnce(payments("managed_files"))
      .mockReturnValueOnce(new Promise((resolve) => { finishSecondLoad = resolve; }));
    render(page("/project/alice/payments"));
    const header = await screen.findByTestId("app-header");
    await waitFor(() => expect(currentTabs(header)).toEqual(["managed-files-nav-button"]));

    act(() => sidebar.props?.onSectionChange?.("pr-history"));
    await waitFor(() => expect(sidebar.props?.activeSection).toBe("pr-history"));

    await user.click(within(header).getByTestId("projects-nav-button"));
    await user.click(await screen.findByRole("link", { name: "Open payments" }));
    await waitFor(() => expect(loadProject).toHaveBeenCalledTimes(2));

    // Nothing of the previous project's: no section current while this one loads,
    // and it opens on its files rather than on PR Campaigns.
    expect(currentTabs(header)).toEqual([]);
    expect(sidebar.props?.activeSection).toBe("workflows");

    finishSecondLoad(payments("standard"));
    await waitFor(() => expect(currentTabs(header)).toEqual(["projects-nav-button"]));
  });
});
