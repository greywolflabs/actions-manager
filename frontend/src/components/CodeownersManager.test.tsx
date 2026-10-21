import React, { useState } from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";
import { vi } from "vitest";
import CodeownersManager from "./CodeownersManager";
import * as api from "../api/codeowners";
import * as workflowsApi from "../api/workflows";
import type { CodeownersDriftDetail } from "../api/drift";

vi.mock("./PlainFileEditor", () => ({
  default: ({ value, onChange, readOnly }: { value: string; onChange?: (v: string) => void; readOnly?: boolean }) => (
    <textarea
      data-testid="codeowners-editor"
      value={value ?? ""}
      readOnly={readOnly}
      onChange={(e) => onChange?.(e.target.value)}
    />
  ),
}));

vi.mock("../api/codeowners", () => ({
  getCodeowners: vi.fn(),
  saveProjectCodeowners: vi.fn(),
}));

vi.mock("../api/workflows", () => ({
  updateWorkflows: vi.fn(),
}));

const mocked = vi.mocked(api);
const mockedPush = vi.mocked(workflowsApi.updateWorkflows);

const REPOS = ["octo/a", "octo/b"];
const PROJECT_ID = 7;

const record = (overrides: Partial<api.CodeownersRecord> = {}): api.CodeownersRecord => ({
  id: 1,
  project_id: PROJECT_ID,
  content: "* @octo/owners\n",
  file_path: ".github/CODEOWNERS",
  git_hash: "a".repeat(40),
  status: "synced_with_github",
  last_modified_by: "someone",
  created_at: null,
  updated_at: null,
  ...overrides,
});

const drifted = (repo: string, branch = "main", overrides: Partial<CodeownersDriftDetail> = {}): CodeownersDriftDetail => ({
  file_path: ".github/CODEOWNERS",
  repo,
  repo_id: 1,
  branch,
  has_drift: true,
  actionsmanager_sha: "a".repeat(40),
  github_sha: "b".repeat(40),
  last_checked: "2026-10-14T00:00:00+00:00",
  message: `CODEOWNERS content differs between local and ${repo}`,
  ...overrides,
});

const gitHubCopy = (repo: string, content: string | null, path = ".github/CODEOWNERS"): api.CodeownersGetResponse => ({
  success: true,
  repo_id: 1,
  repo_name: repo,
  branch: null,
  github: { exists: content !== null, content, sha: content ? "c".repeat(40) : null, path: content ? path : null },
  local: null,
});

/** Owns the file as the page does: a save hands back the record, and drift
 *  comes from the page's drift check, which a save re-reads. */
function Harness(props: Readonly<{
  initial?: api.CodeownersRecord | null;
  drift?: CodeownersDriftDetail[];
  isReadOnly?: boolean;
  repos?: string[];
  paths?: string[];
  onSaved?: (record: api.CodeownersRecord, prState?: string) => void;
  onReposDelivered?: (repos: string[]) => void;
  onKeepActionsManagerVersion?: (repos: string[]) => void;
}>) {
  const [codeowners, setCodeowners] = useState<api.CodeownersRecord | null>(props.initial ?? null);
  const [drift, setDrift] = useState<CodeownersDriftDetail[]>(props.drift ?? []);
  return (
    <>
      <CodeownersManager
        user="u"
        projectName="proj"
        projectId={PROJECT_ID}
        selectedRepos={props.repos ?? REPOS}
        isReadOnly={props.isReadOnly}
        codeowners={codeowners}
        drift={drift}
        onSaved={(saved, prState) => {
          setCodeowners(saved);
          setDrift([]);
          props.onSaved?.(saved, prState);
        }}
        onReposDelivered={props.onReposDelivered}
        onKeepActionsManagerVersion={props.onKeepActionsManagerVersion}
        paths={props.paths ?? PATHS}
      />
      <button type="button" onClick={() => setCodeowners(record({ status: "under_review", content: "* @in-review\n" }))}>
        server: under review
      </button>
      <button type="button" onClick={() => setCodeowners(record({ content: "* @elsewhere\n" }))}>
        server: saved elsewhere
      </button>
    </>
  );
}

