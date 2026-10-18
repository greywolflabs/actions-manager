import { test } from "@playwright/test";
import * as fs from "fs";
import * as path from "path";
import {
  createMockState,
  installApiMocks,
  makeProject,
  makeWorkflow,
  mockCreatePullRequests,
  mockDriftResponse,
  seedAuthenticatedSession,
} from "../e2e/fixtures/mocks";
import { DOCS_USER, seedDocsUserProfile } from "./docs-fixtures";

/**
 * Records the README's demo: edit one workflow, commit it locally, and open a
 * PR campaign to three repositories. GitHub is mocked; no real repository or
 * account is touched. Run with `npm run docs:screenshots` (or this file alone).
 *
 * The recording lands in docs/assets/videos/readme/pr-campaign.webm, which is
 * gitignored like every docs video. The README shows a GIF made from it, so
 * after recording, from the repository root (START is the "flow starts at"
 * second this test prints, which trims the page load):
 *
 *   ffmpeg -y -ss START -i docs/assets/videos/readme/pr-campaign.webm \
 *     -vf "fps=10,scale=960:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle" \
 *     -loop 0 docs/assets/readme/pr-campaign.gif
 *
 * Keep the GIF under about 5 MB; GitHub renders it inline in the README.
 *
 * The docs home page plays the same recording as video instead (a quarter of
 * the GIF's size at full resolution), with its first frame as the poster:
 *
 *   ffmpeg -y -ss START -i docs/assets/videos/readme/pr-campaign.webm -an \
 *     -c:v libvpx-vp9 -crf 38 -b:v 0 -row-mt 1 -deadline good -cpu-used 2 -pix_fmt yuv420p \
 *     docs/assets/readme/pr-campaign.webm
 *   ffmpeg -y -ss START -i docs/assets/videos/readme/pr-campaign.webm -an \
 *     -c:v libx264 -preset slow -crf 27 -pix_fmt yuv420p -movflags +faststart \
 *     docs/assets/readme/pr-campaign.mp4
 *   ffmpeg -y -ss START -i docs/assets/videos/readme/pr-campaign.webm -frames:v 1 \
 *     docs/assets/readme/pr-campaign-poster.png
 */
const WIDTH = 1440;
const HEIGHT = 900;
test.use({ viewport: { width: WIDTH, height: HEIGHT }, video: { mode: "on", size: { width: WIDTH, height: HEIGHT } } });

const PROJECT = "Payments Platform";
const REPOS = ["acme-corp/payments-service", "acme-corp/payments-worker", "acme-corp/payments-api"];
const BASE_COMMITS = [
  "9c41f7ab26d3e5108b7c4f0da2e6913d5f8a0b47",
  "4e83d0c9157fa62b8d0e391c47a5b8206fd1e93c",
  "b72e5d1a08c3f4e69d2a7b0c5e8f1d3a6b9c4e27",
];
const WORKFLOW = [
  "name: Build and Test",
  "on: [push, pull_request]",
  "jobs:",
  "  build:",
  "    runs-on: ubuntu-latest",
  "    steps:",
  "      - uses: actions/checkout@v4",
  "      - uses: actions/setup-node@v4",
  "        with:",
  "          node-version: 20",
  "      - run: npm ci",
  "      - run: npm test",
  "",
].join("\n");

