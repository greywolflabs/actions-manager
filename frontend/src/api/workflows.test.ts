import apiClient from "./apiClient";
import { updateWorkflows } from "./workflows";

import type { Mocked } from "vitest";
vi.mock("./apiClient", () => ({
  __esModule: true,
  default: {
    post: vi.fn(),
  },
}));

const mockedApiClient = apiClient as Mocked<typeof apiClient>;

const rejectWith = (status: number, data: unknown) => {
  mockedApiClient.post.mockRejectedValueOnce({ message: "Request failed", response: { status, data } });
};

const callUpdate = () =>
  updateWorkflows("Payments", ["org/repo"], {});

describe("updateWorkflows", () => {
  beforeEach(() => vi.clearAllMocks());

  it("reports a refused update as an error rather than an empty success", async () => {
    rejectWith(403, { detail: "Not authorized for this project" });

    const result = await callUpdate();

    expect(result.error).toBe("Not authorized for this project");
    expect(result.status).toBe(403);
  });

  it("keeps the backend's own error body unchanged", async () => {
    rejectWith(500, { error: "boom", status: 500 });

    const result = await callUpdate();

    expect(result).toEqual({ error: "boom", status: 500 });
  });
});
