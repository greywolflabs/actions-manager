/**
 * The project page names its project by id on every request (#2240).
 *
 * Project names are unique per owner only, so the page sends the id it holds as
 * X-Project-Id. The page load itself has no id yet - it loads by the name in the
 * URL - so the project list hands the id over in the navigation state.
 */
import React from "react";
import { render, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";

const mockNavigate = vi.fn();
const mockUseParams = vi.fn();
let mockLocationState: unknown = null;

vi.mock(
  "react-router",
  () => ({
    useNavigate: () => mockNavigate,
    useParams: () => mockUseParams(),
    useLocation: () => ({
      pathname: "/project/alice/Infra",
      search: "",
      hash: "",
      state: mockLocationState,
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
          <span key={p.id ?? p.project_id} data-testid="project-name-in-list">
            {p.project_name ?? p.name}
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
import { projectHintHeaders } from "./api/projectHint";

import type { Mock } from "vitest";

const userDetails = {
  avatar_url: "https://example.com/avatar.png",
  github_user: "alice",
  account_type: "free",
  github_account_type: "User" as const,
};

const loaded = (projectId: number) => ({
  project_name: "Infra",
  project_id: projectId,
  project_code: "INF",
  selected_repos: [],
  workflows: [],
  rxworkflows: [],
  branch_regex: "",
  branch_option: "default",
  branch_max_age_days: 30,
  reusable_workflows_enabled: false,
  use_prefix: false,
  project_type: "standard",
  pr_state: "new",
});

describe("ProjectMgmt – requests name the page's project by id", () => {
  let headersAtLoad: Record<string, string> | null;

  beforeEach(() => {
    vi.clearAllMocks();
    mockLocationState = null;
    headersAtLoad = null;
    mockUseParams.mockReturnValue({ user: "alice", projectName: "Infra" });
    (fetchProjects as Mock).mockResolvedValue([]);
    (getSecrets as Mock).mockResolvedValue([]);
    (getEnvVars as Mock).mockResolvedValue([]);
    (getProjectPRStatus as Mock).mockResolvedValue({
      project_state: "new", open_prs: 0, merged_prs: 0, total_prs: 0,
    });
  });

  const loadAs = (projectId: number) => {
    (loadProject as Mock).mockImplementation(async () => {
      headersAtLoad = projectHintHeaders();
      return loaded(projectId);
    });
  };

  test("the page load carries the id the project list passed", async () => {
    mockLocationState = { projectId: 7 };
    loadAs(7);

    render(<ProjectMgmt userDetails={userDetails} onLogout={vi.fn()} />);

    await waitFor(() => expect(loadProject).toHaveBeenCalled());
    expect(headersAtLoad).toEqual({ "X-Project-Id": "7" });
  });

  test("once loaded, requests carry the loaded project's id", async () => {
    loadAs(9);

    render(<ProjectMgmt userDetails={userDetails} onLogout={vi.fn()} />);

    await waitFor(() => expect(projectHintHeaders()).toEqual({ "X-Project-Id": "9" }));
    expect(headersAtLoad).toEqual({});
  });

  test("going back to the project list stops naming the project", async () => {
    mockLocationState = { projectId: 7 };
    loadAs(7);

    const { rerender } = render(<ProjectMgmt userDetails={userDetails} onLogout={vi.fn()} />);
    await waitFor(() => expect(projectHintHeaders()).toEqual({ "X-Project-Id": "7" }));

    // The list renders this same component, so nothing unmounts.
    mockUseParams.mockReturnValue({ user: "alice", projectName: undefined });
    mockLocationState = null;
    rerender(<ProjectMgmt userDetails={userDetails} onLogout={vi.fn()} />);

    await waitFor(() => expect(projectHintHeaders()).toEqual({}));
  });

  test("leaving the page stops naming the project", async () => {
    mockLocationState = { projectId: 7 };
    loadAs(7);

    const { unmount } = render(<ProjectMgmt userDetails={userDetails} onLogout={vi.fn()} />);
    await waitFor(() => expect(projectHintHeaders()).toEqual({ "X-Project-Id": "7" }));

    unmount();

    expect(projectHintHeaders()).toEqual({});
  });
});
