/**
 * PAT sign-in is self-hosted only (#2129). The sign-in screen learns which
 * mode it is in from /healthz's pat_login_enabled, and keeps the PAT form
 * hidden unless the backend explicitly says yes — so a cloud install never
 * shows it, not even while /healthz is still loading.
 */
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "./App";
import { isUninitialized } from "./api/setup";

class StubWebSocket {
  close = vi.fn();
  send = vi.fn();
  addEventListener = vi.fn();
  removeEventListener = vi.fn();
}
vi.stubGlobal("WebSocket", StubWebSocket);

vi.mock("./api/setup", () => ({
  isUninitialized: vi.fn().mockResolvedValue(false),
}));

// Stands in for the signed-in landing page so a test can log out from it.
vi.mock("./ProjectMgmt", () => ({
  default: ({ onLogout }: { onLogout: () => void }) => <button onClick={onLogout} type="button">Log out</button>,
}));

vi.mock("./api/user", () => ({
  loginWithGitHubToken: vi.fn(),
  getUserDetails: vi.fn().mockResolvedValue(undefined),
  checkGitHubPermissions: vi.fn().mockResolvedValue(null),
  logout: vi.fn().mockResolvedValue(undefined),
  updateOnboardingState: vi.fn().mockResolvedValue(undefined),
}));

function mockHealthz(result: Promise<unknown>) {
  const fetchMock = vi.fn((url: string) => (
    url.endsWith("/healthz")
      ? result.then((body) => ({ json: () => Promise.resolve(body) }))
      : Promise.reject(new Error(`unexpected fetch: ${url}`))
  ));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

// Lets the /healthz promise chain settle so a "hidden" assertion is checked
// against the final state, not just the initial render.
async function settleHealthz() {
  await act(() => new Promise((resolve) => { setTimeout(resolve, 0); }));
}

function expectPatFormHidden() {
  expect(screen.queryByLabelText("Personal Access Token login")).toBeNull();
  expect(screen.queryByRole("button", { name: "Sign in with token" })).toBeNull();
  expect(screen.queryByRole("link", { name: /PAT setup guide/ })).toBeNull();
  expect(screen.queryByText("Sign in with a Personal Access Token")).toBeNull();
}

describe("PAT sign-in by installation mode", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.mocked(isUninitialized).mockResolvedValue(false);
  });

  it("shows the PAT form when the backend enables PAT sign-in", async () => {
    mockHealthz(Promise.resolve({ allow_insecure_http: false, pat_login_enabled: true }));

    render(<App />);

    expect(await screen.findByLabelText("Personal Access Token login")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign in with token" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Log in with GitHub/ })).toBeInTheDocument();
  });

  it("never shows the PAT form when the backend disables PAT sign-in", async () => {
    const fetchMock = mockHealthz(Promise.resolve({ allow_insecure_http: false, pat_login_enabled: false }));

    render(<App />);
    expectPatFormHidden();
    await settleHealthz();

    expect(fetchMock).toHaveBeenCalledWith(expect.stringMatching(/\/healthz$/));
    expect(screen.getByRole("button", { name: /Log in with GitHub/ })).toBeInTheDocument();
    expectPatFormHidden();
  });

  it("hides the PAT form when /healthz fails", async () => {
    mockHealthz(Promise.reject(new Error("network down")));

    render(<App />);
    await settleHealthz();

    expect(screen.getByRole("button", { name: /Log in with GitHub/ })).toBeInTheDocument();
    expectPatFormHidden();
  });

  it("hides the PAT form when /healthz does not report pat_login_enabled", async () => {
    mockHealthz(Promise.resolve({ allow_insecure_http: false }));

    render(<App />);
    await settleHealthz();

    expectPatFormHidden();
  });

  it("still offers restore from a backup when PAT sign-in is disabled", async () => {
    vi.mocked(isUninitialized).mockResolvedValue(true);
    mockHealthz(Promise.resolve({ allow_insecure_http: false, pat_login_enabled: false }));

    render(<App />);

    expect(await screen.findByRole("button", { name: /Restore from a backup/ })).toBeInTheDocument();
    await settleHealthz();
    expectPatFormHidden();
  });

  it("keeps asking /healthz until it answers, then shows the PAT form", async () => {
    // The self-hosted image starts nginx before uvicorn, so the first request can 502.
    const fetchMock = vi.fn()
      .mockRejectedValueOnce(new Error("502 Bad Gateway"))
      .mockResolvedValue({ json: () => Promise.resolve({ allow_insecure_http: false, pat_login_enabled: true }) });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);
    await settleHealthz();
    expectPatFormHidden();

    expect(await screen.findByLabelText("Personal Access Token login", {}, { timeout: 3000 })).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("skips /healthz while signed in and shows the PAT form after logout without a reload", async () => {
    localStorage.setItem("github_user", "alice");
    const fetchMock = mockHealthz(Promise.resolve({ allow_insecure_http: false, pat_login_enabled: true }));

    render(<App />);
    const logOut = await screen.findByRole("button", { name: "Log out" });
    expect(fetchMock).not.toHaveBeenCalledWith(expect.stringMatching(/\/healthz$/));

    await userEvent.setup().click(logOut);

    expect(await screen.findByLabelText("Personal Access Token login")).toBeInTheDocument();
  });

  // jsdom runs on http://localhost, where the warning never applies.
  describe("on a non-local HTTP address", () => {
    const dom = (globalThis as unknown as { jsdom: { reconfigure(options: { url: string }): void } }).jsdom;
    let originalUrl: string;

    beforeAll(() => {
      originalUrl = globalThis.location.href;
      dom.reconfigure({ url: "http://actionsmanager.example.com:8080/" });
    });

    afterAll(() => {
      dom.reconfigure({ url: originalUrl });
    });

    it("warns about the insecure connection unless ALLOW_INSECURE_HTTP is set", async () => {
      mockHealthz(Promise.resolve({ allow_insecure_http: false, pat_login_enabled: true }));

      render(<App />);

      expect(await screen.findByText("Insecure Connection Warning")).toBeInTheDocument();
    });

    it("does not warn once ALLOW_INSECURE_HTTP is set", async () => {
      mockHealthz(Promise.resolve({ allow_insecure_http: true, pat_login_enabled: true }));

      render(<App />);

      expect(await screen.findByLabelText("Personal Access Token login")).toBeInTheDocument();
      expect(screen.queryByText("Insecure Connection Warning")).toBeNull();
    });

    it("shows neither the warning nor the PAT form when /healthz fails", async () => {
      mockHealthz(Promise.reject(new Error("network down")));

      render(<App />);
      await settleHealthz();

      expect(screen.queryByText("Insecure Connection Warning")).toBeNull();
      expectPatFormHidden();
    });
  });
});
