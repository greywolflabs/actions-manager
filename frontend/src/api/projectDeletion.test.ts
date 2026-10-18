import axios from "axios";
import apiClient from "./apiClient";
import { getProjectDeletionSummary, deleteProjectEnhanced, deletionErrorMessage } from "./projectDeletion";

import type { Mocked } from "vitest";

vi.mock("./apiClient", () => ({
  __esModule: true,
  default: {
    get: vi.fn(),
    delete: vi.fn(),
  },
}));

const mockedAxios = axios as Mocked<typeof axios>;
const mockedApiClient = apiClient as Mocked<typeof apiClient>;

describe("projectDeletion API", () => {
  beforeEach(() => vi.clearAllMocks());

  describe("getProjectDeletionSummary", () => {
    it("requests the summary for the given project and user", async () => {
      mockedApiClient.get.mockResolvedValueOnce({
        data: { project_name: "My Project", project_code: "MP", workflows: [] },
      });

      const result = await getProjectDeletionSummary("testuser", "My Project");

      expect(mockedApiClient.get).toHaveBeenCalledWith(
        expect.stringContaining("/api/projects/My%20Project/deletion-summary"),
        { params: { github_user: "testuser" } }
      );
      expect(result.project_code).toBe("MP");
    });

    it("encodes project names containing URL-significant characters", async () => {
      mockedApiClient.get.mockResolvedValueOnce({ data: {} });

      await getProjectDeletionSummary("testuser", "a/b?c&d");

      expect(mockedApiClient.get).toHaveBeenCalledWith(
        expect.stringContaining("/api/projects/a%2Fb%3Fc%26d/deletion-summary"),
        expect.any(Object)
      );
    });

    it("throws on API error", async () => {
      mockedApiClient.get.mockRejectedValueOnce(new Error("not found"));

      await expect(getProjectDeletionSummary("testuser", "My Project")).rejects.toThrow(
        "not found"
      );
    });

    // Regression: the endpoint calls assert_session_owns_user, so the request has
    // to carry the session cookie. Bare axios omits it cross-origin (the cloud
    // deployment serves the API from a different origin than the app), which made
    // the delete dialog fall back to "database only" for every project.
    it("goes through the credentialed client, never bare axios", async () => {
      mockedApiClient.get.mockResolvedValueOnce({ data: {} });

      await getProjectDeletionSummary("testuser", "My Project");

      expect(mockedApiClient.get).toHaveBeenCalledTimes(1);
      expect(mockedAxios.get).not.toHaveBeenCalled();
    });
  });

  describe("deleteProjectEnhanced", () => {
    it("keeps the files and the secrets by default", async () => {
      mockedApiClient.delete.mockResolvedValueOnce({ data: { project_deleted: true } });

      await deleteProjectEnhanced("My Project");

      expect(mockedApiClient.delete).toHaveBeenCalledWith(
        expect.stringContaining("/api/projects/My%20Project/enhanced"),
        {
          data: {
            project_name: "My Project",
            remove_files: false,
            delete_github_resources: false,
            delete_deployment_environments: true,
          },
        }
      );
    });

    it("sends the file and secrets choices separately", async () => {
      mockedApiClient.delete.mockResolvedValueOnce({ data: { project_deleted: false } });

      await deleteProjectEnhanced("My Project", {
        removeFiles: true,
        deleteSecrets: true,
        deleteDeploymentEnvironments: false,
      });

      expect(mockedApiClient.delete).toHaveBeenCalledWith(expect.any(String), {
        data: {
          project_name: "My Project",
          remove_files: true,
          delete_github_resources: true,
          delete_deployment_environments: false,
        },
      });
    });

    it("never names the caller: the server takes it from the session", async () => {
      mockedApiClient.delete.mockResolvedValueOnce({ data: { project_deleted: true } });

      await deleteProjectEnhanced("My Project", { removeFiles: true });

      expect(mockedApiClient.delete.mock.calls[0][1]?.data).not.toHaveProperty("github_user");
    });

    it("returns a deletion waiting on its pull requests", async () => {
      mockedApiClient.delete.mockResolvedValueOnce({
        data: {
          message: "Project is being deleted",
          project_deleted: false,
          pr_state: "deleting",
          campaign_id: 7,
          prs_created: 2,
          results: {},
          details: { project_deleted: false, github_resources_deleted: [], errors: [] },
        },
      });

      const result = await deleteProjectEnhanced("My Project", { removeFiles: true });

      expect(result.project_deleted).toBe(false);
      expect(result.pr_state).toBe("deleting");
      expect(result.prs_created).toBe(2);
    });

    it("throws on API error", async () => {
      mockedApiClient.delete.mockRejectedValueOnce(new Error("forbidden"));

      await expect(deleteProjectEnhanced("My Project")).rejects.toThrow("forbidden");
    });
  });

  describe("deletionErrorMessage", () => {
    it("reads a refusal's message, which names the open pull requests", () => {
      const refusal = {
        response: { data: { detail: { message: "Merge or close its open pull requests first: org/a#3.", open_prs: [] } } },
      };

      expect(deletionErrorMessage(refusal)).toBe("Merge or close its open pull requests first: org/a#3.");
    });

    it("reads a plain detail", () => {
      expect(deletionErrorMessage({ response: { data: { detail: "already being deleted" } } })).toBe(
        "already being deleted"
      );
    });

    it("falls back to a generic message", () => {
      expect(deletionErrorMessage(new Error("Network Error"))).toBe("Error deleting project. Please try again.");
    });
  });
});