// The kind's paths, as GET /api/managed-files lists them.
const PATHS = [".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"];

const editor = () => screen.getByTestId("codeowners-editor") as HTMLTextAreaElement;
const commit = () => screen.getByRole("button", { name: /Commit Locally/ });

describe("CodeownersManager", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocked.saveProjectCodeowners.mockImplementation(async (_projectId, content, filePath) => ({
      success: true,
      message: "CODEOWNERS saved",
      codeowners: record({ content, file_path: filePath, status: "committed_locally", git_hash: "0".repeat(40) }),
      validation_warnings: [],
      pr_state: "draft",
    }));
  });

  it("edits one file for the whole project, with no repository to pick", () => {
    render(<Harness initial={record({ status: "committed_locally" })} />);

    expect(editor().value).toBe("* @octo/owners\n");
    expect(screen.getByText("Draft changes")).toBeInTheDocument();
    expect(screen.getByText(/delivered to all 2 repositories/)).toBeInTheDocument();
    expect(screen.queryByText(/Target Repositories/)).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  });

  it("offers the paths it is given, the first as the default (#2367)", async () => {
    render(<Harness paths={[".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"]} />);
    const select = screen.getByLabelText("File path:") as HTMLSelectElement;

    expect([...select.options].map((option) => option.value)).toEqual([".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"]);
    expect(select.value).toBe(".github/CODEOWNERS");
    expect(screen.getByRole("option", { name: "docs/CODEOWNERS (docs folder)" })).toBeInTheDocument();

    fireEvent.change(editor(), { target: { value: "* @octo/docs\n" } });
    fireEvent.change(select, { target: { value: "docs/CODEOWNERS" } });
    fireEvent.click(commit());
    await waitFor(() => expect(mocked.saveProjectCodeowners).toHaveBeenCalledWith(PROJECT_ID, "* @octo/docs\n", "docs/CODEOWNERS"));
  });

  it("Commit Locally saves the project's file once and shows the saved copy", async () => {
    const onSaved = vi.fn();
    render(<Harness initial={record()} onSaved={onSaved} />);

    fireEvent.change(editor(), { target: { value: "* @octo/new\n" } });
    expect(screen.getByText("Unsaved changes")).toBeInTheDocument();
    fireEvent.click(commit());

    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    expect(mocked.saveProjectCodeowners).toHaveBeenCalledTimes(1);
    expect(mocked.saveProjectCodeowners).toHaveBeenCalledWith(PROJECT_ID, "* @octo/new\n", ".github/CODEOWNERS");
    expect(onSaved.mock.calls[0][1]).toBe("draft");
    expect(screen.queryByText("Unsaved changes")).not.toBeInTheDocument();
    expect(screen.getByText("Draft changes")).toBeInTheDocument();
    expect(editor().value).toBe("* @octo/new\n");
  });

  it("shows drift from the project's check, and drops it once a save resolves it", async () => {
    render(<Harness initial={record()} drift={[drifted("octo/a")]} />);

    expect(screen.getByText("Drift detected")).toBeInTheDocument();
    expect(screen.getByTestId("codeowners-drift-banner")).toHaveTextContent(
      "the CODEOWNERS file on GitHub has changed on octo/a @ main since ActionsManager last delivered it"
    );

    fireEvent.change(editor(), { target: { value: "* @octo/new\n" } });
    fireEvent.click(commit());

    await waitFor(() => expect(screen.queryByTestId("codeowners-drift-banner")).not.toBeInTheDocument());
    expect(screen.queryByText("Drift detected")).not.toBeInTheDocument();
  });

  it("loads the version a drifted branch holds, to keep it with Commit Locally", async () => {
    mocked.getCodeowners.mockResolvedValue(gitHubCopy("octo/a", "* @from-github\n"));
    render(<Harness initial={record()} drift={[drifted("octo/a", "release/1.0")]} />);

    fireEvent.click(screen.getByRole("button", { name: "Load GitHub version" }));

    await waitFor(() => expect(editor().value).toBe("* @from-github\n"));
    expect(mocked.getCodeowners).toHaveBeenCalledWith(
      "octo/a", "u", "proj", { branch: "release/1.0", path: ".github/CODEOWNERS" }
    );
    expect(screen.getByText("Unsaved changes")).toBeInTheDocument();
  });

  it("offers each drifted branch that still has a file to load", () => {
    render(
      <Harness
        initial={record()}
        drift={[drifted("octo/a"), drifted("octo/b", "dev", { deleted_in_github: true, github_sha: null })]}
      />
    );

    expect(screen.getByTestId("codeowners-drift-banner")).toHaveTextContent("has changed or was removed on octo/a @ main, octo/b @ dev");
    expect(screen.getByRole("button", { name: "Load from octo/a @ main" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /octo\/b/ })).not.toBeInTheDocument();
  });

  it("keeps the ActionsManager version through a campaign for each drifted repository, once each", () => {
    const keep = vi.fn();
    render(
      <Harness
        initial={record()}
        drift={[drifted("octo/a"), drifted("octo/a", "release/1.0"), drifted("octo/b")]}
        onKeepActionsManagerVersion={keep}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: "Keep ActionsManager version" }));

    expect(keep).toHaveBeenCalledWith(["octo/a", "octo/b"]);
    expect(screen.getByTestId("codeowners-drift-banner")).toHaveTextContent("a PR campaign carrying only this file");
  });

  it("offers no campaign to a read-only member", () => {
    render(<Harness initial={record()} drift={[drifted("octo/a")]} isReadOnly onKeepActionsManagerVersion={vi.fn()} />);

    expect(screen.queryByRole("button", { name: "Keep ActionsManager version" })).not.toBeInTheDocument();
  });

  it("locks the file while a PR carries it, with a greyed-out Commit Locally", () => {
    render(<Harness initial={record({ status: "under_review" })} drift={[drifted("octo/a")]} />);

    expect(screen.getByTestId("unlock-codeowners-button")).toBeInTheDocument();
    expect(editor()).toHaveAttribute("readonly");
    expect(commit()).toBeDisabled();
    expect(screen.queryByTestId("update-pr-button")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load GitHub version" })).not.toBeInTheDocument();
  });

  describe("unlocked under review", () => {
    const unlock = () => {
      fireEvent.click(screen.getByTestId("unlock-codeowners-button"));
      fireEvent.click(screen.getByRole("button", { name: "Unlock and Edit" }));
    };

    it("Commit and Update PR adds the edit to the open PR and locks the file again", async () => {
      mockedPush.mockResolvedValue({ results: { "octo/a on main": { status: "pr_updated" } } } as never);
      const onReposDelivered = vi.fn();
      render(<Harness initial={record({ status: "under_review" })} onReposDelivered={onReposDelivered} />);

      unlock();
      fireEvent.change(editor(), { target: { value: "* @mine\n" } });
      fireEvent.click(screen.getByTestId("update-pr-button"));

      await waitFor(() => expect(screen.getByText("CODEOWNERS committed and pull request updated.")).toBeInTheDocument());
      expect(mocked.saveProjectCodeowners).toHaveBeenCalledWith(PROJECT_ID, "* @mine\n", ".github/CODEOWNERS");
      expect(mockedPush).toHaveBeenCalledWith("proj", REPOS, { includeCodeowners: true });
      expect(onReposDelivered).toHaveBeenCalledWith(["octo/a"]);
      expect(screen.getByTestId("unlock-codeowners-button")).toBeInTheDocument();
      expect(editor().value).toBe("* @mine\n");
      expect(commit()).toBeDisabled();
    });

    it("leaves the save committed locally when the push fails", async () => {
      mockedPush.mockResolvedValue({ error: "GitHub is down" } as never);
      render(<Harness initial={record({ status: "under_review" })} />);

      unlock();
      fireEvent.change(editor(), { target: { value: "* @mine\n" } });
      fireEvent.click(screen.getByTestId("update-pr-button"));

      await waitFor(() => expect(screen.getByText(/its pull request was not updated: GitHub is down/)).toBeInTheDocument());
      expect(screen.getByText("Draft changes")).toBeInTheDocument();
      expect(screen.queryByTestId("unlock-codeowners-button")).not.toBeInTheDocument();
      expect(editor()).not.toHaveAttribute("readonly");
    });

    it("offers no unlock to a viewer", () => {
      render(<Harness initial={record({ status: "under_review" })} isReadOnly />);

      expect(screen.queryByTestId("unlock-codeowners-button")).not.toBeInTheDocument();
      expect(commit()).toBeDisabled();
    });
  });

  it("keeps an unsaved edit when the page's copy changes, unless it went under review", () => {
    render(<Harness initial={record()} />);
    fireEvent.change(editor(), { target: { value: "* @mine\n" } });

    fireEvent.click(screen.getByRole("button", { name: "server: saved elsewhere" }));
    expect(editor().value).toBe("* @mine\n");

    fireEvent.click(screen.getByRole("button", { name: "server: under review" }));
    expect(editor().value).toBe("* @in-review\n");
    expect(screen.queryByText("Unsaved changes")).not.toBeInTheDocument();
  });

  it("starts a project without CODEOWNERS from a repository's file", async () => {
    mocked.getCodeowners.mockResolvedValue(gitHubCopy("octo/b", "* @team-b\n", "CODEOWNERS"));
    render(<Harness initial={null} />);

    fireEvent.change(screen.getByLabelText("Repository to import CODEOWNERS from"), { target: { value: "octo/b" } });
    fireEvent.click(screen.getByRole("button", { name: "Load from repository" }));

    await waitFor(() => expect(editor().value).toBe("* @team-b\n"));
    expect(mocked.getCodeowners).toHaveBeenCalledWith("octo/b", "u", "proj", {});
    expect((screen.getByLabelText("File path:") as HTMLSelectElement).value).toBe("CODEOWNERS");
  });

  it("says so when the repository has no file to import", async () => {
    mocked.getCodeowners.mockResolvedValue(gitHubCopy("octo/a", null));
    render(<Harness initial={null} />);

    fireEvent.click(screen.getByRole("button", { name: "Load from repository" }));

    await waitFor(() => expect(screen.getByText("octo/a has no CODEOWNERS file")).toBeInTheDocument());
    expect(editor().value).toBe("");
  });

  it("keeps the edit when the save fails", async () => {
    mocked.saveProjectCodeowners.mockRejectedValueOnce({
      response: { data: { detail: "Not a project editor" } },
    });
    render(<Harness initial={record()} />);

    fireEvent.change(editor(), { target: { value: "* @mine\n" } });
    fireEvent.click(commit());

    await waitFor(() =>
      expect(screen.getByText("Not a project editor")).toBeInTheDocument()
    );
    expect(editor().value).toBe("* @mine\n");
    expect(screen.getByText("Unsaved changes")).toBeInTheDocument();
  });

  it("is read-only for a viewer", () => {
    render(<Harness initial={null} isReadOnly />);

    expect(editor()).toHaveAttribute("readonly");
    expect(commit()).toBeDisabled();
    expect(screen.queryByTestId("codeowners-import")).not.toBeInTheDocument();
  });

  it("asks for a repository first", () => {
    render(<Harness initial={null} repos={[]} />);

    expect(screen.getByText(/to manage the project's CODEOWNERS file/)).toBeInTheDocument();
  });
});
