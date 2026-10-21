/**
 * Tests for bulk-fixing drift: selecting several drifted files (workflows,
 * custom files, CODEOWNERS) and putting them right together, plus the
 * "identical drift" group selection affordance. The single-row flow is
 * covered by DriftDetection.test.tsx.
 */
import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "@testing-library/jest-dom";
import { vi } from "vitest";
import DriftDetection from "./DriftDetection";
import type { CodeownersDriftDetail, CustomFileDriftDetail, WorkflowDriftDetail, ProjectDriftSummary } from "../api/drift";

vi.mock("../api/drift", () => ({
  getProjectDrift: vi.fn(),
  resolveWorkflowDrift: vi.fn(),
  adoptGithubVersion: vi.fn(),
  bulkResolveWorkflowDrift: vi.fn(),
}));

import { getProjectDrift, bulkResolveWorkflowDrift } from "../api/drift";

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

const mockSummary = (
  drifted: WorkflowDriftDetail[],
  files: { codeowners?: CodeownersDriftDetail[]; customFiles?: CustomFileDriftDetail[] } = {},
): ProjectDriftSummary => ({
  project_id: 1,
  project_name: "proj",
  drift_count: drifted.length,
  drifted_workflows: drifted,
  drifted_codeowners: files.codeowners ?? [],
  drifted_custom_files: files.customFiles ?? [],
  // Recent, so the panel's staleness revalidation does not fire —
  // these tests are about behaviour, not the refresh cadence.
  last_checked: new Date().toISOString(),
});

const codeownersDrift = (overrides: Partial<CodeownersDriftDetail> = {}): CodeownersDriftDetail => ({
  file_id: 3,
  file_path: ".github/CODEOWNERS",
  repo: "org/repo",
  branch: "main",
  has_drift: true,
  actionsmanager_sha: "sha-a",
  github_sha: "sha-co",
  last_checked: "2026-01-01T00:00:00Z",
  message: "",
  ...overrides,
});

const customFileDrift = (overrides: Partial<CustomFileDriftDetail> = {}): CustomFileDriftDetail => ({
  ...codeownersDrift({ file_id: 7, file_path: ".github/scripts/build.sh", github_sha: "sha-cf" }),
  custom_file_id: 7,
  ...overrides,
});

const defaultProps = {
  user: "testuser",
  projectId: 1,
  projectName: "proj",
  selectedRepos: ["org/repo", "org/repo2"],
};

async function openModal(props: Partial<React.ComponentProps<typeof DriftDetection>> = {}) {
  render(<DriftDetection {...defaultProps} {...props} />);
  await waitFor(() => expect(screen.getByTestId("drift-banner")).toBeInTheDocument());
  const user = userEvent.setup();
  await user.click(screen.getByTestId("review-drift-button"));
  return user;
}

