import apiClient from "./apiClient";
import { projectHintHeaders, setProjectHint } from "./projectHint";

// apiClient's request interceptor, as registered on the axios mock.
const sendThroughApiClient = (): Record<string, string> => {
  const [onRequest] = vi.mocked(apiClient.interceptors.request.use).mock.calls[0];
  const sent: Record<string, string> = {};
  onRequest!({ headers: { set: (name: string, value: string) => { sent[name] = value; } } } as never);
  return sent;
};

describe("projectHint", () => {
  afterEach(() => setProjectHint(null));

  it("sends no header until a page names its project", () => {
    expect(projectHintHeaders()).toEqual({});
  });

  it("sends the page's project id as X-Project-Id", () => {
    setProjectHint(12);

    expect(projectHintHeaders()).toEqual({ "X-Project-Id": "12" });
  });

  it("is sent by apiClient on every request", () => {
    setProjectHint(12);

    expect(sendThroughApiClient()).toEqual({ "X-Project-Id": "12" });
  });

  it("leaves apiClient's requests alone off a project page", () => {
    expect(sendThroughApiClient()).toEqual({});
  });

  it.each([0, -1, null])("treats %s as no project", (value) => {
    setProjectHint(5);
    setProjectHint(value);

    expect(projectHintHeaders()).toEqual({});
  });
});
