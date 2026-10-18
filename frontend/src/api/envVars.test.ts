import apiClient from "./apiClient";
import { updateEnvVars, getEnvVars, handleDeleteEnvVars, syncEnvVar, getEnvVarsCount } from "./envVars";

import type { Mock, Mocked } from 'vitest';
vi.mock("./apiClient", () => ({
  __esModule: true,
  default: {
    post: vi.fn(),
    delete: vi.fn(),
  },
}));
vi.mock("../utils/toast", () => ({ toast: { error: vi.fn() } }));

const mockedApiClient = apiClient as Mocked<typeof apiClient>;

describe("envVars API", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    globalThis.fetch = vi.fn();
  });

  describe("updateEnvVars", () => {
    it("posts env vars to the correct endpoint", async () => {
      mockedApiClient.post.mockResolvedValueOnce({ data: { updated: true } });

      const result = await updateEnvVars(
        "testuser",
        ["org/repo1"],
        [{ key: "PORT", value: "3000" }],
        "My Project"
      );

      expect(mockedApiClient.post).toHaveBeenCalledWith(
        expect.stringContaining("/api/update-env-vars"),
        expect.objectContaining({
          user: "testuser",
          repo_names: ["org/repo1"],
          env: [{ key: "PORT", value: "3000" }],
          project_name: "My Project",
        })
      );
      expect(result).toEqual({ updated: true });
    });

    it("maps Repository objects to full_name strings", async () => {
      mockedApiClient.post.mockResolvedValueOnce({ data: {} });

      await updateEnvVars("user", [{ full_name: "org/repo1", name: "repo1" }], [], "proj");

      expect(mockedApiClient.post).toHaveBeenCalledWith(
        expect.any(String),
        expect.objectContaining({ repo_names: ["org/repo1"] })
      );
    });

    it("returns error object instead of throwing on API failure", async () => {
      mockedApiClient.post.mockRejectedValueOnce(new Error("timeout"));

      const result = await updateEnvVars("user", ["org/repo"], [], "proj");
      expect(result).toEqual({ error: "timeout" });
    });

    it("returns the server's reason rather than the status code", async () => {
      mockedApiClient.post.mockRejectedValueOnce({
        message: "Request failed with status code 400",
        response: { status: 400, data: { detail: "Repository 'org/new' is not part of project 'P' yet: save the project first" } },
      });

      const result = await updateEnvVars("user", ["org/new"], [{ key: "K", value: "v" }], "P");
      expect(result).toEqual({ error: "Repository 'org/new' is not part of project 'P' yet: save the project first" });
    });
  });

  describe("getEnvVars", () => {
    it("fetches env vars via fetch and maps them with repo name", async () => {
      (globalThis.fetch as Mock).mockResolvedValueOnce({
        ok: true,
        json: async () => ({ env_vars: [{ env_key: "PORT", value: "3000" }] }),
      });

      const result = await getEnvVars("testuser", "org/repo1", "My Project");

      expect(result).toEqual([{ env_key: "PORT", value: "3000", repo: "org/repo1" }]);
    });

    it("returns empty array on fetch error", async () => {
      (globalThis.fetch as Mock).mockRejectedValueOnce(new Error("network error"));

      const result = await getEnvVars("user", "repo", "proj");
      expect(result).toEqual([]);
    });

    it("returns empty array when env_vars is absent from response", async () => {
      (globalThis.fetch as Mock).mockResolvedValueOnce({
        ok: true,
        json: async () => ({}),
      });

      const result = await getEnvVars("user", "repo", "proj");
      expect(result).toEqual([]);
    });
  });

  describe("handleDeleteEnvVars", () => {
    it("rejects and skips the API call when user is missing", async () => {
      await expect(
        handleDeleteEnvVars("", "proj", ["repo"], [{ env_key: "KEY" }]),
      ).rejects.toThrow(/Missing required fields/);

      expect(mockedApiClient.delete).not.toHaveBeenCalled();
    });

    it("rejects with the server's reason, so the panel does not report a delete that failed", async () => {
      mockedApiClient.delete.mockRejectedValueOnce({
        response: { status: 400, data: { detail: "Repository 'org/new' is not part of project 'P' yet: save the project first" } },
      });

      await expect(
        handleDeleteEnvVars("testuser", "P", ["org/repo1", "org/new"], [{ env_key: "PORT" }]),
      ).rejects.toThrow("Repository 'org/new' is not part of project 'P' yet: save the project first");
    });

    it("deletes env vars via apiClient", async () => {
      mockedApiClient.delete.mockResolvedValueOnce({ data: { results: "ok" } });
      (globalThis.fetch as Mock).mockResolvedValueOnce({
        ok: true,
        json: async () => ({ env_vars: [] }),
      });

      await handleDeleteEnvVars("testuser", "My Project", ["org/repo1"], [{ env_key: "PORT" }]);

      expect(mockedApiClient.delete).toHaveBeenCalledWith(
        expect.stringContaining("/api/delete-env-vars"),
        expect.objectContaining({ data: expect.objectContaining({ user: "testuser" }) })
      );
    });
  });

  describe("syncEnvVar", () => {
    it("posts to sync-env-var endpoint", async () => {
      mockedApiClient.post.mockResolvedValueOnce({ data: { synced: true } });

      const result = await syncEnvVar("user", "proj", ["org/repo1"], "PORT");

      expect(mockedApiClient.post).toHaveBeenCalledWith(
        expect.stringContaining("/api/sync-env-var"),
        expect.objectContaining({ env_key: "PORT" })
      );
      expect(result).toEqual({ synced: true });
    });

    it("throws on error", async () => {
      mockedApiClient.post.mockRejectedValueOnce(new Error("failed"));

      await expect(syncEnvVar("user", "proj", ["repo"], "KEY")).rejects.toThrow("failed");
    });

    it("throws the server's reason rather than the status code", async () => {
      mockedApiClient.post.mockRejectedValueOnce({
        message: "Request failed with status code 400",
        response: { status: 400, data: { detail: "Invalid variable name 'K?x': use letters, digits and underscores" } },
      });

      await expect(syncEnvVar("user", "proj", ["org/repo"], "K?x")).rejects.toThrow(
        "Invalid variable name 'K?x': use letters, digits and underscores",
      );
    });
  });

  describe("getEnvVarsCount", () => {
    it("returns the count from response", async () => {
      (globalThis.fetch as Mock).mockResolvedValueOnce({
        ok: true,
        json: async () => ({ count: 5 }),
      });

      const result = await getEnvVarsCount("user", "proj", ["org/repo1"]);
      expect(result).toBe(5);
    });

    it("returns 0 on error", async () => {
      (globalThis.fetch as Mock).mockRejectedValueOnce(new Error("network error"));

      const result = await getEnvVarsCount("user", "proj", ["repo"]);
      expect(result).toBe(0);
    });
  });
});
