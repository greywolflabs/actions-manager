import apiClient from "./apiClient";
import { fetchManagedFiles, fetchTakenRepos } from "./managedFiles";

import type { Mocked } from "vitest";
vi.mock("./apiClient", () => ({
  __esModule: true,
  default: { get: vi.fn(), post: vi.fn() },
}));

const mockedApiClient = apiClient as Mocked<typeof apiClient>;

describe("managed files API", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("reads the section", async () => {
    const section = { kinds: [], owned_count: 0, limit: 3, limit_message: null };
    mockedApiClient.get.mockResolvedValue({ data: section });

    expect(await fetchManagedFiles()).toEqual(section);
    expect(mockedApiClient.get).toHaveBeenCalledWith("/api/managed-files");
  });

  it("asks about at most 1000 names at a time and merges the answers", async () => {
    const names = Array.from({ length: 1500 }, (_, n) => `octo/r${n}`);
    mockedApiClient.post
      .mockResolvedValueOnce({ data: { taken: { "octo/r1": { project_id: 1, project_name: "A" } } } })
      .mockResolvedValueOnce({ data: { taken: { "octo/r1200": { project_id: null, project_name: null } } } });

    const taken = await fetchTakenRepos("codeowners", names, 4);

    expect(taken).toEqual({
      "octo/r1": { project_id: 1, project_name: "A" },
      "octo/r1200": { project_id: null, project_name: null },
    });
    expect(mockedApiClient.post).toHaveBeenNthCalledWith(1, "/api/managed-files/codeowners/taken-repos", {
      repo_names: names.slice(0, 1000),
      exclude_project_id: 4,
    });
    expect(mockedApiClient.post).toHaveBeenNthCalledWith(2, "/api/managed-files/codeowners/taken-repos", {
      repo_names: names.slice(1000),
      exclude_project_id: 4,
    });
  });

  it("asks nothing for no names, and sends no project to exclude unless given one", async () => {
    mockedApiClient.post.mockResolvedValue({ data: { taken: {} } });

    expect(await fetchTakenRepos("codeowners", [])).toEqual({});
    expect(mockedApiClient.post).not.toHaveBeenCalled();

    await fetchTakenRepos("codeowners", ["octo/api"]);
    expect(mockedApiClient.post).toHaveBeenCalledWith("/api/managed-files/codeowners/taken-repos", {
      repo_names: ["octo/api"],
    });
  });
});