test.describe("readme demo", () => {
  test("PR campaign to three repositories", async ({ page }) => {
    const startedAt = Date.now();
    const now = new Date().toISOString();
    await seedAuthenticatedSession(page, DOCS_USER);
    const project = makeProject({
      project_id: 1,
      project_name: PROJECT,
      project_code: "PAY",
      github_user: DOCS_USER,
      last_modified_by: DOCS_USER,
      updated_at: now,
      pr_state: "synced",
      selected_repos: REPOS,
      workflows: [
        makeWorkflow({ name: "build-and-test.yml", lastModifiedBy: DOCS_USER, workflowStatus: "synced_with_github", content: WORKFLOW }),
      ],
    });
    const target = (repo: string) => `${repo} on main`;
    const state = createMockState({
      projects: [project],
      prStatus: { project_state: "synced", pull_requests: [], total_prs: 0, open_prs: 0, merged_prs: 0, closed_prs: 0 },
      // What the campaign card shows once the PRs exist: the workflow version
      // it applied, and per repository the base commit, the PR and the branch
      // protection in force.
      campaignExtras: {
        branch_option: "default",
        target_repos: REPOS,
        policy_version: { "build-and-test.yml": { version: 4, sha256: "5f0c2a91d7e84b36a1c9e0f27d3b4a6c8e1f9d02b7a5c3e6f4d8a0b1c2e3f4a5" } },
        base_commits: Object.fromEntries(REPOS.map((repo, i) => [target(repo), BASE_COMMITS[i]])),
        target_pr_urls: Object.fromEntries(REPOS.map((repo, i) => [target(repo), `https://github.com/${repo}/pull/${128 + i}`])),
        branch_protection: {
          [target(REPOS[0])]: { status: "protected", required_reviews: 1, required_status_checks: ["build"], enforce_admins: false },
          [target(REPOS[1])]: { status: "none" },
          [target(REPOS[2])]: { status: "none" },
        },
      },
    });
    await installApiMocks(page, state);
    await mockCreatePullRequests(page, state, {
      title: "Update build-and-test.yml",
      author: DOCS_USER,
      workflowNames: "build-and-test.yml",
      createdAt: now,
      repoResults: REPOS.map((repo, i) => ({
        repo,
        success: true,
        pr_number: 128 + i,
        branch_name: "actions-manager/payments-platform",
      })),
    });
    await mockDriftResponse(page, { driftedWorkflows: [], lastChecked: now });
    await seedDocsUserProfile(page);

    await page.goto(`/project/${DOCS_USER}/${encodeURIComponent(PROJECT)}`);
    await page.getByText("build-and-test.yml").first().click();
    await page.getByText("node-version: 20").waitFor({ timeout: 15000 });
    console.log(`flow starts at ${((Date.now() - startedAt) / 1000).toFixed(1)}s`);
    await page.waitForTimeout(1200);

    // Edit: move the build to Node 22.
    await page.getByText("node-version: 20").click();
    await page.keyboard.press("End");
    await page.keyboard.press("Backspace");
    await page.keyboard.press("Backspace");
    await page.keyboard.type("22", { delay: 200 });
    await page.keyboard.press("Escape");
    await page.waitForTimeout(900);

    await page.getByRole("button", { name: /Commit Locally/ }).first().click();
    await page.getByText("saved as draft").waitFor({ timeout: 10000 });
    await page.waitForTimeout(1200);

    // Deliver it to every repository in one PR campaign.
    await page.getByTestId("create-pull-requests-button").click();
    await page.getByTestId("confirm-create-prs").waitFor({ timeout: 10000 });
    await page.waitForTimeout(1800);
    await page.getByTestId("confirm-create-prs").click();
    await page.getByText(/Successfully created 3 pull request/).waitFor({ timeout: 15000 });
    await page.waitForTimeout(1800);
    await page.getByRole("button", { name: "Close" }).last().click();

    await page.getByRole("button", { name: "PR Campaigns" }).click();
    await page.getByText("Update build-and-test.yml").first().waitFor({ timeout: 15000 });
    await page.waitForTimeout(1500);

    // End on what the campaign gives you to act on: the workflow it applied,
    // Merge/Close Open PRs, and each repository's PR with its files.
    await page.locator(".pr-history-workflow-chip").first().evaluate((el) => el.scrollIntoView({ behavior: "smooth", block: "start" }));
    await page.getByRole("button", { name: "Merge Open PRs" }).waitFor({ timeout: 10000 });
    await page.waitForTimeout(3500);

    await page.close(); // finalizes/flushes the video file
    const videoPath = await page.video()!.path();
    const outPath = path.join(__dirname, "../../docs/assets/videos/readme/pr-campaign.webm");
    await fs.promises.mkdir(path.dirname(outPath), { recursive: true });
    await fs.promises.rename(videoPath, outPath);
  });
});
