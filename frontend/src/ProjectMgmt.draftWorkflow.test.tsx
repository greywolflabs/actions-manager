/**
 * An updated workflow handed over by Action Usage ("Update and review") reaches the workflows
 * editor as a draft, and is dropped from the history entry once applied so a refresh does not
 * load it again.
 */
import React from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "@testing-library/jest-dom";
import ProjectMgmt from "./ProjectMgmt";
import { fetchProjects, loadProject } from "./api/projects";
import { getSecrets } from "./api/secrets";
import { getEnvVars } from "./api/envVars";
import { getProjectPRStatus } from "./api/pullRequests";





const mockNavigate = vi.fn();
const mockUseParams = vi.fn();
let mockLocationState: unknown = null;

vi.mock(
  "react-router",
  () => ({
    useNavigate: () => mockNavigate,
    useParams: () => mockUseParams(),
    useLocation: () => ({ pathname: "/project/alice/TestProject", search: "", hash: "", state: mockLocationState, key: "test" }),
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

vi.mock("./components/Sidebar", () => ({
  default: function Sidebar(props: { onSectionChange?: (s: string) => void }) {
    return <div data-testid="sidebar" />;
  },
}));

vi.mock("./components/ProjectList", () => ({
  default: function ProjectList() {
    return <div data-testid="project-list" />;
  },
}));

vi.mock("./components/RepositoriesAndBranches", () => ({
  default: function RepositoriesAndBranches() {
    return <div data-testid="repositories-and-branches">Repos</div>;
  },
}));

vi.mock("./components/DeployEnvironments", () => ({
  default: function DeployEnvironments() {
    return <div data-testid="deploy-environments">DeployEnvironments</div>;
  },
}));

vi.mock("./components/EnvVars", () => ({
  default: function EnvVars() {
    return <div data-testid="env-vars">EnvVars</div>;
  },
}));

vi.mock("./components/Secrets", () => ({
  default: function Secrets() {
    return <div data-testid="secrets">Secrets</div>;
  },
}));

vi.mock("./components/UnifiedWorkflows", () => ({
  default: function UnifiedWorkflows(props: any) {
    return (
      <div data-testid="unified-workflows">
        <span data-testid="draft">{props.draftWorkflow ? JSON.stringify(props.draftWorkflow) : "none"}</span>
        <button type="button" data-testid="draft-applied" onClick={() => props.onDraftApplied?.()}>Applied</button>
      </div>
    );
  },
}));

vi.mock("./components/RulesetManager", () => ({
  default: function RulesetManager() {
    return <div data-testid="rulesets">Rulesets</div>;
  },
}));

vi.mock("./components/CodeownersManager", () => ({
  default: function CodeownersManager() {
    return <div data-testid="codeowners">Codeowners</div>;
  },
}));

vi.mock("./components/UserAvatar", () => ({
  default: function UserAvatar() {
    return <div data-testid="user-avatar">UserAvatar</div>;
  },
}));

vi.mock("./components/PlanUsagePill", () => ({
  default: function PlanUsagePill() {
    return <div data-testid="plan-usage-pill">PlanUsagePill</div>;
  },
}));

// The app header's tabs need a real router, and this file mocks react-router.
vi.mock("./components/AppHeader", () => import("./test/appHeaderStub"));

vi.mock("./components/SaveResultsModal", () => ({
  default: function SaveResultsModal() { return null; },
}));

vi.mock("./components/DeleteProjectModal", () => ({
  default: function DeleteProjectModal() { return null; },
}));

vi.mock("./components/DangerZone", () => ({
  default: function DangerZone() { return null; },
}));

vi.mock("./components/ProjectMembers", () => ({
  default: function ProjectMembers() { return null; },
}));

vi.mock("./components/DriftDetection", () => ({
  default: function DriftDetection() { return null; },
}));

vi.mock("./components/PRStatusPanel", () => ({
  default: function PRStatusPanel() { return null; },
}));

vi.mock("./components/PRHistoryPanel", () => ({
  default: function PRHistoryPanel() { return null; },
}));

vi.mock("./components/CreatePRModal", () => ({
  default: function CreatePRModal() { return null; },
}));

vi.mock("./components/LinkedWorkflowsModal", () => ({
  default: function LinkedWorkflowsModal() { return null; },
}));

vi.mock("./components/ProjectColorSelector", () => ({
  default: function ProjectColorSelector() { return null; },
}));

vi.mock("./components/WorkflowImportPanel", () => ({
  __esModule: true,
  WorkflowImportPanel: function WorkflowImportPanel() { return null; },
}));

const DRAFT = { name: "ci", content: "name: ci\n", reusable: false };

const renderPage = () =>
  render(
    <ProjectMgmt
      userDetails={{
        avatar_url: "https://example.com/avatar.png",
        github_user: "alice",
        account_type: "professional",
        github_account_type: "User",
      }}
      onLogout={vi.fn()}
    />
  );

describe("ProjectMgmt – a workflow handed over by Action Usage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockLocationState = null;
    mockUseParams.mockReturnValue({ user: "alice", projectName: "TestProject" });
    vi.mocked(fetchProjects).mockResolvedValue([]);
    vi.mocked(loadProject).mockResolvedValue({
      project_name: "TestProject",
      updated_at: "2024-06-01T00:00:00Z",
      project_code: "TP",
      project_id: 42,
      pr_state: "draft",
      selected_repos: ["alice/my-repo"],
      workflows: [],
      rxworkflows: [],
      project_type: "standard",
      account_type: "professional",
    });
    vi.mocked(getSecrets).mockResolvedValue([]);
    vi.mocked(getEnvVars).mockResolvedValue([]);
    vi.mocked(getProjectPRStatus).mockResolvedValue({
      project_state: "draft",
      pull_requests: [],
      closed_prs: 0,
      open_prs: 0,
      merged_prs: 0,
      total_prs: 0,
    });
  });

  test("passes the draft from the navigation state to the workflows editor", async () => {
    mockLocationState = { projectId: 42, draftWorkflow: DRAFT };

    renderPage();

    expect(await screen.findByTestId("draft")).toHaveTextContent(JSON.stringify(DRAFT));
  });

  test("passes nothing when the page was opened without one", async () => {
    renderPage();

    expect(await screen.findByTestId("draft")).toHaveTextContent("none");
  });

  test("once applied, the draft is dropped from the history entry and the project id is kept", async () => {
    const user = userEvent.setup();
    mockLocationState = { projectId: 42, draftWorkflow: DRAFT };
    renderPage();

    await user.click(await screen.findByTestId("draft-applied"));

    expect(mockNavigate).toHaveBeenCalledWith("/project/alice/TestProject", { replace: true, state: { projectId: 42 } });
  });
});
