import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "@testing-library/jest-dom";
import { vi } from "vitest";
import DriftDetection, { driftSummaryText } from "./DriftDetection";
import type { CodeownersDriftDetail, CustomFileDriftDetail, WorkflowDriftDetail, ProjectDriftSummary } from "../api/drift";

vi.mock("../api/drift", () => ({
  getProjectDrift: vi.fn(),
  resolveWorkflowDrift: vi.fn(),
  adoptGithubVersion: vi.fn(),
}));

vi.mock("../api/workflows", () => ({
  deleteWorkflowFromGitHub: vi.fn().mockResolvedValue({ success: true }),
  deleteWorkflowFromDatabase: vi.fn().mockResolvedValue({ success: true }),
}));

vi.mock("../api/codeowners", () => ({ getCodeowners: vi.fn() }));
vi.mock("../api/customFiles", () => ({ getCustomFileOnGitHub: vi.fn() }));

import { getProjectDrift, resolveWorkflowDrift } from "../api/drift";
import { deleteWorkflowFromGitHub, deleteWorkflowFromDatabase } from "../api/workflows";
import { getCodeowners } from "../api/codeowners";
import { getCustomFileOnGitHub } from "../api/customFiles";

const mockDrift = (overrides: Partial<WorkflowDriftDetail> = {}): WorkflowDriftDetail => ({
  workflow_id: 1,
  workflow_name: "Build",
  workflow_filename: "build.yml",
  repo: "org/repo",
  branch: "main",
  has_drift: true,
  actionsmanager_yaml: "a",
  github_yaml: "b",
  actionsmanager_sha: "sha-a",
  github_sha: "sha-b",
  last_checked: "2026-01-01T00:00:00Z",
  message: "",
  ...overrides,
});

// Mirrors _determine_drift_summary_status in backend/workflows.py.
const savedStatusFor = (driftedCount: number, uncheckedCount: number) => {
  if (driftedCount) return "drifted";
  if (uncheckedCount) return "check_failed";
  return "clean";
};

const mockSummary = (
  drifted: WorkflowDriftDetail[],
  uncheckedCount = 0,
): ProjectDriftSummary => ({
  project_id: 1,
  project_name: "proj",
  drift_count: drifted.length,
  drifted_workflows: drifted,
  last_checked: new Date().toISOString(),
  unchecked_count: uncheckedCount,
  drift_status: savedStatusFor(drifted.length, uncheckedCount),
});

const defaultProps = {
  user: "testuser",
  projectId: 1,
  projectName: "proj",
  selectedRepos: ["org/repo"],
};

/** Render, open the drift modal, and expand the diff for the first drift. */
async function openDiff(
  user: ReturnType<typeof userEvent.setup>,
  props: Partial<React.ComponentProps<typeof DriftDetection>> = {},
) {
  render(<DriftDetection {...defaultProps} {...props} />);
  await waitFor(() => expect(screen.getByTestId("drift-banner")).toBeInTheDocument());
  await user.click(screen.getByTestId("review-drift-button"));
  await user.click(screen.getByRole("button", { name: /View Diff/i }));
}

