import React, { useState } from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";
import { vi } from "vitest";
import CustomFiles, { CustomFilePanel } from "./CustomFiles";
import * as api from "../api/customFiles";
import * as workflowsApi from "../api/workflows";
import type { CustomFileDriftDetail } from "../api/drift";

vi.mock("./PlainFileEditor", () => ({
  default: ({ value, onChange }: { value: string; onChange?: (v: string) => void }) => (
    <textarea
      data-testid="file-content-input"
      value={value ?? ""}
      onChange={(e) => onChange?.(e.target.value)}
    />
  ),
}));

vi.mock("../api/customFiles", () => ({
  createCustomFile: vi.fn(),
  updateCustomFile: vi.fn(),
  deleteCustomFile: vi.fn(),
  restoreCustomFile: vi.fn(),
  getCustomFileOnGitHub: vi.fn(),
  validateFilePath: vi.fn((p: string) => {
    if (!p?.trim()) return "File path is required";
    if (p.startsWith("/")) return "Absolute paths are not allowed";
    if (p.includes("..")) return "Path traversal (..) is not allowed";
    if (p.endsWith(".env") || p.includes(".env.")) return ".env files are not allowed";
    return null;
  }),
}));

vi.mock("../api/workflows", () => ({
  updateWorkflows: vi.fn(),
}));

const mockFile = (overrides = {}): api.CustomFile => ({
  id: 1,
  project_id: 42,
  display_name: "Build Script",
  file_path: ".github/scripts/build.sh",
  file_content: "#!/bin/bash",
  git_hash: null,
  file_status: "new",
  pending_delete: false,
  last_modified_by: "testuser",
  description: null,
  created_at: null,
  updated_at: null,
  ...overrides,
});

