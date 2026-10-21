import React from "react";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "@testing-library/jest-dom";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { AppHeader, AppShell } from "./AppHeader";
import type { UserDetails } from "../api/user";
import { BETA_PLAN } from "../test/plans";

const { avatarProps } = vi.hoisted(() => ({ avatarProps: vi.fn() }));

vi.mock("./UserAvatar", () => ({
  default: function UserAvatar(props: { username: string | null }) {
    avatarProps(props);
    return <button type="button" aria-label={`User menu for ${props.username}`} />;
  },
}));

const TAB_IDS = ["projects-nav-button", "managed-files-nav-button", "actions-projects-nav-button"];

const DETAILS: UserDetails = {
  avatar_url: "https://example.com/alice.png",
  github_user: "alice",
  account_type: "Professional",
  plan: BETA_PLAN,
  github_account_type: "Organization",
  connected_github_account: "acme",
  connected_github_account_type: "Organization",
  workspace_role: "admin",
  rate_limit: { limit: 5000, used: 10, remaining: 4990, percentage_used: 0.2, should_warn: false, reset_at: "2026-10-20T12:00:00Z" },
  github_token: { configured: true, status: "valid", message: "Token works" },
};

function CurrentPath(): React.ReactElement {
  return <output data-testid="current-path">{useLocation().pathname}</output>;
}

// null: the user's details haven't loaded yet.
function renderHeaderAt(path: string, userDetails: UserDetails | null = DETAILS, onLogout = vi.fn()) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route
          path="*"
          element={(
            <>
              <AppHeader user="alice" userDetails={userDetails ?? undefined} onLogout={onLogout} />
              <CurrentPath />
            </>
          )}
        />
      </Routes>
    </MemoryRouter>,
  );
}

function currentTabs(): string[] {
  return TAB_IDS.filter((id) => screen.getByTestId(id).getAttribute("aria-current") === "page");
}

describe("AppHeader", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  test("shows Projects, Managed Files and Managed Actions, in that order, as the primary navigation", () => {
    renderHeaderAt("/project/alice");

    const nav = screen.getByRole("navigation", { name: "Primary" });
    expect(within(nav).getAllByRole("link").map((link) => link.textContent)).toEqual([
      "Projects",
      "Managed Files",
      "Managed Actions",
    ]);
    expect(within(nav).getAllByRole("link").map((link) => link.dataset.testid)).toEqual(TAB_IDS);
  });

  test.each([
    ["/project/alice", ["projects-nav-button"]],
    ["/project/alice/managed-files", ["managed-files-nav-button"]],
    ["/project/alice/actions-projects", ["actions-projects-nav-button"]],
    ["/project/alice/actions-projects/new", ["actions-projects-nav-button"]],
    ["/project/alice/actions-projects/7", ["actions-projects-nav-button"]],
    ["/workspace/members", []],
    ["/workspace/notifications", []],
    ["/workspace/backup", []],
    ["/workspace/drift", []],
  ])("on %s the current tab is %j", (path, expected) => {
    renderHeaderAt(path);

    expect(currentTabs()).toEqual(expected);
  });

  test.each([
    ["projects-nav-button", "/project/alice"],
    ["managed-files-nav-button", "/project/alice/managed-files"],
    ["actions-projects-nav-button", "/project/alice/actions-projects"],
  ])("clicking %s opens %s", async (testId, path) => {
    renderHeaderAt("/workspace/members");

    await userEvent.setup().click(screen.getByTestId(testId));

    expect(screen.getByTestId("current-path")).toHaveTextContent(path);
    expect(currentTabs()).toEqual([testId]);
  });

  test("the logo goes back to the Projects page", async () => {
    renderHeaderAt("/project/alice/managed-files");

    const home = screen.getByRole("link", { name: "ActionsManager home" });
    expect(home).toHaveAttribute("href", "/project/alice");
    await userEvent.setup().click(home);
    expect(screen.getByTestId("current-path")).toHaveTextContent("/project/alice");
  });

  test("the account menu waits for the user's details", () => {
    const { rerender } = renderHeaderAt("/project/alice", null);
    expect(screen.queryByRole("button", { name: /User menu/ })).not.toBeInTheDocument();

    const onLogout = vi.fn();
    rerender(
      <MemoryRouter initialEntries={["/project/alice"]}>
        <AppHeader user="alice" userDetails={DETAILS} onLogout={onLogout} />
      </MemoryRouter>,
    );

    expect(within(screen.getByTestId("app-header")).getByRole("button", { name: "User menu for alice" })).toBeInTheDocument();
  });

  test("the plan chip sits beside the account menu on every page", () => {
    renderHeaderAt("/project/alice/managed-files");

    const header = screen.getByTestId("app-header");
    const chip = within(header).getByTestId("plan-chip");
    expect(chip).toHaveTextContent("Self-Hosted Beta");
    expect(chip.parentElement).toContainElement(within(header).getByRole("button", { name: "User menu for alice" }));
  });

  test("there is no plan chip until the user's details have loaded", () => {
    renderHeaderAt("/project/alice", null);

    expect(screen.queryByTestId("plan-chip")).not.toBeInTheDocument();
  });

  test("the account menu gets the same details on every page", () => {
    const onLogout = vi.fn();
    renderHeaderAt("/workspace/backup", DETAILS, onLogout);

    expect(avatarProps).toHaveBeenLastCalledWith({
      avatarUrl: DETAILS.avatar_url,
      username: DETAILS.github_user,
      accountType: DETAILS.account_type,
      plan: DETAILS.plan,
      githubAccountType: DETAILS.github_account_type,
      connectedGithubAccount: DETAILS.connected_github_account,
      connectedGithubAccountType: DETAILS.connected_github_account_type,
      workspaceRole: DETAILS.workspace_role,
      rateLimit: DETAILS.rate_limit,
      githubToken: DETAILS.github_token,
      onLogout,
    });
  });
});