describe("DriftDetection", () => {
  beforeEach(() => {
    vi.mocked(getProjectDrift).mockReset();
    vi.mocked(resolveWorkflowDrift).mockReset();
  });

  test("shows a not-checked-yet status row while the request is pending and there is no seeded drift", () => {
    vi.mocked(getProjectDrift).mockReturnValue(new Promise(() => {}));
    render(<DriftDetection {...defaultProps} />);
    expect(screen.queryByText(/Checking for workflow drift/i)).not.toBeInTheDocument();
    expect(screen.getByTestId("drift-status-row")).toHaveTextContent("Not checked yet");
    expect(screen.getByTestId("drift-status-row")).toHaveTextContent("❔");
    expect(screen.queryByTestId("drift-banner")).not.toBeInTheDocument();
  });

  test("a verified-clean project shows a checkmark, distinct from a never-checked project", async () => {
    vi.mocked(getProjectDrift).mockResolvedValueOnce(mockSummary([]));
    render(<DriftDetection {...defaultProps} />);

    await waitFor(() => expect(screen.getByTestId("drift-status-row")).toHaveTextContent("No drift detected"));
    expect(screen.getByTestId("drift-status-row")).toHaveTextContent("✅");
    expect(screen.getByTestId("drift-status-row")).not.toHaveTextContent("❔");
  });

  test("switching to a project with no repos clears the previous project's drift status instead of showing stale data", async () => {
    vi.mocked(getProjectDrift).mockResolvedValueOnce(mockSummary([]));
    const { rerender } = render(<DriftDetection {...defaultProps} />);
    await waitFor(() => expect(screen.getByTestId("drift-status-row")).toHaveTextContent("No drift detected"));

    rerender(<DriftDetection {...defaultProps} projectId={2} projectName="proj2" selectedRepos={[]} />);

    await waitFor(() => expect(screen.getByTestId("drift-status-row")).toHaveTextContent("Not checked yet"));
    expect(screen.getByTestId("drift-status-row")).toHaveTextContent("❔");
  });

  test("clicking Check Now on a clean project triggers a live refresh", async () => {
    const user = userEvent.setup();
    vi.mocked(getProjectDrift).mockResolvedValueOnce(mockSummary([]));
    render(<DriftDetection {...defaultProps} />);
    await waitFor(() => expect(screen.getByTestId("drift-status-row")).toBeInTheDocument());

    vi.mocked(getProjectDrift).mockResolvedValueOnce(mockSummary([]));
    await user.click(screen.getByTestId("drift-inline-check-now-button"));

    await waitFor(() =>
      expect(vi.mocked(getProjectDrift)).toHaveBeenLastCalledWith(1, "testuser", { refresh: true }),
    );
  });

  test("surfaces the stale reason inline on a clean project without needing the modal", async () => {
    vi.mocked(getProjectDrift).mockResolvedValue({
      ...mockSummary([]),
      stale_reason: "Automatic drift checks are paused: no saved GitHub token.",
    });
    render(<DriftDetection {...defaultProps} />);

    await waitFor(() => expect(screen.getByTestId("drift-status-row")).toHaveTextContent(
      /Automatic drift checks are paused/i,
    ));
  });

  test("renders the seeded banner synchronously, before the live check resolves", () => {
    vi.mocked(getProjectDrift).mockReturnValue(new Promise(() => {}));
    render(<DriftDetection {...defaultProps} seededDriftNames={["Build", "Deploy"]} />);

    // Synchronous assertion (no waitFor): the banner must be in the very first
    // paint, otherwise it pops in later and shifts the layout.
    expect(screen.getByTestId("drift-banner")).toBeInTheDocument();
    expect(screen.getByText("2 workflows changed in GitHub")).toBeInTheDocument();
  });

  test("disables Review Drift until the live check supplies resolvable rows", async () => {
    vi.mocked(getProjectDrift).mockReturnValue(new Promise(() => {}));
    const { rerender } = render(
      <DriftDetection {...defaultProps} seededDriftNames={["Build"]} refreshSignal={0} />,
    );
    expect(screen.getByTestId("review-drift-button")).toBeDisabled();

    vi.mocked(getProjectDrift).mockResolvedValue(mockSummary([mockDrift()]));
    rerender(<DriftDetection {...defaultProps} seededDriftNames={["Build"]} refreshSignal={1} />);

    await waitFor(() => expect(screen.getByTestId("review-drift-button")).toBeEnabled());
    expect(screen.getByText("1 workflow changed in GitHub")).toBeInTheDocument();
  });

  test("live check clears the seeded banner when the drift is already resolved", async () => {
    vi.mocked(getProjectDrift).mockResolvedValue(mockSummary([]));
    render(
      <DriftDetection {...defaultProps} seededDriftNames={["Build"]} />,
    );
    expect(screen.getByTestId("drift-banner")).toBeInTheDocument();

    await waitFor(() => expect(screen.queryByTestId("drift-banner")).not.toBeInTheDocument());
    expect(screen.getByTestId("drift-status-row")).toHaveTextContent("No drift detected");
  });

  test("a failed first check keeps the seeded banner instead of blanking it", async () => {
    vi.mocked(getProjectDrift).mockRejectedValue(new Error("network error"));
    render(<DriftDetection {...defaultProps} seededDriftNames={["Build"]} />);

    await waitFor(() => expect(vi.mocked(getProjectDrift)).toHaveBeenCalled());
    expect(screen.getByTestId("drift-banner")).toBeInTheDocument();
    // Enabled despite no live rows so the failure message stays reachable.
    await waitFor(() => expect(screen.getByTestId("review-drift-button")).toBeEnabled());
  });

  test("opening the panel costs no GitHub calls — one cached read, no refresh", async () => {
    // The background sweep keeps stored state fresh, so re-checking on mount
    // would spend rate limit re-answering a question already answered.
    vi.mocked(getProjectDrift).mockResolvedValue(mockSummary([mockDrift()]));
    render(<DriftDetection {...defaultProps} />);

    await waitFor(() => expect(screen.getByTestId("drift-banner")).toBeInTheDocument());
    expect(vi.mocked(getProjectDrift)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(getProjectDrift)).toHaveBeenCalledWith(1, "testuser", { refresh: undefined });
  });

  test("a stale-state reason is surfaced instead of the timestamp silently freezing", async () => {
    const user = userEvent.setup();
    vi.mocked(getProjectDrift).mockResolvedValue({
      ...mockSummary([mockDrift()]),
      stale_reason: "Automatic drift checks are paused: no saved GitHub token.",
    });
    render(<DriftDetection {...defaultProps} />);
    await waitFor(() => expect(screen.getByTestId("drift-banner")).toBeInTheDocument());
    await user.click(screen.getByTestId("review-drift-button"));

    expect(screen.getByTestId("drift-stale-reason")).toHaveTextContent(
      /Automatic drift checks are paused/i,
    );
  });

  test("no warning is shown when automatic checking is working", async () => {
    const user = userEvent.setup();
    vi.mocked(getProjectDrift).mockResolvedValue(mockSummary([mockDrift()]));
    render(<DriftDetection {...defaultProps} />);
    await waitFor(() => expect(screen.getByTestId("drift-banner")).toBeInTheDocument());
    await user.click(screen.getByTestId("review-drift-button"));

    expect(screen.queryByTestId("drift-stale-reason")).not.toBeInTheDocument();
  });

  test("Check Now triggers a live refresh and updates the last-checked time", async () => {
    const user = userEvent.setup();
    vi.mocked(getProjectDrift).mockResolvedValueOnce(mockSummary([mockDrift()]));
    render(<DriftDetection {...defaultProps} />);
    await waitFor(() => expect(screen.getByTestId("drift-banner")).toBeInTheDocument());
    await user.click(screen.getByTestId("review-drift-button"));

    vi.mocked(getProjectDrift).mockResolvedValueOnce({
      ...mockSummary([mockDrift()]),
      last_checked: "2026-02-02T00:00:00Z",
    });
    await user.click(screen.getByTestId("drift-check-now-button"));

    await waitFor(() =>
      expect(vi.mocked(getProjectDrift)).toHaveBeenLastCalledWith(1, "testuser", { refresh: true }),
    );
    await waitFor(() =>
      expect(screen.getByTestId("drift-last-checked")).toHaveTextContent(
        new Date("2026-02-02T00:00:00Z").toLocaleString(),
      ),
    );
  });

  test("an unchecked repo is surfaced instead of reading as clean", async () => {
    // No drift found, but GitHub couldn't be reached for some repos — showing
    // nothing would imply everything is in sync.
    vi.mocked(getProjectDrift).mockResolvedValue(mockSummary([], 2));
    render(<DriftDetection {...defaultProps} />);

    const row = await screen.findByTestId("drift-status-row");
    expect(row).toHaveTextContent(/Couldn't check 2 workflows/i);
  });

  test("a fully successful clean check renders the clean status row", async () => {
    vi.mocked(getProjectDrift).mockResolvedValue(mockSummary([], 0));
    render(<DriftDetection {...defaultProps} />);

    await waitFor(() => expect(vi.mocked(getProjectDrift)).toHaveBeenCalled());
    expect(screen.getByTestId("drift-status-row")).toHaveTextContent("No drift detected");
    expect(screen.queryByTestId("drift-banner")).not.toBeInTheDocument();
  });

  test("a saved check_failed result warns instead of showing the green check", async () => {
    // The stored path never knows the unchecked count; only the saved status
    // says some pairs could not be checked.
    vi.mocked(getProjectDrift).mockResolvedValue({
      ...mockSummary([]),
      drift_status: "check_failed",
      stale_reason: "12 workflow/repo pair(s) could not be checked",
    });
    render(<DriftDetection {...defaultProps} />);

    const row = await screen.findByTestId("drift-status-row");
    await waitFor(() => expect(row).toHaveTextContent("Couldn't check some workflows"));
    expect(row).toHaveTextContent("12 workflow/repo pair(s) could not be checked");
    expect(row).toHaveTextContent("❔");
    expect(row).not.toHaveTextContent("✅");
    expect(row).not.toHaveTextContent("No drift detected");
  });

  test("a saved unknown status with a past check is not shown as clean", async () => {
    vi.mocked(getProjectDrift).mockResolvedValue({ ...mockSummary([]), drift_status: "unknown" });
    render(<DriftDetection {...defaultProps} />);

    const row = await screen.findByTestId("drift-status-row");
    await waitFor(() => expect(row).toHaveTextContent("Drift status unknown"));
    expect(row).not.toHaveTextContent("✅");
  });

  test("a saved drifted status with no stored rows is not shown as clean", async () => {
    vi.mocked(getProjectDrift).mockResolvedValue({ ...mockSummary([]), drift_status: "drifted" });
    render(<DriftDetection {...defaultProps} />);

    const row = await screen.findByTestId("drift-status-row");
    await waitFor(() => expect(row).toHaveTextContent("Drift detected on the last check"));
    expect(row).not.toHaveTextContent("✅");
  });

  test("Check Now updates the status row in place, without a remount", async () => {
    const user = userEvent.setup();
    vi.mocked(getProjectDrift).mockResolvedValueOnce({ ...mockSummary([]), drift_status: "check_failed" });
    render(<DriftDetection {...defaultProps} />);
    const row = await screen.findByTestId("drift-status-row");
    await waitFor(() => expect(row).toHaveTextContent("Couldn't check some workflows"));

    vi.mocked(getProjectDrift).mockResolvedValueOnce(mockSummary([]));
    await user.click(screen.getByTestId("drift-inline-check-now-button"));

    await waitFor(() => expect(row).toHaveTextContent("No drift detected"));
    expect(row).toHaveTextContent("✅");
    expect(screen.getByTestId("drift-status-row")).toBe(row);
    expect(vi.mocked(getProjectDrift)).toHaveBeenLastCalledWith(1, "testuser", { refresh: true });
  });

  describe("Workflow deleted in GitHub", () => {
    const deletedDrift = () =>
      mockDrift({
        deleted_in_github: true,
        github_yaml: null,
        github_sha: null,
        message: "Workflow was deleted from org/repo",
        affected_repos: ["org/repo2"],
      });

    async function openDeletedDiff(props: Partial<React.ComponentProps<typeof DriftDetection>> = {}) {
      const user = userEvent.setup();
      vi.mocked(getProjectDrift).mockResolvedValue(mockSummary([deletedDrift()]));
      render(<DriftDetection {...defaultProps} {...props} />);
      await waitFor(() => expect(screen.getByTestId("drift-banner")).toBeInTheDocument());
      await user.click(screen.getByTestId("review-drift-button"));
      await user.click(screen.getByRole("button", { name: /View Diff/i }));
      return user;
    }

    test("is labelled deleted rather than shown as generic drift", async () => {
      await openDeletedDiff();

      expect(screen.getByTestId("drift-status-deleted")).toHaveTextContent("Deleted in GitHub");
    });

    test("explains the deletion instead of rendering a blank diff pane", async () => {
      await openDeletedDiff();

      // The reported bug: the GitHub column rendered as one empty line.
      expect(screen.getByTestId("deleted-in-github-panel")).toBeInTheDocument();
      expect(screen.queryByText("Current GitHub version")).not.toBeInTheDocument();
      expect(screen.getByText(/no longer exists in org\/repo/i)).toBeInTheDocument();
    });

    test("does not offer to adopt content that does not exist", async () => {
      await openDeletedDiff();

      expect(screen.queryByTestId("adopt-github-version-button")).not.toBeInTheDocument();
      expect(screen.getByTestId("keep-actionsmanager-button")).toBeInTheDocument();
      expect(screen.getByTestId("delete-everywhere-button")).toBeInTheDocument();
    });

    test("delete everywhere names every repo before doing anything", async () => {
      const user = await openDeletedDiff();

      await user.click(screen.getByTestId("delete-everywhere-button"));

      expect(await screen.findByText(/org\/repo, org\/repo2/)).toBeInTheDocument();
      expect(screen.getByText(/version history included, when they merge/i)).toBeInTheDocument();
      // Nothing deleted until the user confirms.
      expect(vi.mocked(deleteWorkflowFromGitHub)).not.toHaveBeenCalled();
    });

    test("confirming opens pull requests removing it, and leaves the workflow until they merge", async () => {
      const onProjectChanged = vi.fn();
      const user = await openDeletedDiff({ onProjectChanged });

      await user.click(screen.getByTestId("delete-everywhere-button"));
      await user.click(await screen.findByRole("button", { name: /Delete everywhere/i }));

      // A removal campaign: drift is put right through pull requests only, and
      // the workflow leaves ActionsManager when they merge, not now.
      await waitFor(() =>
        expect(vi.mocked(deleteWorkflowFromGitHub)).toHaveBeenCalledWith(
          "testuser", ["org/repo", "org/repo2"], "Build", "", "proj", "campaign",
        ),
      );
      expect(vi.mocked(deleteWorkflowFromDatabase)).not.toHaveBeenCalled();
      // The page shows the workflow marked for removal without a refresh.
      expect(onProjectChanged).toHaveBeenCalledTimes(1);
    });
  });

  test("shows the drift banner once a drifted response resolves", async () => {
    vi.mocked(getProjectDrift).mockResolvedValue(mockSummary([mockDrift()]));
    const onDriftLoaded = vi.fn();
    render(<DriftDetection {...defaultProps} onDriftLoaded={onDriftLoaded} />);
    await waitFor(() => expect(screen.getByTestId("drift-banner")).toBeInTheDocument());
    expect(onDriftLoaded).toHaveBeenCalledWith([mockDrift()], [], []);
  });

  test("shows the clean status row instead of the drift banner when the response reports no drift", async () => {
    vi.mocked(getProjectDrift).mockResolvedValue(mockSummary([]));
    const onDriftLoaded = vi.fn();
    render(<DriftDetection {...defaultProps} onDriftLoaded={onDriftLoaded} />);
    await waitFor(() => expect(onDriftLoaded).toHaveBeenCalledWith([], [], []));
    expect(screen.queryByTestId("drift-banner")).not.toBeInTheDocument();
    expect(screen.getByTestId("drift-status-row")).toHaveTextContent("No drift detected");
  });

  test("a failed check does not clear a previously known drift state", async () => {
    vi.mocked(getProjectDrift).mockResolvedValueOnce(mockSummary([mockDrift()]));
    const onDriftLoaded = vi.fn();
    const { rerender } = render(
      <DriftDetection {...defaultProps} onDriftLoaded={onDriftLoaded} refreshSignal={0} />,
    );
    await waitFor(() => expect(screen.getByTestId("drift-banner")).toBeInTheDocument());

    vi.mocked(getProjectDrift).mockRejectedValueOnce(new Error("network error"));
    rerender(<DriftDetection {...defaultProps} onDriftLoaded={onDriftLoaded} refreshSignal={1} />);

    await waitFor(() => expect(vi.mocked(getProjectDrift)).toHaveBeenCalledTimes(2));
    expect(screen.getByTestId("drift-banner")).toBeInTheDocument();
    expect(onDriftLoaded).not.toHaveBeenLastCalledWith([], [], []);
  });

  test("ignores a stale response from a previously viewed project", async () => {
    let resolveFirst!: (v: ProjectDriftSummary) => void;
    const first = new Promise<ProjectDriftSummary>((resolve) => {
      resolveFirst = resolve;
    });
    vi.mocked(getProjectDrift).mockReturnValueOnce(first);

    const onDriftLoaded = vi.fn();
    const { rerender } = render(
      <DriftDetection {...defaultProps} projectId={1} onDriftLoaded={onDriftLoaded} />,
    );

    vi.mocked(getProjectDrift).mockResolvedValueOnce(mockSummary([]));
    rerender(<DriftDetection {...defaultProps} projectId={2} onDriftLoaded={onDriftLoaded} />);
    await waitFor(() => expect(onDriftLoaded).toHaveBeenCalledWith([], [], []));

    // The stale first-project response resolves after project 2's clean result already landed.
    resolveFirst(mockSummary([mockDrift()]));
    await Promise.resolve();
    await Promise.resolve();

    expect(onDriftLoaded).toHaveBeenLastCalledWith([], [], []);
    expect(screen.queryByTestId("drift-banner")).not.toBeInTheDocument();
  });

  test("offers the same two actions as every file, and nothing that skips a pull request", async () => {
    const user = userEvent.setup();
    vi.mocked(getProjectDrift).mockResolvedValue(mockSummary([mockDrift()]));
    await openDiff(user);

    expect(screen.getByRole("button", { name: /Keep ActionsManager Version/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Use GitHub Version/i })).toBeInTheDocument();
    expect(screen.getByText(/carrying only this file/i)).toBeInTheDocument();
    expect(screen.getByText(/Recommended/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Restore Directly/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Create Fix Pull Request/i })).not.toBeInTheDocument();
    // Repo + target branch surfaced prominently above the diff.
    expect(screen.getByText(/Target branch:/i)).toBeInTheDocument();
    // Clarified diff column headers.
    expect(screen.getByText("ActionsManager managed version")).toBeInTheDocument();
    expect(screen.getByText("Current GitHub version")).toBeInTheDocument();
  });

  test("Keep ActionsManager Version hands exactly this workflow to the campaign and closes the review", async () => {
    const user = userEvent.setup();
    const onKeep = vi.fn();
    vi.mocked(getProjectDrift).mockResolvedValue(mockSummary([mockDrift()]));
    await openDiff(user, { onKeepActionsManagerVersion: onKeep });

    await user.click(screen.getByRole("button", { name: /Keep ActionsManager Version/i }));

    expect(onKeep).toHaveBeenCalledTimes(1);
    const [[rows]] = onKeep.mock.calls;
    expect(rows.map((row: { kind: string; fileId: number; repo: string }) => [row.kind, row.fileId, row.repo]))
      .toEqual([["workflow", 1, "org/repo"]]);
    // Nothing is written from here: the campaign modal opens for review.
    expect(vi.mocked(resolveWorkflowDrift)).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByTestId("drift-modal")).not.toBeInTheDocument());
  });

  test("Use GitHub Version on a workflow opens its scope choice, without a direct commit", async () => {
    const user = userEvent.setup();
    vi.mocked(getProjectDrift).mockResolvedValue(mockSummary([mockDrift()]));
    await openDiff(user);

    await user.click(screen.getByRole("button", { name: /Use GitHub Version/i }));

    expect(await screen.findByTestId("adopt-github-version-modal")).toBeInTheDocument();
    expect(screen.queryByText(/Direct commit/i)).not.toBeInTheDocument();
  });
});

