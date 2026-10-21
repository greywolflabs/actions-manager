/**
 * Every page App renders itself (all but ProjectMgmt's) is in the one app header: the
 * three primary tabs and the account menu, with no "Back to Projects" and no
 * Log out button of its own (Log out lives in the account menu).
 */
import React from "react";
import { render, screen, within } from "@testing-library/react";
import "@testing-library/jest-dom";
import App from "./App";

class StubWebSocket {
  close = vi.fn();
  send = vi.fn();
  addEventListener = vi.fn();
  removeEventListener = vi.fn();
}
vi.stubGlobal("WebSocket", StubWebSocket);

vi.mock("./api/setup", () => ({
  isUninitialized: vi.fn().mockResolvedValue(false),
}));

vi.mock("./api/user", () => ({
  loginWithGitHubToken: vi.fn(),
  getUserDetails: vi.fn().mockResolvedValue({
    avatar_url: "",
    github_user: "alice",
    account_type: "Professional",
    workspace_role: "admin",
    onboarding: { completed: true, completed_at: "2026-10-01T00:00:00Z", step: null },
  }),
  checkGitHubPermissions: vi.fn().mockResolvedValue(null),
  logout: vi.fn().mockResolvedValue(undefined),
  updateOnboardingState: vi.fn().mockResolvedValue(undefined),
  getGitHubTokenStatus: vi.fn(),
  removeGitHubToken: vi.fn(),
  saveGitHubToken: vi.fn(),
  testGitHubToken: vi.fn(),
}));

const { page } = vi.hoisted(() => ({
  page: (testId: string) => () => ({ default: function Page() { return <h2 data-testid={testId}>{testId}</h2>; } }),
}));
vi.mock("./ProjectMgmt", page("project-mgmt"));
vi.mock("./ManagedFilesList", page("managed-files-page"));
vi.mock("./ActionsProjectsList", page("actions-projects-page"));
vi.mock("./NewProject", page("new-project-page"));
vi.mock("./AddActionsProject", page("add-actions-project-page"));
vi.mock("./ActionsProjectDetail", page("actions-project-detail-page"));
vi.mock("./components/WorkspaceMembers", page("workspace-members-page"));
vi.mock("./components/WorkspaceNotifications", page("workspace-notifications-page"));
vi.mock("./components/WorkspaceBackup", page("workspace-backup-page"));
vi.mock("./components/WorkspaceDriftSettings", page("workspace-drift-page"));

describe("App pages outside a project workspace", () => {
  beforeEach(() => {
    localStorage.setItem("github_user", "alice");
  });

  afterEach(() => {
    localStorage.clear();
    globalThis.history.replaceState({}, "", "/");
  });

  test.each([
    ["/project/alice/managed-files", "managed-files-page", "Managed Files"],
    ["/project/alice/actions-projects", "actions-projects-page", "Managed Actions"],
    ["/project/alice/actions-projects/new", "add-actions-project-page", "Managed Actions"],
    ["/project/alice/actions-projects/7", "actions-project-detail-page", "Managed Actions"],
    ["/project/alice/new", "new-project-page", "Projects"],
    ["/workspace/members", "workspace-members-page", null],
    ["/workspace/notifications", "workspace-notifications-page", null],
    ["/workspace/backup", "workspace-backup-page", null],
    ["/workspace/drift", "workspace-drift-page", null],
  ])("%s renders inside the app header", async (path, pageTestId, currentTab) => {
    globalThis.history.replaceState({}, "", path);
    render(<App />);

    const header = await screen.findByTestId("app-header");
    expect(await within(header).findByRole("button", { name: "User menu for alice" })).toBeInTheDocument();
    expect(within(header).getByRole("navigation", { name: "Primary" })).toBeInTheDocument();
    const current = within(header).getAllByRole("link").filter((link) => link.hasAttribute("aria-current"));
    expect(current.map((link) => link.textContent)).toEqual(currentTab ? [currentTab] : []);
    expect(screen.getByRole("main")).toContainElement(screen.getByTestId(pageTestId));

    expect(screen.queryByText(/Back to Projects/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Log out" })).not.toBeInTheDocument();
  });
});
