import apiClient from "./apiClient";
import { getCodeowners, getProjectCodeowners, saveProjectCodeowners } from "./codeowners";

import type { Mocked } from 'vitest';
vi.mock("./apiClient", () => ({
  __esModule: true,
  default: {
    get: vi.fn(),
    put: vi.fn(),
  },
}));

const mockedApiClient = apiClient as Mocked<typeof apiClient>;

describe("codeowners API", () => {
  beforeEach(() => vi.clearAllMocks());

  describe("getProjectCodeowners", () => {
    it("reads the project's file by project id", async () => {
      mockedApiClient.get.mockResolvedValueOnce({ data: { codeowners: { id: 1, content: "* @a" } } });

      const record = await getProjectCodeowners(7);

      expect(mockedApiClient.get).toHaveBeenCalledWith("/api/projects/7/codeowners");
      expect(record?.content).toBe("* @a");
    });

    it("returns null for a project without one", async () => {
      mockedApiClient.get.mockResolvedValueOnce({ data: { codeowners: null } });

      expect(await getProjectCodeowners(7)).toBeNull();
    });
  });

  describe("saveProjectCodeowners", () => {
    it("puts the content and path to the project's file", async () => {
      mockedApiClient.put.mockResolvedValueOnce({ data: { success: true, message: "CODEOWNERS saved" } });

      const result = await saveProjectCodeowners(7, "* @owner", "CODEOWNERS");

      expect(mockedApiClient.put).toHaveBeenCalledWith(
        "/api/projects/7/codeowners",
        { content: "* @owner", file_path: "CODEOWNERS" }
      );
      expect(result.success).toBe(true);
    });

    it("uses the default file path when not specified", async () => {
      mockedApiClient.put.mockResolvedValueOnce({ data: { success: true } });

      await saveProjectCodeowners(7, "* @me");

      expect(mockedApiClient.put).toHaveBeenCalledWith(
        expect.any(String),
        { content: "* @me", file_path: ".github/CODEOWNERS" }
      );
    });
  });

  describe("getCodeowners", () => {
    it("reads a repository's GitHub copy on the branch and path asked for", async () => {
      mockedApiClient.get.mockResolvedValueOnce({ data: { success: true, github: { exists: true } } });

      const result = await getCodeowners("org/repo1", "testuser", "My Project", { branch: "release/1.0", path: "CODEOWNERS" });

      expect(mockedApiClient.get).toHaveBeenCalledWith(
        "/api/repos/org/repo1/codeowners",
        { params: { github_user: "testuser", project_name: "My Project", branch: "release/1.0", path: "CODEOWNERS" } }
      );
      expect(result.success).toBe(true);
    });

    it("uses numeric repo id directly without encoding", async () => {
      mockedApiClient.get.mockResolvedValueOnce({ data: { success: true } });

      await getCodeowners(42, "testuser", "My Project");

      expect(mockedApiClient.get).toHaveBeenCalledWith("/api/repos/42/codeowners", expect.any(Object));
    });
  });
});
