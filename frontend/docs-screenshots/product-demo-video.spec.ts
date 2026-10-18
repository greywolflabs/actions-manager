import { test, type Locator, type Page } from "@playwright/test";
import * as fs from "fs";
import * as path from "path";
import { createMockState, installApiMocks, mockCreatePullRequests } from "../e2e/fixtures/mocks";
import { DOCS_USER, seedDocsRepos, seedDocsUserProfile } from "./docs-fixtures";

/**
 * Records the full product demo for the docs' Product Demo page (and the copy
 * uploaded to YouTube): sign in with a PAT, create a Caller Workflow Project,
 * detect the build type, open pull requests across three repositories, and
 * merge them. GitHub is mocked; no real repository or account is touched.
 *
 * The page gets two recording-only overlays, injected by this test and never
 * part of the app: a chapter caption, since the video has no narration, and a
 * cursor, since Playwright's recordings don't show the pointer.
 *
 * The recording lands in docs/assets/videos/product-demo/full-demo.webm
 * (gitignored). The test prints when the flow starts and when each chapter
 * begins; with START being the first of those and POSTER a second before
 * chapter 5 (the three open pull requests), from the repository root:
 *
 *   # The docs site plays a 720p cut:
 *   ffmpeg -y -ss START -i docs/assets/videos/product-demo/full-demo.webm -an -vf scale=1280:-2 \
 *     -c:v libvpx-vp9 -crf 40 -b:v 0 -row-mt 1 -deadline good -cpu-used 2 -pix_fmt yuv420p \
 *     docs/assets/demo/product-demo.webm
 *   ffmpeg -y -ss START -i docs/assets/videos/product-demo/full-demo.webm -an -vf scale=1280:-2 \
 *     -c:v libx264 -preset slow -crf 28 -pix_fmt yuv420p -movflags +faststart \
 *     docs/assets/demo/product-demo.mp4
 *   ffmpeg -y -ss POSTER -i docs/assets/videos/product-demo/full-demo.webm -frames:v 1 -vf scale=1280:-2 \
 *     docs/assets/demo/product-demo-poster.png
 *
 *   # YouTube gets 1080p; it isn't committed:
 *   ffmpeg -y -ss START -i docs/assets/videos/product-demo/full-demo.webm -an \
 *     -c:v libx264 -preset slow -crf 20 -pix_fmt yuv420p -movflags +faststart full-demo-1080p.mp4
 */
const WIDTH = 1920;
const HEIGHT = 1080;
test.use({ viewport: { width: WIDTH, height: HEIGHT }, video: { mode: "on", size: { width: WIDTH, height: HEIGHT } } });

const REPOS = ["payments-service", "payments-worker", "checkout-web"];
// A project's repositories share its workflows, so all three build the same way.
const BUILD_TYPES: Record<string, { name: string; technology: string; confidence: number }> = {
  "payments-service": { name: "npm", technology: "Node.js", confidence: 0.96 },
  "payments-worker": { name: "npm", technology: "Node.js", confidence: 0.93 },
  "checkout-web": { name: "npm", technology: "Node.js", confidence: 0.91 },
};
const NODE_WORKFLOW = [
  "name: Build and Test",
  "on: [push, pull_request]",
  "jobs:",
  "  build:",
  "    runs-on: ubuntu-latest",
  "    steps:",
  "      - uses: actions/checkout@v4",
  "      - uses: actions/setup-node@v4",
  "        with:",
  "          node-version: 22",
  "      - run: npm ci",
  "      - run: npm test",
  "",
].join("\n");