describe("DriftDetection - bulk resolve", () => {
  beforeEach(() => {
    vi.mocked(getProjectDrift).mockReset();
    vi.mocked(bulkResolveWorkflowDrift).mockReset();
  });

  test("checkboxes render per row; bulk toolbar only appears once something is selected", async () => {
    vi.mocked(getProjectDrift).mockResolvedValue(
      mockSummary([mockDrift({ workflow_id: 1 }), mockDrift({ workflow_id: 2, repo: "org/repo2" })]),
    );
    const user = await openModal();

    expect(screen.queryByTestId("bulk-action-toolbar")).not.toBeInTheDocument();

    const boxes = screen.getAllByTestId(/^select-drift-/);
    expect(boxes).toHaveLength(2);

    await user.click(boxes[0]);
    expect(screen.getByTestId("bulk-action-toolbar")).toBeInTheDocument();
    expect(screen.getByText("1 of 2 selected")).toBeInTheDocument();
  });

  test("identical drifts (same github_sha) show a 'select all' group affordance", async () => {
    vi.mocked(getProjectDrift).mockResolvedValue(
      mockSummary([
        mockDrift({ workflow_id: 1, repo: "org/repo", github_sha: "same-sha" }),
        mockDrift({ workflow_id: 2, repo: "org/repo2", github_sha: "same-sha" }),
      ]),
    );
    const user = await openModal();

    const groupButtons = screen.getAllByTestId(/^select-group-/);
    expect(groupButtons).toHaveLength(2);
    expect(groupButtons[0]).toHaveTextContent("2 identical — select all");

    await user.click(groupButtons[0]);
    expect(screen.getByText("2 of 2 selected")).toBeInTheDocument();
  });

  test("drifts with different github_sha do not show the group affordance", async () => {
    vi.mocked(getProjectDrift).mockResolvedValue(
      mockSummary([
        mockDrift({ workflow_id: 1, repo: "org/repo", github_sha: "sha-a" }),
        mockDrift({ workflow_id: 2, repo: "org/repo2", github_sha: "sha-b" }),
      ]),
    );
    await openModal();

    expect(screen.queryByTestId(/^select-group-/)).not.toBeInTheDocument();
  });

  test("bulk Keep ActionsManager Version hands every selected file to one campaign and closes the review", async () => {
    vi.mocked(getProjectDrift).mockResolvedValue(
      mockSummary([mockDrift({ workflow_id: 1 })], { codeowners: [codeownersDrift()], customFiles: [customFileDrift()] }),
    );
    const onKeep = vi.fn();
    const user = await openModal({ onKeepActionsManagerVersion: onKeep });

    await user.click(screen.getByTestId("select-all-drifts"));
    await user.click(screen.getByRole("button", { name: /Keep ActionsManager Version \(3\)/i }));

    expect(onKeep.mock.calls[0][0].map((row: { kind: string; fileId: number }) => [row.kind, row.fileId])).toEqual([
      ["workflow", 1], ["custom_file", 7], ["codeowners", 3],
    ]);
    expect(vi.mocked(bulkResolveWorkflowDrift)).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByTestId("drift-modal")).not.toBeInTheDocument());
  });

  test("only pull requests put drift right: nothing restores directly", async () => {
    vi.mocked(getProjectDrift).mockResolvedValue(mockSummary([mockDrift({ workflow_id: 1 })]));
    const user = await openModal();

    await user.click(screen.getByTestId("select-all-drifts"));

    expect(screen.queryByRole("button", { name: /Restore Directly/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Create Fix PR/i })).not.toBeInTheDocument();
  });

  test("Use GitHub Version needs one GitHub copy per file", async () => {
    vi.mocked(getProjectDrift).mockResolvedValue(
      mockSummary([
        mockDrift({ workflow_id: 1, repo: "org/repo", github_sha: "sha-a" }),
        mockDrift({ workflow_id: 1, repo: "org/repo2", github_sha: "sha-c" }),
      ]),
    );
    const user = await openModal();

    await user.click(screen.getByTestId("select-all-drifts"));

    const useGithub = screen.getByTestId("bulk-use-github-button");
    expect(useGithub).toBeDisabled();
    expect(useGithub).toHaveAttribute("title", expect.stringMatching(/different GitHub versions of the same file/));
  });

  test("Use GitHub Version takes workflows through the bulk resolve and files through the page, after a confirmation", async () => {
    vi.mocked(getProjectDrift).mockResolvedValue(
      mockSummary([mockDrift({ workflow_id: 1 })], { customFiles: [customFileDrift()] }),
    );
    vi.mocked(bulkResolveWorkflowDrift).mockResolvedValue({
      success: true,
      results: [{ workflow_id: 1, repo: "org/repo", branch: "main", success: true, message: "ok" }],
    });
    const onUseGithub = vi.fn().mockResolvedValue("used");
    const user = await openModal({ onUseGithubVersionOfFiles: onUseGithub });

    await user.click(screen.getByTestId("select-all-drifts"));
    await user.click(screen.getByTestId("bulk-use-github-button"));
    // A file's GitHub version replaces the project's copy, so it is confirmed first.
    expect(onUseGithub).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Use GitHub version" }));

    await waitFor(() => expect(onUseGithub).toHaveBeenCalled());
    expect(onUseGithub.mock.calls[0][0].map((row: { kind: string }) => row.kind)).toEqual(["custom_file"]);
    expect(vi.mocked(bulkResolveWorkflowDrift)).toHaveBeenCalledWith(1, expect.objectContaining({
      resolution: "use_github",
      items: [{ workflow_id: 1, repo: "org/repo", branch: "main", expected_github_sha: "sha-b" }],
    }));
  });

  test("a mixed success/failure response renders an N resolved, M failed summary", async () => {
    vi.mocked(getProjectDrift).mockResolvedValue(
      mockSummary([
        mockDrift({ workflow_id: 1, repo: "org/repo" }),
        mockDrift({ workflow_id: 2, repo: "org/repo2" }),
      ]),
    );
    vi.mocked(bulkResolveWorkflowDrift).mockResolvedValue({
      success: false,
      results: [
        { workflow_id: 1, repo: "org/repo", branch: "main", success: true, message: "ok" },
        { workflow_id: 2, repo: "org/repo2", branch: "main", success: false, message: "GitHub API failed" },
      ],
    });
    const user = await openModal();

    await user.click(screen.getByTestId("select-all-drifts"));
    await user.click(screen.getByRole("button", { name: /Use GitHub Version \(2\)/i }));

    await waitFor(() => expect(screen.getByText(/1 resolved, 1 failed/i)).toBeInTheDocument());
    expect(screen.getByText(/GitHub API failed/i)).toBeInTheDocument();
  });
});