describe("CustomFiles", () => {
  const defaultProps = { projectId: 42, githubUser: "testuser", initialFiles: [] };

  test("renders empty state when no files", () => {
    render(<CustomFiles {...defaultProps} />);
    expect(screen.getByTestId("empty-state")).toBeInTheDocument();
  });

  test("renders file list when files provided", () => {
    render(<CustomFiles {...defaultProps} initialFiles={[mockFile()]} />);
    expect(screen.queryByTestId("empty-state")).not.toBeInTheDocument();
    expect(screen.getByTestId("custom-file-row")).toBeInTheDocument();
    expect(screen.getByText(".github/scripts/build.sh")).toBeInTheDocument();
  });

  test("shows pending delete badge for pending_delete files", () => {
    render(<CustomFiles {...defaultProps} initialFiles={[mockFile({ pending_delete: true })]} />);
    expect(screen.getByTestId("pending-delete-badge")).toBeInTheDocument();
  });

  test("shows restore button for pending_delete files", () => {
    render(<CustomFiles {...defaultProps} initialFiles={[mockFile({ pending_delete: true })]} />);
    fireEvent.click(screen.getByTestId("custom-file-row"));
    expect(screen.getByTestId("restore-button")).toBeInTheDocument();
    expect(screen.queryByTestId("delete-button")).not.toBeInTheDocument();
  });

  test("shows add file form when button clicked", () => {
    render(<CustomFiles {...defaultProps} />);
    fireEvent.click(screen.getByTestId("add-custom-file-button"));
    expect(screen.getByTestId("file-path-input")).toBeInTheDocument();
    expect(screen.getByTestId("file-content-input")).toBeInTheDocument();
  });

  test("add form disables save button when path is empty", () => {
    render(<CustomFiles {...defaultProps} />);
    fireEvent.click(screen.getByTestId("add-custom-file-button"));
    expect(screen.getByTestId("save-button")).toBeDisabled();
  });

  test("add form validates absolute path", async () => {
    render(<CustomFiles {...defaultProps} />);
    fireEvent.click(screen.getByTestId("add-custom-file-button"));
    fireEvent.change(screen.getByTestId("file-path-input"), { target: { value: "/etc/passwd" } });
    expect(screen.getByTestId("path-error")).toBeInTheDocument();
    expect(screen.getByTestId("save-button")).toBeDisabled();
  });

  test("add form validates dotenv path", async () => {
    render(<CustomFiles {...defaultProps} />);
    fireEvent.click(screen.getByTestId("add-custom-file-button"));
    fireEvent.change(screen.getByTestId("file-path-input"), { target: { value: ".env.production" } });
    expect(screen.getByTestId("path-error")).toBeInTheDocument();
  });

  test("saving calls create API and updates state", async () => {
    const newFile = mockFile({ id: 99, file_path: "sonar-project.properties" });
    vi.mocked(api.createCustomFile).mockResolvedValueOnce({ custom_file: newFile });
    const onChange = vi.fn();
    render(<CustomFiles {...defaultProps} onChange={onChange} />);
    fireEvent.click(screen.getByTestId("add-custom-file-button"));
    fireEvent.change(screen.getByTestId("file-path-input"), { target: { value: "sonar-project.properties" } });
    fireEvent.click(screen.getByTestId("save-button"));
    await waitFor(() => expect(api.createCustomFile).toHaveBeenCalledWith(42, expect.objectContaining({ file_path: "sonar-project.properties" })));
    await waitFor(() => expect(onChange).toHaveBeenCalledWith([newFile]));
  });

  test("editing calls update API", async () => {
    const cf = mockFile({ file_status: "synced_with_github", git_hash: "a".repeat(40) });
    const updated = { ...cf, file_status: "committed_locally", git_hash: null };
    vi.mocked(api.updateCustomFile).mockResolvedValueOnce({ custom_file: updated });
    render(<CustomFiles {...defaultProps} initialFiles={[cf]} />);
    fireEvent.click(screen.getByTestId("custom-file-row"));
    fireEvent.click(screen.getByTestId("edit-button"));
    fireEvent.change(screen.getByTestId("file-content-input"), { target: { value: "new content" } });
    fireEvent.click(screen.getByTestId("save-button"));
    await waitFor(() => expect(api.updateCustomFile).toHaveBeenCalledWith(42, 1, expect.objectContaining({ file_content: "new content" })));
  });

  test("restore calls restore API", async () => {
    const cf = mockFile({ pending_delete: true, git_hash: "a".repeat(40), file_status: "committed_locally" });
    const restored = { ...cf, pending_delete: false };
    vi.mocked(api.restoreCustomFile).mockResolvedValueOnce({ custom_file: restored });
    render(<CustomFiles {...defaultProps} initialFiles={[cf]} />);
    fireEvent.click(screen.getByTestId("custom-file-row"));
    fireEvent.click(screen.getByTestId("restore-button"));
    await waitFor(() => expect(api.restoreCustomFile).toHaveBeenCalledWith(42, 1));
  });

  // ── Bug 1 regression: onChange must fire on every mutation ──────────────────

  test("add fires onChange so parent can promote pr_state to draft", async () => {
    const newFile = mockFile({ id: 99, file_path: "sonar.properties", file_status: "new" });
    vi.mocked(api.createCustomFile).mockResolvedValueOnce({ custom_file: newFile });
    const onChange = vi.fn();
    render(<CustomFiles {...defaultProps} onChange={onChange} />);
    fireEvent.click(screen.getByTestId("add-custom-file-button"));
    fireEvent.change(screen.getByTestId("file-path-input"), { target: { value: "sonar.properties" } });
    fireEvent.click(screen.getByTestId("save-button"));
    await waitFor(() => expect(onChange).toHaveBeenCalledWith([newFile]));
  });

  test("edit fires onChange so parent can promote pr_state to draft", async () => {
    const cf = mockFile({ file_status: "synced_with_github", git_hash: "a".repeat(40) });
    const updated = { ...cf, file_status: "committed_locally", git_hash: null };
    vi.mocked(api.updateCustomFile).mockResolvedValueOnce({ custom_file: updated });
    const onChange = vi.fn();
    render(<CustomFiles {...defaultProps} initialFiles={[cf]} onChange={onChange} />);
    fireEvent.click(screen.getByTestId("custom-file-row"));
    fireEvent.click(screen.getByTestId("edit-button"));
    fireEvent.change(screen.getByTestId("file-content-input"), { target: { value: "changed" } });
    fireEvent.click(screen.getByTestId("save-button"));
    await waitFor(() => expect(onChange).toHaveBeenCalledWith([updated]));
  });

  // ── Bug 2 regression: component must reflect parent-driven prop updates ─────

  test("synced files remain visible after parent reloads customFiles", () => {
    const syncedFile = mockFile({ file_status: "synced_with_github", git_hash: "a".repeat(40) });
    const { rerender } = render(<CustomFiles {...defaultProps} initialFiles={[]} />);
    expect(screen.queryByTestId("custom-file-row")).not.toBeInTheDocument();

    // Simulate parent (ProjectMgmt) refreshing customFiles after PR merge
    rerender(<CustomFiles {...defaultProps} initialFiles={[syncedFile]} />);
    expect(screen.getByTestId("custom-file-row")).toBeInTheDocument();
    expect(screen.queryByTestId("empty-state")).not.toBeInTheDocument();
  });

  test("synced file shows correct status badge", () => {
    const syncedFile = mockFile({ file_status: "synced_with_github", git_hash: "a".repeat(40) });
    render(<CustomFiles {...defaultProps} initialFiles={[syncedFile]} />);
    expect(screen.getByTestId("custom-file-row")).toBeInTheDocument();
    // No pending-delete badge on a clean synced file
    expect(screen.queryByTestId("pending-delete-badge")).not.toBeInTheDocument();
  });

  describe("removal scope", () => {
    const syncedFile = () =>
      mockFile({ file_status: "synced_with_github", git_hash: "a".repeat(40) });

    const openRemovalDialog = (file: api.CustomFile) => {
      render(<CustomFiles {...defaultProps} initialFiles={[file]} />);
      fireEvent.click(screen.getByTestId("custom-file-row"));
      fireEvent.click(screen.getByTestId("delete-button"));
    };

    test("delete opens the scope dialog with the safe option preselected", () => {
      openRemovalDialog(syncedFile());

      expect(screen.getByRole("dialog")).toBeInTheDocument();
      expect(screen.getByLabelText("Remove from ActionsManager only")).toBeChecked();
      expect(api.deleteCustomFile).not.toHaveBeenCalled();
    });

    test("project-scoped removal drops the row and leaves the file in GitHub", async () => {
      vi.mocked(api.deleteCustomFile).mockResolvedValue({ deleted: true, hard_deleted: true });
      const onChange = vi.fn();
      const file = syncedFile();

      render(<CustomFiles {...defaultProps} initialFiles={[file]} onChange={onChange} />);
      fireEvent.click(screen.getByTestId("custom-file-row"));
      fireEvent.click(screen.getByTestId("delete-button"));
      fireEvent.click(screen.getByRole("button", { name: /Remove from ActionsManager/i }));

      await waitFor(() => {
        expect(api.deleteCustomFile).toHaveBeenCalledWith(42, file.id, "project", "campaign");
      });
      // The list updates from the response, with no refetch and no reload.
      expect(onChange).toHaveBeenCalledWith([]);
    });

    test("delete-everywhere defaults to a PR Campaign", async () => {
      vi.mocked(api.deleteCustomFile).mockResolvedValue({
        deleted: false,
        pending_delete: true,
        custom_file: { ...syncedFile(), pending_delete: true },
        campaign_id: 7,
        prs_created: 2,
      });
      const onChange = vi.fn();

      render(<CustomFiles {...defaultProps} initialFiles={[syncedFile()]} onChange={onChange} />);
      fireEvent.click(screen.getByTestId("custom-file-row"));
      fireEvent.click(screen.getByTestId("delete-button"));
      fireEvent.click(screen.getByLabelText("Delete from ActionsManager and GitHub"));
      fireEvent.click(screen.getByRole("button", { name: /Delete Everywhere/i }));

      await waitFor(() => {
        expect(api.deleteCustomFile).toHaveBeenCalledWith(42, 1, "project_and_github", "campaign");
      });
      // The row stays until the PR merges, so Restore still has something to act on.
      expect(onChange).toHaveBeenCalledWith([
        expect.objectContaining({ pending_delete: true }),
      ]);
    });

    test("delete-everywhere can commit the removal directly instead", async () => {
      vi.mocked(api.deleteCustomFile).mockResolvedValue({
        deleted: true,
        hard_deleted: true,
        targets: [{ repo: "acme/app", branch: "main", status: "deleted" }],
      });
      const onChange = vi.fn();

      render(<CustomFiles {...defaultProps} initialFiles={[syncedFile()]} onChange={onChange} />);
      fireEvent.click(screen.getByTestId("custom-file-row"));
      fireEvent.click(screen.getByTestId("delete-button"));
      fireEvent.click(screen.getByLabelText("Delete from ActionsManager and GitHub"));
      fireEvent.click(screen.getByLabelText("Commit directly to the target branch"));
      fireEvent.click(screen.getByRole("button", { name: /Delete Everywhere/i }));

      await waitFor(() => {
        expect(api.deleteCustomFile).toHaveBeenCalledWith(42, 1, "project_and_github", "direct");
      });
      // Gone from GitHub, so gone from the list — no pending-delete row left behind.
      expect(onChange).toHaveBeenCalledWith([]);
    });

    test("reports the new project state so Create PR Campaign enables without a reload", async () => {
      vi.mocked(api.deleteCustomFile).mockResolvedValue({
        deleted: false,
        pending_delete: true,
        custom_file: { ...syncedFile(), pending_delete: true },
        campaign_id: 7,
        prs_created: 2,
        pr_state: "draft",
      });
      const onProjectStateChange = vi.fn();

      render(
        <CustomFiles
          {...defaultProps}
          initialFiles={[syncedFile()]}
          onChange={vi.fn()}
          onProjectStateChange={onProjectStateChange}
        />
      );
      fireEvent.click(screen.getByTestId("custom-file-row"));
      fireEvent.click(screen.getByTestId("delete-button"));
      fireEvent.click(screen.getByLabelText("Delete from ActionsManager and GitHub"));
      fireEvent.click(screen.getByRole("button", { name: /Delete Everywhere/i }));

      await waitFor(() => expect(onProjectStateChange).toHaveBeenCalledWith("draft"));
    });

    test("GitHub option is disabled for a file GitHub has never seen", () => {
      openRemovalDialog(mockFile({ file_status: "new", git_hash: null }));

      expect(screen.getByLabelText("Delete from ActionsManager and GitHub")).toBeDisabled();
    });

    test("the warning matches the delivery the user picked", () => {
      openRemovalDialog(syncedFile());
      fireEvent.click(screen.getByLabelText("Delete from ActionsManager and GitHub"));

      // A pull request can be closed and the file restored until it merges.
      // Said in both the delivery option and the amber warning.
      expect(screen.getAllByText(/you can Restore it here until then/).length).toBeGreaterThan(0);
      expect(screen.queryByText(/cannot be undone/i)).not.toBeInTheDocument();

      fireEvent.click(screen.getByLabelText("Commit directly to the target branch"));

      // A direct commit really is permanent.
      expect(screen.getByText(/cannot be undone/i)).toBeInTheDocument();
    });

    test("CustomFilePanel tells its parent to clear the selection when the row is gone", async () => {
      vi.mocked(api.deleteCustomFile).mockResolvedValue({ deleted: true, hard_deleted: true });
      const onRemoved = vi.fn();
      const file = syncedFile();

      render(
        <CustomFilePanel
          cf={file}
          allFiles={[file]}
          projectId={42}
          githubUser="testuser"
          onChange={vi.fn()}
          onRemoved={onRemoved}
        />
      );
      fireEvent.click(screen.getByTestId("delete-button"));
      fireEvent.click(screen.getByRole("button", { name: /Remove from ActionsManager/i }));

      // Without this the parent keeps an id that no longer exists and the panel
      // silently renders the "add file" form in place of the removed file.
      await waitFor(() => expect(onRemoved).toHaveBeenCalled());
    });
  });

  test("prop update replaces previous list (no stale internal state)", () => {
    const fileA = mockFile({ id: 1, file_path: ".yamllint.yml" });
    const fileB = mockFile({ id: 2, file_path: "sonar.properties", file_status: "synced_with_github" });
    const { rerender } = render(<CustomFiles {...defaultProps} initialFiles={[fileA]} />);
    expect(screen.getByText(".yamllint.yml")).toBeInTheDocument();

    // Parent swaps in a different list (e.g. after a project reload post-merge)
    rerender(<CustomFiles {...defaultProps} initialFiles={[fileB]} />);
    expect(screen.queryByText(".yamllint.yml")).not.toBeInTheDocument();
    expect(screen.getByText("sonar.properties")).toBeInTheDocument();
  });

  describe("CustomFilePanel under review", () => {
    const underReview = () => mockFile({ file_status: "under_review", file_content: "echo old" });

    // Owns the list as the page does, so a save shows up without a remount.
    function Panel({ onReposDelivered }: Readonly<{ onReposDelivered?: (repos: string[]) => void }>) {
      const [files, setFiles] = useState([underReview()]);
      return (
        <CustomFilePanel
          cf={files[0]}
          allFiles={files}
          projectId={42}
          githubUser="testuser"
          projectName="proj"
          selectedRepos={["octo/a"]}
          onChange={setFiles}
          onReposDelivered={onReposDelivered}
        />
      );
    }

    const unlockAndEdit = () => {
      fireEvent.click(screen.getByTestId("unlock-custom-file-button"));
      fireEvent.click(screen.getByRole("button", { name: "Unlock and Edit" }));
      fireEvent.change(screen.getByTestId("file-content-input"), { target: { value: "echo new" } });
      fireEvent.click(screen.getByTestId("update-pr-button"));
    };

    beforeEach(() => {
      vi.mocked(api.updateCustomFile).mockImplementation(async (_projectId, id, data) => ({
        custom_file: mockFile({ id, file_content: data.file_content, file_status: "committed_locally" }),
      }));
    });

    test("is locked with a greyed-out Commit Locally", () => {
      render(<Panel />);

      expect(screen.getByTestId("unlock-custom-file-button")).toBeInTheDocument();
      expect(screen.getByTestId("save-button")).toHaveTextContent("💾 Commit Locally");
      expect(screen.getByTestId("save-button")).toBeDisabled();
      expect(screen.queryByTestId("update-pr-button")).not.toBeInTheDocument();
    });

    test("Commit and Update PR adds the edit to the open PR and locks the file again", async () => {
      vi.mocked(workflowsApi.updateWorkflows).mockResolvedValue({ results: { "octo/a on main": { status: "pr_updated" } } } as never);
      const onReposDelivered = vi.fn();
      render(<Panel onReposDelivered={onReposDelivered} />);

      unlockAndEdit();

      await waitFor(() => expect(screen.getByTestId("unlock-custom-file-button")).toBeInTheDocument());
      expect(api.updateCustomFile).toHaveBeenCalledWith(42, 1, expect.objectContaining({ file_content: "echo new" }));
      expect(workflowsApi.updateWorkflows).toHaveBeenCalledWith(
        "proj", ["octo/a"], { customFileIds: [1] }
      );
      expect(onReposDelivered).toHaveBeenCalledWith(["octo/a"]);
      expect(screen.getByTestId("file-content-input")).toHaveValue("echo new");
    });

    test("leaves the save committed locally when the push fails", async () => {
      vi.mocked(workflowsApi.updateWorkflows).mockResolvedValue({ error: "GitHub is down" } as never);
      render(<Panel />);

      unlockAndEdit();

      await waitFor(() => expect(screen.getByTestId("save-button")).toHaveTextContent("💾 Commit Locally"));
      expect(screen.getByTestId("save-button")).not.toBeDisabled();
      expect(screen.queryByTestId("unlock-custom-file-button")).not.toBeInTheDocument();
    });
  });

  describe("CustomFilePanel drift", () => {
    const GITHUB_COPY = "#!/bin/bash\necho hotfix\n";
    const synced = () => mockFile({ file_status: "synced_with_github", git_hash: "a".repeat(40) });
    const drifted = (repo: string, branch = "main", overrides: Partial<CustomFileDriftDetail> = {}): CustomFileDriftDetail => ({
      custom_file_id: 1,
      file_path: ".github/scripts/build.sh",
      repo,
      repo_id: 3,
      branch,
      has_drift: true,
      actionsmanager_sha: "a".repeat(40),
      github_sha: "b".repeat(40),
      last_checked: "2026-10-17T00:00:00+00:00",
      message: `.github/scripts/build.sh content differs between local and ${repo}`,
      ...overrides,
    });

    /** Owns the file and its drift as the page does: a save is handed back, then drift is re-read. */
    function Harness({ initial, drift: initialDrift }: Readonly<{ initial: api.CustomFile; drift: CustomFileDriftDetail[] }>) {
      const [files, setFiles] = useState([initial]);
      const [drift, setDrift] = useState(initialDrift);
      return (
        <CustomFilePanel
          cf={files[0]}
          allFiles={files}
          projectId={42}
          githubUser="testuser"
          drift={drift}
          onChange={(next) => {
            setFiles(next);
            // The page's re-read after the save: the server resolved the drift the save adopted.
            setDrift((rows) => rows.filter((d) => d.github_sha !== "b".repeat(40) || next[0].file_content !== GITHUB_COPY));
          }}
        />
      );
    }

    beforeEach(() => {
      vi.mocked(api.getCustomFileOnGitHub).mockReset();
      vi.mocked(api.updateCustomFile).mockClear();
      vi.mocked(api.updateCustomFile).mockImplementation(async (_projectId, id, data) => ({
        custom_file: mockFile({ id, file_content: data.file_content, file_status: "committed_locally" }),
      }));
    });

    test("shows a drift badge and a banner naming each drifted repo @ branch", () => {
      render(
        <Harness
          initial={synced()}
          drift={[drifted("octo/a"), drifted("octo/b", "dev", { deleted_in_github: true, github_sha: null })]}
        />
      );

      expect(screen.getByTestId("file-drift-badge")).toHaveTextContent("Drift detected");
      expect(screen.getByTestId("custom-file-drift-banner")).toHaveTextContent(
        "'.github/scripts/build.sh' on GitHub has changed or was removed on octo/a @ main, octo/b @ dev"
      );
      // A deleted copy has nothing to load.
      expect(screen.getByRole("button", { name: "Load from octo/a @ main" })).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Load from octo/b @ dev" })).not.toBeInTheDocument();
    });

    test("a file without drift shows neither", () => {
      render(<Harness initial={synced()} drift={[]} />);

      expect(screen.queryByTestId("file-drift-badge")).not.toBeInTheDocument();
      expect(screen.queryByTestId("custom-file-drift-banner")).not.toBeInTheDocument();
    });

    test("Load GitHub version puts GitHub's copy in the form, and Commit Locally adopts it without a remount", async () => {
      vi.mocked(api.getCustomFileOnGitHub).mockResolvedValue({
        repo_id: 3, repo_name: "octo/a", branch: "release/1.0",
        github: { exists: true, content: GITHUB_COPY, sha: "b".repeat(40), path: ".github/scripts/build.sh" },
      });
      render(<Harness initial={synced()} drift={[drifted("octo/a", "release/1.0")]} />);

      fireEvent.click(screen.getByRole("button", { name: "Load GitHub version" }));

      await waitFor(() => expect(screen.getByTestId("file-content-input")).toHaveValue(GITHUB_COPY));
      expect(api.getCustomFileOnGitHub).toHaveBeenCalledWith(42, 1, "octo/a", "release/1.0");
      expect(api.updateCustomFile).not.toHaveBeenCalled();

      fireEvent.click(screen.getByTestId("save-button"));

      await waitFor(() => expect(screen.queryByTestId("custom-file-drift-banner")).not.toBeInTheDocument());
      expect(api.updateCustomFile).toHaveBeenCalledWith(42, 1, expect.objectContaining({ file_content: GITHUB_COPY }));
      expect(screen.queryByTestId("file-drift-badge")).not.toBeInTheDocument();
      expect(screen.getByTestId("file-content-input")).toHaveValue(GITHUB_COPY);
    });

    test("Revert after loading goes back to the saved content", async () => {
      vi.mocked(api.getCustomFileOnGitHub).mockResolvedValue({
        repo_id: 3, repo_name: "octo/a", branch: "main",
        github: { exists: true, content: GITHUB_COPY, sha: "b".repeat(40), path: ".github/scripts/build.sh" },
      });
      render(<Harness initial={synced()} drift={[drifted("octo/a")]} />);
      fireEvent.click(screen.getByRole("button", { name: "Load GitHub version" }));
      await waitFor(() => expect(screen.getByTestId("file-content-input")).toHaveValue(GITHUB_COPY));

      fireEvent.click(screen.getByRole("button", { name: "Revert" }));

      expect(screen.getByTestId("file-content-input")).toHaveValue("#!/bin/bash");
      expect(screen.getByTestId("custom-file-drift-banner")).toBeInTheDocument();
    });

    test("a copy gone from GitHub since the check loads nothing", async () => {
      vi.mocked(api.getCustomFileOnGitHub).mockResolvedValue({
        repo_id: 3, repo_name: "octo/a", branch: "main",
        github: { exists: false, content: null, sha: null, path: ".github/scripts/build.sh" },
      });
      render(<Harness initial={synced()} drift={[drifted("octo/a")]} />);

      fireEvent.click(screen.getByRole("button", { name: "Load GitHub version" }));

      await waitFor(() => expect(api.getCustomFileOnGitHub).toHaveBeenCalled());
      await waitFor(() => expect(screen.getByRole("button", { name: "Load GitHub version" })).not.toBeDisabled());
      expect(screen.getByTestId("file-content-input")).toHaveValue("#!/bin/bash");
    });

    test("a locked file under review shows its drift without offering to load", () => {
      render(<Harness initial={mockFile({ file_status: "under_review" })} drift={[drifted("octo/a")]} />);

      expect(screen.getByTestId("unlock-custom-file-button")).toBeInTheDocument();
      expect(screen.getByTestId("file-drift-badge")).toBeInTheDocument();
      expect(screen.getByTestId("custom-file-drift-banner")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Load GitHub version" })).not.toBeInTheDocument();
    });
  });
});