/** Recording-only overlays: the chapter caption and a visible cursor. */
function installOverlays(page: Page) {
  return page.addInitScript(() => {
    const install = () => {
      if (document.getElementById("demo-cursor")) return;
      const style = document.createElement("style");
      style.textContent = `
        #demo-cursor { position: fixed; left: -14px; top: -14px; width: 28px; height: 28px; border-radius: 50%;
          border: 3px solid #fff; background: rgba(59,130,246,.35); box-shadow: 0 0 0 2px rgba(15,23,42,.6);
          pointer-events: none; z-index: 2147483647; transition: transform .05s linear, background .15s; }
        #demo-cursor.down { background: rgba(59,130,246,.85); }
        #demo-chapter { position: fixed; left: 50%; bottom: 40px; padding: 14px 22px; border-radius: 12px;
          background: rgba(15,23,42,.92); border: 1px solid rgba(148,163,184,.35); color: #f8fafc;
          font: 600 26px/1.3 system-ui, sans-serif; pointer-events: none; z-index: 2147483646;
          opacity: 0; transform: translate(-50%, 8px); transition: opacity .4s, transform .4s; }
        #demo-chapter.show { opacity: 1; transform: translate(-50%, 0); }
        #demo-chapter b { color: #60a5fa; margin-right: 12px; }`;
      document.head.appendChild(style);
      const cursor = document.createElement("div");
      cursor.id = "demo-cursor";
      const chapter = document.createElement("div");
      chapter.id = "demo-chapter";
      document.body.append(cursor, chapter);
      document.addEventListener("mousemove", (e) => {
        cursor.style.transform = `translate(${e.clientX}px, ${e.clientY}px)`;
      }, true);
      document.addEventListener("mousedown", () => cursor.classList.add("down"), true);
      document.addEventListener("mouseup", () => cursor.classList.remove("down"), true);
    };
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", install);
    else install();
  });
}

async function showChapter(page: Page, number: number, title: string) {
  await page.evaluate(([n, t]) => {
    const el = document.getElementById("demo-chapter");
    if (!el) return;
    el.classList.remove("show");
    const step = document.createElement("b");
    step.textContent = `${n}/5`;
    el.replaceChildren(step, document.createTextNode(t));
    requestAnimationFrame(() => el.classList.add("show"));
  }, [String(number), title]);
}

/** Glide the cursor to the element, then click it. */
async function click(page: Page, target: Locator, pause = 350) {
  await target.scrollIntoViewIfNeeded();
  const box = await target.boundingBox();
  if (box) await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, { steps: 25 });
  await page.waitForTimeout(pause);
  await target.click();
}

async function type(page: Page, target: Locator, text: string) {
  await click(page, target);
  await page.keyboard.type(text, { delay: 85 });
}

