import apiClient from "./apiClient";
import { applyPin, fetchActionUsage, fetchLatestVersion, previewPin, updateWorkflow, type ActionUsage, type PinPreview } from "./actionUsage";

import type { Mocked } from "vitest";
vi.mock("./apiClient", () => ({
  __esModule: true,
  default: { get: vi.fn(), post: vi.fn() },
}));

const mockedApiClient = apiClient as Mocked<typeof apiClient>;

describe("action usage API", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("returns the inventory from /api/action-usage", async () => {
    const usage: ActionUsage = {
      actions: [],
      other: { local: 0, docker: 0 },
      summary: { action_count: 0, workflows_read: 0, unpinned_workflows: 0 },
    };
    mockedApiClient.get.mockResolvedValue({ data: usage });

    await expect(fetchActionUsage()).resolves.toEqual(usage);
    expect(mockedApiClient.get).toHaveBeenCalledWith("/api/action-usage");
  });

  it("asks for the latest release when no ref is given, and for the ref when one is", async () => {
    const preview: PinPreview = { action: "actions/checkout", ref: "v5.0.0", source: "latest release", sha: "a".repeat(40), comment: "v5.0.0", projects: [] };
    mockedApiClient.get.mockResolvedValue({ data: preview });

    await previewPin("actions/checkout");
    await previewPin("actions/checkout", "v4");

    expect(mockedApiClient.get).toHaveBeenNthCalledWith(1, "/api/action-usage/preview", { params: { action: "actions/checkout" } });
    expect(mockedApiClient.get).toHaveBeenNthCalledWith(2, "/api/action-usage/preview", { params: { action: "actions/checkout", ref: "v4" } });
  });

  it("asks for an action's latest version", async () => {
    const latest = { action: "actions/checkout", ref: "v5.0.0", sha: "a".repeat(40), comment: "v5.0.0" };
    mockedApiClient.get.mockResolvedValue({ data: latest });

    await expect(fetchLatestVersion("actions/checkout")).resolves.toEqual(latest);
    expect(mockedApiClient.get).toHaveBeenCalledWith("/api/action-usage/latest", { params: { action: "actions/checkout" } });
  });

  it("updates one workflow, committing it or not as asked", async () => {
    mockedApiClient.post.mockResolvedValue({ data: { workflow_id: 7 } });
    const request = { workflow_id: 7, action: "actions/checkout", sha: "a".repeat(40), comment: "v5.0.0" };

    await updateWorkflow({ ...request, commit: false });
    await updateWorkflow({ ...request, commit: true });

    expect(mockedApiClient.post).toHaveBeenNthCalledWith(1, "/api/action-usage/update-workflow", { ...request, commit: false });
    expect(mockedApiClient.post).toHaveBeenNthCalledWith(2, "/api/action-usage/update-workflow", { ...request, commit: true });
  });

  it("updates every workflow of a project", async () => {
    mockedApiClient.post.mockResolvedValue({ data: { updated: 2, workflows: ["ci", "lint"], pr_state: "draft" } });
    const request = { project_id: 3, action: "actions/checkout", sha: "a".repeat(40), comment: null };

    await expect(applyPin(request)).resolves.toEqual({ updated: 2, workflows: ["ci", "lint"], pr_state: "draft" });
    expect(mockedApiClient.post).toHaveBeenCalledWith("/api/action-usage/apply", request);
  });
});