describe("AppHeader on pages whose URL names no tab", () => {
  test("marks the section it is given as current, and only that one", () => {
    render(
      <MemoryRouter initialEntries={["/project/alice/payments"]}>
        <AppHeader user="alice" userDetails={DETAILS} currentSection="managed-files" />
      </MemoryRouter>,
    );

    const managedFiles = screen.getByTestId("managed-files-nav-button");
    expect(managedFiles).toHaveAttribute("aria-current", "true");
    expect(managedFiles).toHaveAttribute("href", "/project/alice/managed-files");
    expect(managedFiles).toHaveClass("border-primary");
    expect(screen.getByTestId("projects-nav-button")).not.toHaveAttribute("aria-current");
    expect(screen.getByTestId("actions-projects-nav-button")).not.toHaveAttribute("aria-current");
  });

  test("stays at the top of the window and spans it, logo left and account menu right", () => {
    render(
      <MemoryRouter>
        <AppHeader user="alice" userDetails={DETAILS} currentSection="projects" />
      </MemoryRouter>,
    );
    const header = screen.getByTestId("app-header");
    const row = header.firstElementChild as HTMLElement;
    expect(header).toHaveClass("sticky", "top-0");
    expect(row).toHaveClass("w-full", "px-6", "lg:px-8");
    expect(row).not.toHaveClass("max-w-[1400px]");
    expect(row.firstElementChild).toBe(screen.getByRole("link", { name: "ActionsManager home" }));
    expect(row.lastElementChild).toHaveClass("ml-auto");
    expect(row.lastElementChild).toContainElement(screen.getByRole("button", { name: "User menu for alice" }));
  });
});

describe("AppShell", () => {
  test("centers the page in its container, with the header's side padding so the logo and heading line up", () => {
    render(
      <MemoryRouter initialEntries={["/project/alice/managed-files"]}>
        <AppShell user="alice" userDetails={DETAILS}>
          <h2>Managed Files</h2>
        </AppShell>
      </MemoryRouter>,
    );

    const padding = ["px-6", "lg:px-8"];
    const headerRow = screen.getByTestId("app-header").firstElementChild as HTMLElement;
    const main = screen.getByRole("main");
    expect(headerRow).toHaveClass(...padding);
    expect(main).toHaveClass("w-full", "max-w-[1400px]", "mx-auto", ...padding);
    expect(main).toContainElement(screen.getByRole("heading", { name: "Managed Files" }));
  });
});