test.describe("product demo", () => {
  test("full tour, sign-in to merge", async ({ page }) => {
    test.setTimeout(180000);
    const startedAt = Date.now();
    const at = (label: string) => console.log(`${label} at ${((Date.now() - startedAt) / 1000).toFixed(1)}s`);
    const now = new Date().toISOString();

    const state = createMockState({
      projects: [],
      projectDefaults: { github_user: DOCS_USER, last_modified_by: DOCS_USER, updated_at: now },
    });
    await installApiMocks(page, state);
    await seedDocsRepos(page);
    await seedDocsUserProfile(page);
    await page.route((url) => url.pathname === "/healthz", (route) =>
      route.fulfill({ json: { status: "ok", pat_login_enabled: true, allow_insecure_http: true } }));
    await page.route((url) => url.pathname === "/auth/token", (route) => route.fulfill({ json: { user: DOCS_USER } }));
    await page.route((url) => url.pathname.startsWith("/api/repos/detect-build-type/"), (route) => {
      const repo = new URL(route.request().url()).pathname.split("/").pop() ?? "";
      return route.fulfill({ json: { detected_build_types: [BUILD_TYPES[repo]] } });
    });
    await page.route((url) => url.pathname.startsWith("/api/repos/suggest-workflow/"), (route) =>
      route.fulfill({ json: { workflow: NODE_WORKFLOW } }));
    await mockCreatePullRequests(page, state, {
      title: "Add build-and-test.yml",
      author: DOCS_USER,
      createdAt: now,
      workflowNames: "AM_PAY_build-and-test.yml",
      repoResults: REPOS.map((repo, i) => ({
        repo: `${DOCS_USER}/${repo}`,
        success: true,
        pr_number: 41 + i,
        branch_name: "actions-manager/payments-platform",
      })),
    });
    await installOverlays(page);

    await page.goto("/");
    const tokenInput = page.getByPlaceholder(/Paste fine-grained or classic/i);
    await tokenInput.waitFor({ timeout: 15000 });
    at("flow starts");

    // 1. Sign in with a personal access token.
    at("chapter 1");
    await showChapter(page, 1, "Sign in with a personal access token");
    await page.waitForTimeout(3200);
    await type(page, tokenInput, "github_pat_demo_only");
    await click(page, page.getByRole("button", { name: /Sign in with token/ }));
    await page.getByRole("button", { name: /New Project/ }).first().waitFor({ timeout: 15000 });
    await page.waitForTimeout(3800);

    // 2. Create a Caller Workflow Project with three repositories.
    at("chapter 2");
    await showChapter(page, 2, "Create a Caller Workflow Project");
    await click(page, page.getByRole("button", { name: /New Project/ }).first());
    await type(page, page.getByLabel(/Project Name:/i), "Payments Platform");
    await page.waitForTimeout(800);
    await click(page, page.getByTestId("wizard-continue"));
    await page.waitForTimeout(1100);
    for (const repo of REPOS) {
      await click(page, page.getByTestId(`available-checkbox-${DOCS_USER}/${repo}`), 200);
    }
    await page.waitForTimeout(1100);
    await click(page, page.getByTestId("wizard-continue"));
    await page.waitForTimeout(1100);
    await click(page, page.locator('input[name="resourceNamingMode"][value="prefix"]'));
    await click(page, page.getByRole("button", { name: "Advanced Options" }));
    await click(page, page.getByLabel("Use custom project key"));
    await type(page, page.getByPlaceholder("Enter project key (2-10 chars, letters/numbers)"), "PAY");
    await page.waitForTimeout(800);
    await click(page, page.getByTestId("create-project-button"));
    await page.getByRole("button", { name: "Open project Payments Platform" }).waitFor({ timeout: 15000 });
    await page.waitForTimeout(2400);
    await click(page, page.getByRole("button", { name: "Open project Payments Platform" }));
    await page.getByRole("button", { name: "Add Workflow" }).waitFor({ timeout: 15000 });
    await page.waitForTimeout(1600);

    // 3. Detect the build type and start from a matching workflow.
    at("chapter 3");
    await showChapter(page, 3, "Detect the build type");
    await click(page, page.getByRole("button", { name: "Add Workflow" }));
    await click(page, page.getByRole("button").filter({ has: page.getByRole("heading", { name: "Workflow", exact: true }) }));
    await type(page, page.getByLabel("Workflow Name"), "build-and-test");
    await click(page, page.getByText("Detect Build Types", { exact: true }));
    await page.getByText("Node.js (npm)").first().waitFor({ timeout: 15000 });
    await page.waitForTimeout(3400);
    await click(page, page.getByText("Node.js (npm)").first());
    await page.getByRole("button", { name: /Commit Locally/ }).first().waitFor({ timeout: 15000 });
    await page.waitForTimeout(3400);
    await click(page, page.getByRole("button", { name: /Commit Locally/ }).first());
    await page.getByText("saved as draft").waitFor({ timeout: 10000 });
    await page.waitForTimeout(2400);

    // 4. Open pull requests across the three repositories.
    at("chapter 4");
    await showChapter(page, 4, "Open pull requests across repositories");
    await click(page, page.getByTestId("create-pull-requests-button"));
    await page.getByTestId("confirm-create-prs").waitFor({ timeout: 10000 });
    await page.waitForTimeout(3000);
    await click(page, page.getByTestId("confirm-create-prs"));
    await page.getByText(/Successfully created 3 pull request/).waitFor({ timeout: 15000 });
    await page.waitForTimeout(3000);
    await click(page, page.getByRole("button", { name: "Close" }).last());
    await click(page, page.getByRole("button", { name: "PR Campaigns" }));
    await page.locator(".pr-history-workflow-chip").first().waitFor({ timeout: 15000 });
    await page.waitForTimeout(1600);
    await page.locator(".pr-history-workflow-chip").first().evaluate((el) => el.scrollIntoView({ behavior: "smooth", block: "start" }));
    await page.waitForTimeout(4100);

    // 5. Review and merge them.
    at("chapter 5");
    await showChapter(page, 5, "Review and merge");
    await click(page, page.getByRole("button", { name: "Merge Open PRs" }));
    await page.waitForTimeout(1600);
    await click(page, page.getByRole("dialog").getByRole("button", { name: "Merge Open PRs" }));
    await page.getByText(/3 pull requests merged/).first().waitFor({ timeout: 15000 });
    await page.waitForTimeout(3000);
    await click(page, page.getByRole("button", { name: "Completed Campaigns" }));
    await page.mouse.move(WIDTH - 200, HEIGHT - 200, { steps: 25 });
    await page.waitForTimeout(5400);
    at("flow ends");

    await page.close(); // finalizes/flushes the video file
    const videoPath = await page.video()!.path();
    const outPath = path.join(__dirname, "../../docs/assets/videos/product-demo/full-demo.webm");
    await fs.promises.mkdir(path.dirname(outPath), { recursive: true });
    await fs.promises.rename(videoPath, outPath);
  });
});