describe("DriftDetection – CODEOWNERS", () => {
  const codeownersDrift = (overrides: Partial<CodeownersDriftDetail> = {}): CodeownersDriftDetail => ({
    file_path: ".github/CODEOWNERS",
    repo: "org/repo",
    repo_id: 3,
    branch: "main",
    has_drift: true,
    actionsmanager_sha: "sha-a",
    github_sha: "sha-b",
    last_checked: "2026-01-01T00:00:00Z",
    message: "CODEOWNERS content differs between local and org/repo",
    ...overrides,
  });

  const withCodeowners = (workflows: WorkflowDriftDetail[], codeowners: CodeownersDriftDetail[]): ProjectDriftSummary => ({
    ...mockSummary(workflows),
    drift_count: workflows.length + codeowners.length,
    drifted_codeowners: codeowners,
    drift_status: "drifted",
  });

  beforeEach(() => {
    vi.mocked(getProjectDrift).mockReset();
  });

  test.each([
    [0, false, ""],
    [0, true, "CODEOWNERS changed in GitHub"],
    [1, false, "1 workflow changed in GitHub"],
    [2, true, "2 workflows and CODEOWNERS changed in GitHub"],
  ])("summarizes %i drifted workflows with CODEOWNERS drifted=%s", (count, codeowners, text) => {
    expect(driftSummaryText(count, codeowners)).toBe(text);
  });

  test("a project whose only drift is CODEOWNERS shows the banner, not a clean status", async () => {
    vi.mocked(getProjectDrift).mockResolvedValue(withCodeowners([], [codeownersDrift()]));
    render(<DriftDetection {...defaultProps} />);

    await waitFor(() => expect(screen.getByTestId("drift-banner")).toHaveTextContent("CODEOWNERS changed in GitHub"));
    expect(screen.queryByTestId("drift-status-row")).not.toBeInTheDocument();
  });

  test("the seeded banner counts CODEOWNERS before the stored drift loads", () => {
    vi.mocked(getProjectDrift).mockReturnValue(new Promise(() => {}));
    render(<DriftDetection {...defaultProps} seededDriftNames={["Build"]} seededCodeownersDrift />);

    expect(screen.getByText("1 workflow and CODEOWNERS changed in GitHub")).toBeInTheDocument();
  });

  test("hands the CODEOWNERS rows to the page alongside the workflow rows", async () => {
    const onDriftLoaded = vi.fn();
    const row = codeownersDrift();
    vi.mocked(getProjectDrift).mockResolvedValue(withCodeowners([mockDrift()], [row]));
    render(<DriftDetection {...defaultProps} onDriftLoaded={onDriftLoaded} />);

    await waitFor(() => expect(onDriftLoaded).toHaveBeenCalledWith([mockDrift()], [row], []));
  });

  test("lists each drifted branch in the one table, with its kind, its diff and its two actions", async () => {
    const user = userEvent.setup();
    const onKeep = vi.fn();
    vi.mocked(getCodeowners).mockResolvedValue({
      success: true, repo_id: 3, repo_name: "org/repo", branch: "main", local: null,
      github: { exists: true, content: "* @someone-else\n", sha: "sha-b", path: ".github/CODEOWNERS" },
    });
    vi.mocked(getProjectDrift).mockResolvedValue(withCodeowners([], [
      codeownersDrift({ file_id: 4 }),
      codeownersDrift({ file_id: 4, repo: "org/other", branch: "release/1", deleted_in_github: true, github_sha: null }),
    ]));
    render(<DriftDetection {...defaultProps} onKeepActionsManagerVersion={onKeep}
      contentOf={() => "* @org/team\n"} />);
    await waitFor(() => expect(screen.getByTestId("review-drift-button")).toBeEnabled());

    await user.click(screen.getByTestId("review-drift-button"));
    const rows = screen.getAllByRole("row").slice(1);

    expect(screen.getByText("⚠️ Drift Detected")).toBeInTheDocument();
    expect(rows[0]).toHaveTextContent(".github/CODEOWNERSCODEOWNERSorg/repomainDrift detected");
    expect(rows[1]).toHaveTextContent(".github/CODEOWNERSCODEOWNERSorg/otherrelease/1Deleted in GitHub");

    await user.click(screen.getAllByRole("button", { name: /View Diff/i })[0]);
    // GitHub's side read where the check read it; ActionsManager's from the page.
    expect(await screen.findByText("* @someone-else")).toBeInTheDocument();
    expect(screen.getByText("* @org/team")).toBeInTheDocument();
    expect(vi.mocked(getCodeowners)).toHaveBeenCalledWith(
      "org/repo", "testuser", "proj", { branch: "main", path: ".github/CODEOWNERS" },
    );

    await user.click(screen.getByRole("button", { name: /Keep ActionsManager Version/i }));
    expect(onKeep.mock.calls[0][0].map((row: { kind: string; fileId: number }) => [row.kind, row.fileId]))
      .toEqual([["codeowners", 4]]);
  });

  test("Use GitHub Version of CODEOWNERS is confirmed, then saved by the page", async () => {
    const user = userEvent.setup();
    const onUseGithub = vi.fn().mockResolvedValue(".github/CODEOWNERS now holds the GitHub version from org/repo.");
    vi.mocked(getCodeowners).mockResolvedValue({
      success: true, repo_id: 3, repo_name: "org/repo", branch: "main", local: null,
      github: { exists: true, content: "x", sha: "sha-b", path: ".github/CODEOWNERS" },
    });
    vi.mocked(getProjectDrift).mockResolvedValue(withCodeowners([], [codeownersDrift({ file_id: 4 })]));
    render(<DriftDetection {...defaultProps} onUseGithubVersionOfFiles={onUseGithub} />);
    await waitFor(() => expect(screen.getByTestId("review-drift-button")).toBeEnabled());
    await user.click(screen.getByTestId("review-drift-button"));
    await user.click(screen.getByRole("button", { name: /View Diff/i }));

    await user.click(screen.getByRole("button", { name: /Use GitHub Version/i }));
    expect(onUseGithub).not.toHaveBeenCalled();
    expect(screen.getByText(/becomes the project's version/i)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Use GitHub version" }));

    await waitFor(() => expect(onUseGithub).toHaveBeenCalledTimes(1));
    expect(onUseGithub.mock.calls[0][0][0]).toMatchObject({ kind: "codeowners", fileId: 4, repo: "org/repo", branch: "main" });
  });

  test("shows the page's error detail when using the GitHub version fails", async () => {
    const user = userEvent.setup();
    const onUseGithub = vi.fn().mockRejectedValue({
      response: { data: { detail: "CODEOWNERS syntax error on line 2" } },
      message: "Request failed with status code 400",
    });
    vi.mocked(getCodeowners).mockResolvedValue({
      success: true, repo_id: 3, repo_name: "org/repo", branch: "main", local: null,
      github: { exists: true, content: "x", sha: "sha-b", path: ".github/CODEOWNERS" },
    });
    vi.mocked(getProjectDrift).mockResolvedValue(withCodeowners([], [codeownersDrift({ file_id: 4 })]));
    render(<DriftDetection {...defaultProps} onUseGithubVersionOfFiles={onUseGithub} />);
    await waitFor(() => expect(screen.getByTestId("review-drift-button")).toBeEnabled());
    await user.click(screen.getByTestId("review-drift-button"));
    await user.click(screen.getByRole("button", { name: /View Diff/i }));

    await user.click(screen.getByRole("button", { name: /Use GitHub Version/i }));
    await user.click(screen.getByRole("button", { name: "Use GitHub version" }));

    expect(await screen.findByText("CODEOWNERS syntax error on line 2")).toBeInTheDocument();
    expect(screen.queryByText(/Request failed with status code 400/i)).not.toBeInTheDocument();
  });
});

describe("DriftDetection – custom files", () => {
  const customFileDrift = (overrides: Partial<CustomFileDriftDetail> = {}): CustomFileDriftDetail => ({
    custom_file_id: 7,
    file_path: "scripts/build.sh",
    repo: "org/repo",
    repo_id: 3,
    branch: "main",
    has_drift: true,
    actionsmanager_sha: "sha-a",
    github_sha: "sha-b",
    last_checked: "2026-01-01T00:00:00Z",
    message: "scripts/build.sh content differs between local and org/repo",
    ...overrides,
  });

  const withCustomFiles = (workflows: WorkflowDriftDetail[], customFiles: CustomFileDriftDetail[]): ProjectDriftSummary => ({
    ...mockSummary(workflows),
    drift_count: workflows.length + customFiles.length,
    drifted_custom_files: customFiles,
    drift_status: "drifted",
  });

  beforeEach(() => {
    vi.mocked(getProjectDrift).mockReset();
  });

  test.each([
    [0, false, 1, "1 custom file changed in GitHub"],
    [0, true, 2, "2 custom files and CODEOWNERS changed in GitHub"],
    [1, true, 1, "1 workflow, 1 custom file and CODEOWNERS changed in GitHub"],
    [3, false, 2, "3 workflows and 2 custom files changed in GitHub"],
  ])("summarizes %i workflows, CODEOWNERS=%s and %i custom files", (workflows, codeowners, customFiles, text) => {
    expect(driftSummaryText(workflows, codeowners, customFiles)).toBe(text);
  });

  test("a project whose only drift is a custom file shows the banner, counting files not branches", async () => {
    vi.mocked(getProjectDrift).mockResolvedValue(withCustomFiles([], [
      customFileDrift(), customFileDrift({ repo: "org/other" }),
    ]));
    render(<DriftDetection {...defaultProps} />);

    await waitFor(() => expect(screen.getByTestId("drift-banner")).toHaveTextContent("1 custom file changed in GitHub"));
    expect(screen.queryByTestId("drift-status-row")).not.toBeInTheDocument();
  });

  test("the seeded banner counts custom files before the stored drift loads", () => {
    vi.mocked(getProjectDrift).mockReturnValue(new Promise(() => {}));
    render(<DriftDetection {...defaultProps} seededCustomFileDriftCount={2} />);

    expect(screen.getByText("2 custom files changed in GitHub")).toBeInTheDocument();
  });

  test("hands the custom-file rows to the page", async () => {
    const onDriftLoaded = vi.fn();
    const row = customFileDrift();
    vi.mocked(getProjectDrift).mockResolvedValue(withCustomFiles([mockDrift()], [row]));
    render(<DriftDetection {...defaultProps} onDriftLoaded={onDriftLoaded} />);

    await waitFor(() => expect(onDriftLoaded).toHaveBeenCalledWith([mockDrift()], [], [row]));
  });

  test("lists each drifted file and branch in the one table, its diff read from the file's own GitHub copy", async () => {
    const user = userEvent.setup();
    vi.mocked(getCustomFileOnGitHub).mockResolvedValue({
      repo_id: 3, repo_name: "org/repo", branch: "main",
      github: { exists: true, content: "echo changed\n", sha: "sha-b", path: "scripts/build.sh" },
    });
    vi.mocked(getProjectDrift).mockResolvedValue(withCustomFiles([], [
      customFileDrift(),
      customFileDrift({ repo: "org/other", branch: "release/1", deleted_in_github: true, github_sha: null }),
    ]));
    render(<DriftDetection {...defaultProps} contentOf={() => "echo build\n"} />);
    await waitFor(() => expect(screen.getByTestId("review-drift-button")).toBeEnabled());

    await user.click(screen.getByTestId("review-drift-button"));
    const rows = screen.getAllByRole("row").slice(1);

    expect(rows[0]).toHaveTextContent("scripts/build.shCustom fileorg/repomainDrift detected");
    expect(rows[1]).toHaveTextContent("scripts/build.shCustom fileorg/otherrelease/1Deleted in GitHub");

    await user.click(screen.getAllByRole("button", { name: /View Diff/i })[0]);
    expect(await screen.findByText("echo changed")).toBeInTheDocument();
    expect(vi.mocked(getCustomFileOnGitHub)).toHaveBeenCalledWith(1, 7, "org/repo", "main");
  });

  test("a deleted custom file can be put back, and is not deleted everywhere from here", async () => {
    const user = userEvent.setup();
    vi.mocked(getProjectDrift).mockResolvedValue(withCustomFiles([], [
      customFileDrift({ deleted_in_github: true, github_sha: null }),
    ]));
    render(<DriftDetection {...defaultProps} />);
    await waitFor(() => expect(screen.getByTestId("review-drift-button")).toBeEnabled());
    await user.click(screen.getByTestId("review-drift-button"));
    await user.click(screen.getByRole("button", { name: /View Diff/i }));

    expect(screen.getByTestId("keep-actionsmanager-button")).toBeInTheDocument();
    expect(screen.queryByTestId("delete-everywhere-button")).not.toBeInTheDocument();
    expect(screen.queryByTestId("adopt-github-version-button")).not.toBeInTheDocument();
  });
});
