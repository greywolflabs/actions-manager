import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import "@testing-library/jest-dom";
import { vi } from "vitest";

vi.mock("../api/projectDeletion", () => ({ getProjectDeletionSummary: vi.fn() }));

import DeleteProjectModal from "./DeleteProjectModal";
import { getProjectDeletionSummary, type ProjectDeletionSummary } from "../api/projectDeletion";

const KEEP_FILES = "Delete the project, keep its files in GitHub";
const REMOVE_FILES =
  "Delete the project and remove its files from GitHub (opens a pull request per repository and branch)";
const DELETE_SECRETS = "Also delete secrets and variables now";
const CI_PATH = ".github/workflows/AM_DMD_ci.yml";

const summary = (overrides: Partial<ProjectDeletionSummary> = {}): ProjectDeletionSummary => ({
  project_name: "doomed",
  project_code: "DMD",
  workflows: [{ name: "ci", is_reusable: false }],
  reusable_workflows: [],
  secrets: [{ name: "AM_DMD_TOKEN", repository: "org/app" }],
  environment_variables: [],
  deployment_environments: [{ name: "production", repository: "org/app" }],
  removal_targets: [
    {
      repository: "org/app",
      branch: "main",
      files: [
        { kind: "workflow", name: "ci", path: CI_PATH },
        { kind: "codeowners", name: ".github/CODEOWNERS", path: ".github/CODEOWNERS" },
      ],
    },
    { repository: "org/app", branch: "release/1", files: [{ kind: "workflow", name: "ci", path: CI_PATH }] },
  ],
  unrecorded_files: [],
  removal_blocked_reason: null,
  ...overrides,
});

const renderModal = async (loaded: ProjectDeletionSummary | Error = summary()) => {
  if (loaded instanceof Error) {
    vi.mocked(getProjectDeletionSummary).mockRejectedValue(loaded);
  } else {
    vi.mocked(getProjectDeletionSummary).mockResolvedValue(loaded);
  }
  const onConfirmDelete = vi.fn();
  render(
    <DeleteProjectModal
      isOpen
      onClose={vi.fn()}
      onConfirmDelete={onConfirmDelete}
      projectName="doomed"
      githubUser="testuser"
    />
  );
  await screen.findByLabelText(KEEP_FILES);
  return onConfirmDelete;
};

const confirm = (name: RegExp) => fireEvent.click(screen.getByRole("button", { name }));

describe("DeleteProjectModal", () => {
  beforeEach(() => vi.clearAllMocks());

  test("offers both choices and starts on keeping the files", async () => {
    const onConfirmDelete = await renderModal();

    expect(await screen.findByLabelText(REMOVE_FILES)).not.toBeChecked();
    expect(screen.getByLabelText(KEEP_FILES)).toBeChecked();
    confirm(/^Delete project$/);

    expect(onConfirmDelete).toHaveBeenCalledWith({
      removeFiles: false,
      deleteSecrets: false,
      deleteDeploymentEnvironments: true,
    });
  });

  test("removing the files lists a pull request per repository and branch with its files", async () => {
    const onConfirmDelete = await renderModal();

    fireEvent.click(await screen.findByLabelText(REMOVE_FILES));

    const plan = screen.getByTestId("removal-plan");
    expect(plan).toHaveTextContent("Pull requests to open (2):");
    expect(plan).toHaveTextContent("org/app@main");
    expect(plan).toHaveTextContent("org/app@release/1");
    expect(plan).toHaveTextContent(".github/CODEOWNERS");
    confirm(/Delete and open pull requests/);
    expect(onConfirmDelete).toHaveBeenCalledWith(expect.objectContaining({ removeFiles: true, deleteSecrets: false }));
  });

  test("names the files no pull request removes", async () => {
    await renderModal(summary({
      unrecorded_files: [{ kind: "workflow", name: "legacy", path: ".github/workflows/AM_DMD_legacy.yml" }],
    }));

    expect(await screen.findByTestId("removal-plan")).toHaveTextContent(
      "Not removed, because ActionsManager has no record of where they were delivered:"
    );
    expect(screen.getByTestId("removal-plan")).toHaveTextContent(".github/workflows/AM_DMD_legacy.yml");
  });

  test("with nothing delivered, removal says the project is deleted now", async () => {
    await renderModal(summary({ removal_targets: [] }));

    fireEvent.click(await screen.findByLabelText(REMOVE_FILES));

    expect(screen.getByTestId("removal-plan")).toHaveTextContent("nothing to remove and the project is deleted now");
    expect(screen.getByRole("button", { name: /^Delete project$/ })).toBeInTheDocument();
  });

  test("a blocked removal is disabled and says why", async () => {
    const reason = "Merge or close its open pull requests first: org/app#3.";
    const onConfirmDelete = await renderModal(summary({ removal_blocked_reason: reason }));

    const removal = await screen.findByLabelText(REMOVE_FILES);
    expect(removal).toBeDisabled();
    expect(screen.getByTestId("removal-blocked-reason")).toHaveTextContent(`Not available: ${reason}`);
    expect(screen.queryByTestId("removal-plan")).not.toBeInTheDocument();
    fireEvent.click(removal);
    confirm(/^Delete project$/);
    expect(onConfirmDelete).toHaveBeenCalledWith(expect.objectContaining({ removeFiles: false }));
  });

  test("the secrets checkbox says plainly it is immediate and not undone by a call-off", async () => {
    const onConfirmDelete = await renderModal();

    const secrets = await screen.findByLabelText(DELETE_SECRETS);
    expect(secrets).not.toBeChecked();
    const option = secrets.closest("label")!;
    expect(option).toHaveTextContent("immediately, without a pull request");
    expect(option).toHaveTextContent("it is not undone if the deletion is called off");
    fireEvent.click(secrets);
    fireEvent.click(screen.getByLabelText(/Keep deployment environments/));
    confirm(/^Delete project$/);

    expect(onConfirmDelete).toHaveBeenCalledWith({
      removeFiles: false,
      deleteSecrets: true,
      deleteDeploymentEnvironments: false,
    });
  });

  test("secrets are deleted with or without removing the files", async () => {
    const onConfirmDelete = await renderModal();

    fireEvent.click(await screen.findByLabelText(REMOVE_FILES));
    fireEvent.click(screen.getByLabelText(DELETE_SECRETS));
    confirm(/Delete and open pull requests/);

    expect(onConfirmDelete).toHaveBeenCalledWith({
      removeFiles: true,
      deleteSecrets: true,
      deleteDeploymentEnvironments: true,
    });
  });

  test("with nothing to delete the secrets checkbox is disabled", async () => {
    await renderModal(summary({ secrets: [], deployment_environments: [] }));

    expect(await screen.findByLabelText(DELETE_SECRETS)).toBeDisabled();
    expect(screen.getByText(/No secrets, variables or deployment environments were found/)).toBeInTheDocument();
  });

  test("when the summary cannot load, only keeping the files is offered", async () => {
    const onConfirmDelete = await renderModal(new Error("network"));

    expect(await screen.findByText(/You can still delete it and keep its files/)).toBeInTheDocument();
    expect(screen.queryByLabelText(REMOVE_FILES)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(DELETE_SECRETS)).not.toBeInTheDocument();
    confirm(/^Delete project$/);
    expect(onConfirmDelete).toHaveBeenCalledWith(expect.objectContaining({ removeFiles: false, deleteSecrets: false }));
  });
});
