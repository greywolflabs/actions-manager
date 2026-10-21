import { test, type Page, type Route } from "@playwright/test";
import {
  corsHeaders,
  createMockState,
  installApiMocks,
  makeProject,
  makeWorkflow,
  mockBuildMetricsResponse,
  mockDriftResponse,
  mockReusableWorkflowLinks,
  seedAuthenticatedSession,
  NO_CREDENTIAL_REASON,
} from "../e2e/fixtures/mocks";
import { DOCS_REPOS, DOCS_USER, seedDocsRepos, seedDocsUserProfile, waitForAppHeader } from "./docs-fixtures";
import { BETA_PLAN } from "../src/test/plans";

/**
 * Regenerates the screenshots embedded in docs/**\/*.md
 * (docs/src/content/docs/assets/screenshots/<feature>/<name>.png). Run with
 * `npm run docs:screenshots`.
 *
 * Each test drives the real app against mocked API routes (no real backend,
 * no real GitHub session — see e2e/fixtures/mocks.ts) and overwrites the
 * existing PNG at the same path the corresponding doc page already
 * references. To add a new doc screenshot: add a test() below following the
 * same shape, using DOCS_USER and doc-friendly project/workflow names —
 * never the shared e2e fixtures' "octocat"/"demo-project" (those are fine
 * for functional tests, not for images published in the public docs site).
 *
 * The projects-dashboard scene lives in ./projects-video.spec.ts instead —
 * that doc asset is a recorded video, not a screenshot.
 */

/** Keeps every drift scene's "last checked" plausible for a published doc,
 *  instead of the shared fixtures' 2025-01-01 placeholder. */
const DOCS_LAST_CHECKED = "2026-07-24T09:12:00Z";

const DOCS_DRIFT_PROJECT = {
  project_name: "NodeBuilds",
  project_code: "NODE",
  github_user: DOCS_USER,
  last_modified_by: DOCS_USER,
  updated_at: "2026-07-24T00:00:00Z",
  pr_state: "draft",
  selected_repos: ["acme-corp/node-api", "acme-corp/node-worker"],
} as const;

const DOCS_BUILD_METRICS_PROJECT = {
  project_name: "NodeBuilds",
  project_code: "NODE",
  github_user: DOCS_USER,
  last_modified_by: DOCS_USER,
  updated_at: "2026-07-24T00:00:00Z",
  pr_state: "synced",
  selected_repos: ["acme-corp/node-api", "acme-corp/node-worker"],
} as const;

/** The same synced project, for scenes that only need one to exist. The
 *  "workflow removal scope" scene above predates this and still inlines it. */
const DOCS_SYNCED_PROJECT = DOCS_BUILD_METRICS_PROJECT;

/** A synced CODEOWNERS project in Managed Files. */
const DOCS_CODEOWNERS_PROJECT = {
  ...DOCS_SYNCED_PROJECT,
  project_name: "NodeBuilds owners",
  project_code: "NBOWN",
  project_type: "managed_files",
  file_kind: "codeowners",
} as const;

/** What GET /api/projects/?section=managed_files adds to that project's row. */
const DOCS_MANAGED_ROW = {
  owner: DOCS_USER,
  repo_count: DOCS_CODEOWNERS_PROJECT.selected_repos.length,
  file_path: ".github/CODEOWNERS",
  drifted_repos: [],
  drift_status: "clean",
  last_drift_check_at: DOCS_LAST_CHECKED,
};

const DOCS_CODEOWNERS = "* @acme-corp/node-team\n/infra/ @acme-corp/platform\n/docs/ @acme-corp/tech-writers\n";

const CODEOWNERS_KIND = {
  kind: "codeowners",
  label: "CODEOWNERS",
  paths: [".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"],
};

function fulfillJson(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    headers: corsHeaders(route),
    body: JSON.stringify(body),
  });
}

/** GET /api/managed-files on the self-hosted beta, and the repositories other CODEOWNERS projects hold. */
async function mockManagedFiles(
  page: Page,
  { ownedCount = 1, taken = {} }: { ownedCount?: number; taken?: Record<string, object> } = {},
) {
  await page.route(
    (url) => url.pathname === "/api/managed-files",
    (route) => fulfillJson(route, {
      kinds: [CODEOWNERS_KIND],
      owned_count: ownedCount,
      limit: 3,
      limit_message: "Beta limit reached (3/3 Managed Files projects).",
    }),
  );
  await page.route(
    (url) => url.pathname === "/api/managed-files/codeowners/taken-repos",
    (route) => {
      const names: string[] = route.request().postDataJSON()?.repo_names ?? [];
      return fulfillJson(route, { taken: Object.fromEntries(names.filter((n) => n in taken).map((n) => [n, taken[n]])) });
    },
  );
}

/** DOCS_CODEOWNERS_PROJECT, with the PR status that agrees it is synced. */
function codeownersProjectState() {
  const state = createMockState({ projects: [makeProject(DOCS_CODEOWNERS_PROJECT)] });
  state.prStatus.project_state = "synced";
  return state;
}

/** The project's synced CODEOWNERS file. */
async function mockCodeowners(page: Page, content: string) {
  await page.route(
    (url) => /^\/api\/projects\/\d+\/codeowners$/.test(url.pathname),
    (route) => fulfillJson(route, {
      codeowners: {
        id: 1, project_id: 1, content, file_path: ".github/CODEOWNERS",
        git_hash: "a".repeat(40), status: "synced_with_github", last_modified_by: DOCS_USER,
        created_at: DOCS_LAST_CHECKED, updated_at: DOCS_LAST_CHECKED,
      },
    }),
  );
}

/** `confirmed_present_at` as the server renders it: _utc_iso emits +00:00,
 *  never a Z. */
const DOCS_CONFIRMED_PRESENT_AT = DOCS_LAST_CHECKED.replace("Z", "+00:00");

/** A fortnight of plausible build history: mostly green, with a bad Tuesday
 *  on deploy-production so the trend and the failures view have something
 *  real to show. */
const DOCS_BUILD_METRICS = (() => {
  // Relative to capture time, not a fixed date: this panel renders *relative*
  // times ("synced 4 minutes ago"), so a pinned fixture would publish a
  // screenshot reading "3 weeks ago" the moment it was committed.
  const DAY_MS = 24 * 60 * 60 * 1000;
  const day = (offset: number) => new Date(Date.now() - offset * DAY_MS).toISOString();
  const trend = Array.from({ length: 14 }, (_, i) => {
    const offset = 13 - i;
    const total = [6, 8, 7, 9, 6, 4, 3, 8, 11, 9, 7, 8, 6, 9][i];
    const failure = offset === 8 ? 4 : [0, 1, 0, 0, 1, 0, 0, 0, 2, 0, 1, 0, 0, 1][i];
    return { date: day(offset).slice(0, 10), total, success: total - failure, failure };
  });

  return {
    summary: {
      window_days: 30,
      last_synced: new Date(Date.now() - 4 * 60 * 1000).toISOString(),
      total_runs: 101,
      decided_runs: 98,
      conclusion_counts: { success: 86, failure: 12, cancelled: 3 },
      success_rate: 87.8,
      avg_duration_seconds: 268,
      p50_duration_seconds: 241,
      p95_duration_seconds: 612,
      avg_queue_seconds: 14,
      trend,
    },
    workflows: [
      {
        workflow_name: "build-and-test",
        workflow_filename: "build-and-test.yml",
        total: 68,
        success_rate: 94.1,
        avg_duration_seconds: 214,
        actions_url:
          "https://github.com/acme-corp/node-api/actions/workflows/build-and-test.yml",
      },
      {
        workflow_name: "deploy-production",
        workflow_filename: "deploy-production.yml",
        total: 33,
        success_rate: 75.8,
        avg_duration_seconds: 379,
        actions_url:
          "https://github.com/acme-corp/node-api/actions/workflows/deploy-production.yml",
      },
    ],
    recentRuns: [
      { github_run_id: 4821, run_number: 214, workflow_name: "build-and-test", repo: "acme-corp/node-api", branch: "main", conclusion: "success", duration_seconds: 203, created_at: day(0) },
      { github_run_id: 4820, run_number: 96, workflow_name: "deploy-production", repo: "acme-corp/node-api", branch: "main", conclusion: "failure", duration_seconds: 412, created_at: day(0) },
      { github_run_id: 4816, run_number: 213, workflow_name: "build-and-test", repo: "acme-corp/node-worker", branch: "main", conclusion: "success", duration_seconds: 188, created_at: day(1) },
      { github_run_id: 4810, run_number: 95, workflow_name: "deploy-production", repo: "acme-corp/node-api", branch: "release/4.2", conclusion: "success", duration_seconds: 366, created_at: day(1) },
      { github_run_id: 4804, run_number: 212, workflow_name: "build-and-test", repo: "acme-corp/node-api", branch: "main", conclusion: "success", duration_seconds: 221, created_at: day(2) },
    ],
    scoped: {
      "deploy-production.yml": {
        total_runs: 33,
        decided_runs: 33,
        conclusion_counts: { success: 25, failure: 8 },
        success_rate: 75.8,
        avg_duration_seconds: 379,
        p50_duration_seconds: 361,
        p95_duration_seconds: 640,
        avg_queue_seconds: 22,
        // The real endpoint scopes the trend too. Without this the published
        // image would show the project-wide chart under a scoped heading.
        trend: Array.from({ length: 14 }, (_, i) => {
          const total = [2, 3, 2, 3, 2, 1, 1, 2, 4, 3, 2, 3, 2, 3][i];
          const failure = [0, 1, 0, 0, 1, 0, 0, 0, 3, 0, 1, 0, 0, 2][i];
          return { date: day(13 - i).slice(0, 10), total, success: total - failure, failure };
        }),
        recent_runs: [
          { github_run_id: 4820, run_number: 96, workflow_name: "deploy-production", repo: "acme-corp/node-api", branch: "main", event: "push", status: "completed", conclusion: "failure", duration_seconds: 412, created_at: day(0), html_url: "https://github.com/acme-corp/node-api/actions/runs/4820" },
          { github_run_id: 4810, run_number: 95, workflow_name: "deploy-production", repo: "acme-corp/node-api", branch: "release/4.2", event: "push", status: "completed", conclusion: "success", duration_seconds: 366, created_at: day(1), html_url: "https://github.com/acme-corp/node-api/actions/runs/4810" },
          { github_run_id: 4795, run_number: 94, workflow_name: "deploy-production", repo: "acme-corp/node-api", branch: "main", event: "workflow_dispatch", status: "completed", conclusion: "success", duration_seconds: 344, created_at: day(3), html_url: "https://github.com/acme-corp/node-api/actions/runs/4795" },
        ],
      },
    },
  };
})();

/** repo-branch-configs isn't in the shared mocks, and its failure renders a
 *  "Network Error" strip below the repository picker. Every repo inherits the
 *  project's default branch. */
async function mockDocsRepoBranchConfigs(
  page: Page,
  repos: readonly { id: number; full_name: string }[],
) {
  await page.route(
    (url) => /\/api\/projects\/[^/]+\/repo-branch-configs$/.test(url.pathname),
    (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          project_id: 1,
          project_branch_option: "default",
          project_branch_regex: "",
          project_branch_max_age_days: 30,
          repos: repos.map((repo) => ({
            repo_id: repo.id,
            repo_name: repo.full_name,
            branch_config_mode: "inherit",
            branch_option: null,
            branch_regex: null,
            branch_max_age_days: null,
            effective_branch_option: "default",
            effective_branch_regex: "",
            effective_branch_max_age_days: 30,
            using_project_default: true,
          })),
        }),
      })
  );
}

test.describe("docs screenshots", () => {
  test.beforeEach(async ({ page }) => {
    await seedAuthenticatedSession(page, DOCS_USER);
  });

  test("workspace backup page", async ({ page }) => {
    await installApiMocks(page, createMockState());
    await seedDocsUserProfile(page);
    await page.route("**/api/workspace/backup/info", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          backup_format_version: "1.0",
          table_count: 31,
          total_rows: 1284,
          tables: {
            accounts: 12,
            projects: 8,
            repos: 46,
            workflows: 122,
            workflow_versions: 940,
            project_workflows: 122,
            rulesets: 6,
            codeowners: 8,
            custom_files: 14,
            project_pull_requests: 6,
          },
          excluded_tables: ["auth_sessions"],
        }),
      })
    );

    await page.goto("/workspace/backup");
    await page.getByText(/1284 row\(s\)/).waitFor({ timeout: 15000 });
    await waitForAppHeader(page);
    await page.waitForTimeout(300);

    await page.screenshot({ path: "../docs/src/content/docs/assets/screenshots/backup-restore/workspace-backup.png" });
  });

  test("workspace drift settings page", async ({ page }) => {
    await installApiMocks(page, createMockState());
    await seedDocsUserProfile(page);
    await page.route("**/api/drift/settings", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          sweep_enabled: true,
          recheck_interval_minutes: 30,
          batch_size: 5,
          poll_interval_seconds: 60,
        }),
      })
    );

    await page.goto("/workspace/drift");
    await page.getByTestId("drift-settings-form").waitFor({ timeout: 15000 });
    await waitForAppHeader(page);
    await page.waitForTimeout(300);

    await page.screenshot({ path: "../docs/src/content/docs/assets/screenshots/drift-detection/drift-settings.png" });
  });

  test("workflow editor page view", async ({ page }) => {
    const project = makeProject({
      project_name: "NodeBuilds",
      project_code: "NODE",
      github_user: DOCS_USER,
      last_modified_by: DOCS_USER,
      updated_at: "2026-07-24T00:00:00Z",
      pr_state: "draft",
      selected_repos: ["acme-corp/node-api", "acme-corp/node-worker"],
      workflows: [
        makeWorkflow({ name: "build-and-test.yml", lastModifiedBy: DOCS_USER }),
        makeWorkflow({ name: "deploy-production.yml", lastModifiedBy: DOCS_USER }),
      ],
    });
    await installApiMocks(page, createMockState({ projects: [project] }));
    await seedDocsUserProfile(page);

    await page.goto(`/project/${DOCS_USER}/${encodeURIComponent("NodeBuilds")}`);
    await page
      .getByText("build-and-test.yml", { exact: false })
      .first()
      .waitFor({ timeout: 15000 });
    await page.getByText("build-and-test.yml", { exact: false }).first().click();
    await page.waitForTimeout(500);

    await waitForAppHeader(page);
    await page.screenshot({ path: "../docs/src/content/docs/assets/screenshots/workflows/workflow-page-view.png" });

    // Expanded editor, in GUI mode: that's where the extra room actually
    // shows - the job list and step panel stop being a column of a column.
    await page.getByRole("button", { name: "GUI", exact: true }).click();
    await page.waitForTimeout(300);
    await page.getByRole("button", { name: /Expand/ }).click();
    await page.getByRole("dialog").waitFor({ timeout: 5000 });
    await page.waitForTimeout(500);
    await page.screenshot({ path: "../docs/src/content/docs/assets/screenshots/workflows/workflow-editor-expanded.png" });
  });

  test("workflow removal scope choice", async ({ page }) => {
    const project = makeProject({
      project_name: "NodeBuilds",
      project_code: "NODE",
      github_user: DOCS_USER,
      last_modified_by: DOCS_USER,
      updated_at: "2026-07-24T00:00:00Z",
      pr_state: "synced",
      selected_repos: ["acme-corp/node-api", "acme-corp/node-worker"],
      // Delivered, so the dialog offers the GitHub option rather than
      // disabling it for a workflow GitHub has never seen.
      workflows: [
        makeWorkflow({
          name: "build-and-test.yml",
          lastModifiedBy: DOCS_USER,
          workflowStatus: "synced_with_github",
        }),
        makeWorkflow({ name: "deploy-production.yml", lastModifiedBy: DOCS_USER }),
      ],
    });
    await installApiMocks(page, createMockState({ projects: [project] }));
    await seedDocsUserProfile(page);

    await page.goto(`/project/${DOCS_USER}/${encodeURIComponent("NodeBuilds")}`);
    await page
      .getByText("build-and-test.yml", { exact: false })
      .first()
      .waitFor({ timeout: 15000 });
    await page.getByText("build-and-test.yml", { exact: false }).first().click();
    await page.waitForTimeout(500);

    await page.getByLabel("More options").click();
    await page.getByRole("menuitem", { name: /Delete workflow/ }).click();
    await page.getByRole("dialog").waitFor({ timeout: 5000 });
    await page.waitForTimeout(300);

    await waitForAppHeader(page);
    await page.screenshot({ path: "../docs/src/content/docs/assets/screenshots/workflows/workflow-removal-scope.png" });
  });

  test("workflow rename impact", async ({ page }) => {
    const project = makeProject({
      ...DOCS_SYNCED_PROJECT,
      // The dialog renders purely from the mocked rename-impact response below,
      // so nothing in this project state decides what it lists. The workflow
      // rows only have to exist for the editor to open on one.
      workflows: [
        makeWorkflow({
          name: "build-and-test.yml",
          lastModifiedBy: DOCS_USER,
          workflowStatus: "synced_with_github",
        }),
        makeWorkflow({ name: "deploy-production.yml", lastModifiedBy: DOCS_USER }),
      ],
    });
    await installApiMocks(
      page,
      createMockState({
        projects: [project],
        // Overrides the shared RENAME_IMPACT fixture's octocat/hello-world
        // single target: the doc image should show the delivered-to-several
        // -repositories case, plus an override, since that is what the section
        // describes.
        //
        // Field for field from `RenameImpactResponse`, per the shared fixture's
        // warning. In particular `warnings` is NOT empty: the server appends the
        // build-metrics note on every rename (_rename_impact_warnings), so a
        // published image without it would show a dialog no user ever sees. The
        // text is copied from the backend verbatim. `confirmed_present_at` uses
        // +00:00 for the same reason — _utc_iso never emits a Z.
        renameImpact: {
          classification: "delivered",
          blocked_reason: null,
          old_filename: "build-and-test.yml",
          new_filename: "build-and-verify.yml",
          targets: [
            {
              repo: "acme-corp/node-api",
              branch: "main",
              old_file_present: true,
              new_file_present: false,
              confirmed_present_at: DOCS_CONFIRMED_PRESENT_AT,
            },
            {
              repo: "acme-corp/node-worker",
              branch: "main",
              old_file_present: true,
              new_file_present: false,
              confirmed_present_at: DOCS_CONFIRMED_PRESENT_AT,
            },
          ],
          consumers: [],
          overrides: ["acme-corp/node-worker"],
          warnings: [
            "Build metrics recorded under the old filename stay attached to it, so this " +
              "workflow's run history will show a break at the rename.",
          ],
        },
      })
    );
    await seedDocsUserProfile(page);

    await page.goto(`/project/${DOCS_USER}/${encodeURIComponent("NodeBuilds")}`);
    await page
      .getByText("build-and-test.yml", { exact: false })
      .first()
      .waitFor({ timeout: 15000 });
    await page.getByText("build-and-test.yml", { exact: false }).first().click();
    await page.waitForTimeout(500);

    // By aria-label, not data-testid: the project sidebar renders the same
    // EditableNameField for the project name, so the test id matches twice.
    await page.getByRole("button", { name: "Edit workflow filename" }).click();
    const nameInput = page.getByRole("textbox", { name: "workflow filename" });
    await nameInput.fill("build-and-verify");
    await page.getByRole("button", { name: "Save workflow filename" }).click();

    await page.getByRole("dialog").waitFor({ timeout: 5000 });
    // The dialog opens on its loading state and swaps the body in when the
    // impact arrives — capture the answer, not the spinner.
    await page.getByTestId("rename-targets").waitFor({ timeout: 5000 });
    await page.waitForTimeout(300);

    await waitForAppHeader(page);
    await page.screenshot({ path: "../docs/src/content/docs/assets/screenshots/workflows/workflow-rename-impact.png" });
  });

  test("reusable workflows — linked workflow in a caller project", async ({ page }) => {
    // The caller's own workflow calls the shared one, so the file list shows
    // both sides: the project's workflow and the one linked in from Shared CI.
    const reusableBuild = {
      workflow_id: 10,
      workflow_name: "java-build.yml",
      workflow_yaml: [
        "name: Java Build",
        "on:",
        "  workflow_call:",
        "    inputs:",
        "      java-version:",
        "        type: string",
        '        default: "21"',
        "jobs:",
        "  build:",
        "    runs-on: ubuntu-latest",
        "    steps:",
        "      - uses: actions/checkout@v4",
        "      - uses: actions/setup-java@v4",
        "        with:",
        "          distribution: temurin",
        "          java-version: ${{ inputs.java-version }}",
        "      - run: mvn -B verify",
        "",
      ].join("\n"),
      rwx_project_id: 2,
      rwx_project_name: "Shared CI",
      rwx_repo: "acme-corp/shared-workflows",
      rwx_repo_visibility: "public" as const,
      workflowStatus: "synced_with_github",
    };
    const project = makeProject({
      ...DOCS_SYNCED_PROJECT,
      selected_repos: [...DOCS_SYNCED_PROJECT.selected_repos],
      workflows: [
        makeWorkflow({
          name: "build-and-test.yml",
          lastModifiedBy: DOCS_USER,
          workflowStatus: "synced_with_github",
          content: [
            "name: Build and Test",
            "on: [push, pull_request]",
            "jobs:",
            "  build:",
            "    uses: acme-corp/shared-workflows/.github/workflows/java-build.yml@main",
            "    with:",
            '      java-version: "21"',
            "",
          ].join("\n"),
        }),
      ],
      linked_reusable_workflows: [reusableBuild],
    });
    await installApiMocks(page, createMockState({ projects: [project] }));
    // After installApiMocks, whose own /api/rwx-workflows handler would
    // otherwise win (routes are LIFO).
    await mockReusableWorkflowLinks(page, { linkedWorkflows: [reusableBuild] });
    await seedDocsUserProfile(page, undefined, { plan: BETA_PLAN });
    await mockDriftResponse(page, { lastChecked: DOCS_LAST_CHECKED });
    // 16:10, the landing page's card ratio, so the card shows the whole frame.
    await page.setViewportSize({ width: 1710, height: 1069 });

    await page.goto(`/project/${DOCS_USER}/${encodeURIComponent("NodeBuilds")}`);
    const linkedRow = page.getByTestId("linked-rwx-workflow-card");
    await linkedRow.waitFor({ timeout: 15000 });
    await linkedRow.click();
    await page.getByText("workflow_call", { exact: false }).first().waitFor({ timeout: 5000 });
    await page.waitForTimeout(500);

    await waitForAppHeader(page);
    await page.screenshot({
      path: "../docs/src/content/docs/assets/screenshots/reusable-workflows/linked-reusable-workflow.png",
    });
  });

  test("PR campaign dashboard", async ({ page }) => {
    const project = makeProject({
      project_name: "NodeBuilds",
      project_code: "NODE",
      github_user: DOCS_USER,
      last_modified_by: DOCS_USER,
      updated_at: "2026-07-24T00:00:00Z",
      pr_state: "open",
      selected_repos: ["acme-corp/node-api", "acme-corp/node-worker"],
      workflows: [makeWorkflow({ name: "build-and-test.yml", lastModifiedBy: DOCS_USER })],
    });
    const state = createMockState({
      projects: [project],
      prStatus: {
        project_state: "open",
        pull_requests: [
          {
            repo_name: "acme-corp/node-api",
            pr_number: 128,
            pr_url: "https://github.com/acme-corp/node-api/pull/128",
            pr_state: "open",
            branch_name: "actions-manager/nodebuilds",
            target_branch: "main",
            title: "Update build-and-test.yml",
            author: DOCS_USER,
            workflow_names: "build-and-test.yml",
            created_at: "2026-07-18T00:00:00Z",
            updated_at: "2026-07-24T00:00:00Z",
          },
          {
            repo_name: "acme-corp/node-worker",
            pr_number: 74,
            pr_url: "https://github.com/acme-corp/node-worker/pull/74",
            pr_state: "merged",
            branch_name: "actions-manager/nodebuilds",
            target_branch: "main",
            title: "Update build-and-test.yml",
            author: DOCS_USER,
            workflow_names: "build-and-test.yml",
            created_at: "2026-07-01T00:00:00Z",
            updated_at: "2026-07-24T00:00:00Z",
            merged_at: "2026-07-24T00:00:00Z",
          },
        ],
        total_prs: 2,
        open_prs: 1,
        merged_prs: 1,
        closed_prs: 0,
      },
      // The creation-time snapshot the card reads: which branch mode the
      // project is on, and per target the base branch + commit, the PR that
      // opened against it, and what branch protection was in force. Without
      // these the card falls back to its bare form and the screenshot stops
      // matching what the docs describe.
      campaignExtras: {
        branch_option: "default",
        target_repos: ["acme-corp/node-api", "acme-corp/node-worker"],
        base_commits: {
          "acme-corp/node-api on main": "9c41f7ab26d3e5108b7c4f0da2e6913d5f8a0b47",
          "acme-corp/node-worker on main": "4e83d0c9157fa62b8d0e391c47a5b8206fd1e93c",
        },
        target_pr_urls: {
          "acme-corp/node-api on main": "https://github.com/acme-corp/node-api/pull/128",
          "acme-corp/node-worker on main": "https://github.com/acme-corp/node-worker/pull/74",
        },
        branch_protection: {
          "acme-corp/node-api on main": {
            status: "protected",
            required_reviews: 2,
            required_status_checks: ["ci/build", "ci/test"],
            enforce_admins: true,
          },
          "acme-corp/node-worker on main": { status: "none" },
        },
      },
    });
    await installApiMocks(page, state);
    await seedDocsUserProfile(page);

    // Taller than the shared viewport: the expanded card runs well past 900px,
    // and the parts the docs describe sit at both ends of it — header tiles and
    // the rollback control at the top, per-repository base commits and the
    // bulk-operation buttons at the bottom. `fullPage` cannot reach them, since
    // the app scrolls an inner container rather than the document.
    await page.setViewportSize({ width: 1710, height: 1500 });

    await page.goto(`/project/${DOCS_USER}/${encodeURIComponent("NodeBuilds")}`);
    await page.getByRole("button", { name: "PR Campaigns" }).click();
    await page
      .getByText("Update build-and-test.yml", { exact: false })
      .first()
      .waitFor({ timeout: 15000 });
    await page.waitForTimeout(300);

    // fullPage: the expanded card is taller than the viewport, and the parts
    // the docs describe sit at both ends of it — the header tiles and rollback
    // control at the top, the per-repository base commits and bulk-operation
    // buttons at the bottom. A viewport shot can only ever show one end.
    await waitForAppHeader(page);
    await page.screenshot({
      path: "../docs/src/content/docs/assets/screenshots/pr-campaigns/pr-campaign.png",
      fullPage: true,
    });
  });

  test("PR campaign rollback review", async ({ page }) => {
    const project = makeProject({
      project_name: "NodeBuilds",
      project_code: "NODE",
      github_user: DOCS_USER,
      last_modified_by: DOCS_USER,
      updated_at: "2026-07-24T00:00:00Z",
      pr_state: "open",
      selected_repos: ["acme-corp/node-api", "acme-corp/node-worker"],
      workflows: [makeWorkflow({ name: "build-and-test.yml", lastModifiedBy: DOCS_USER })],
    });
    // The campaign bumped the runner image everywhere. node-api can be
    // inverted cleanly; node-worker was edited by hand after the merge, so it
    // is flagged rather than clobbered — the two cases the docs describe.
    const beforeRollback = [
      "name: Build and Test",
      "on:",
      "  push:",
      "    branches: [main]",
      "jobs:",
      "  build:",
      "    runs-on: ubuntu-24.04",
      "    steps:",
      "      - uses: actions/checkout@v4",
      "      - run: npm ci",
      "      - run: npm test",
      "",
    ].join("\n");
    const afterRollback = beforeRollback.replace("ubuntu-24.04", "ubuntu-latest");

    const state = createMockState({
      projects: [project],
      prStatus: {
        project_state: "open",
        pull_requests: [
          {
            repo_name: "acme-corp/node-api",
            pr_number: 128,
            pr_url: "https://github.com/acme-corp/node-api/pull/128",
            pr_state: "merged",
            branch_name: "actions-manager/nodebuilds",
            target_branch: "main",
            title: "Update build-and-test.yml",
            author: DOCS_USER,
            workflow_names: "build-and-test.yml",
            created_at: "2026-07-01T00:00:00Z",
            updated_at: "2026-07-24T00:00:00Z",
            merged_at: "2026-07-24T00:00:00Z",
          },
        ],
        total_prs: 1,
        open_prs: 0,
        merged_prs: 1,
        closed_prs: 0,
      },
      rollbackPreview: {
        campaign_id: 1,
        campaign_name: "Update build-and-test.yml",
        invertible_count: 1,
        targets: [
          {
            repo_name: "acme-corp/node-api",
            target_branch: "main",
            pr_number: 128,
            pr_url: "https://github.com/acme-corp/node-api/pull/128",
            workflow_names: "build-and-test.yml",
            invertible: true,
            left_alone: [],
            reason: null,
            files: [
              {
                path: ".github/workflows/build-and-test.yml",
                action: "restore",
                before: beforeRollback,
                after: afterRollback,
              },
            ],
          },
          {
            repo_name: "acme-corp/node-worker",
            target_branch: "main",
            pr_number: 74,
            pr_url: "https://github.com/acme-corp/node-worker/pull/74",
            workflow_names: "build-and-test.yml",
            invertible: false,
            left_alone: [],
            reason:
              ".github/workflows/build-and-test.yml changed on main after this campaign merged — rolling back would discard that change.",
            files: [],
          },
        ],
      },
    });
    await installApiMocks(page, state);
    await seedDocsUserProfile(page);

    // Tall enough for the whole dialog: at 900px it cut off mid-way through the
    // "once the rollback merges" choice, which the docs walk through as a step.
    await page.setViewportSize({ width: 1710, height: 1200 });

    await page.goto(`/project/${DOCS_USER}/${encodeURIComponent("NodeBuilds")}`);
    await page.getByRole("button", { name: "PR Campaigns" }).click();
    // Every PR merged, so the campaign lives in Completed and starts collapsed.
    await page.getByRole("button", { name: /Completed Campaigns/i }).click();
    await page.getByText("Campaign: Update build-and-test.yml").click();
    await page.getByTestId("rollback-campaign-button").first().click();
    await page.getByTestId("rollback-summary").waitFor({ timeout: 15000 });
    await page.waitForTimeout(300);

    await waitForAppHeader(page);
    await page.screenshot({ path: "../docs/src/content/docs/assets/screenshots/pr-campaigns/pr-campaign-rollback.png" });
  });

  test("drift detection — bulk resolve", async ({ page }) => {
    const project = makeProject({
      project_name: "NodeBuilds",
      project_code: "NODE",
      github_user: DOCS_USER,
      last_modified_by: DOCS_USER,
      updated_at: "2026-07-24T00:00:00Z",
      pr_state: "draft",
      selected_repos: ["acme-corp/node-api", "acme-corp/node-worker"],
      workflows: [makeWorkflow({ name: "build-and-test.yml", lastModifiedBy: DOCS_USER })],
    });
    // Realistic managed vs. GitHub content so the diff view screenshot shows
    // an actual, readable drift - someone added a lint step directly in
    // GitHub, bypassing ActionsManager.
    const managedYaml = [
      "name: Build and Test",
      "on:",
      "  push:",
      "    branches: [main]",
      "jobs:",
      "  build:",
      "    runs-on: ubuntu-latest",
      "    steps:",
      "      - uses: actions/checkout@v4",
      "      - run: npm ci",
      "      - run: npm test",
      "",
    ].join("\n");
    const githubYaml = [
      "name: Build and Test",
      "on:",
      "  push:",
      "    branches: [main]",
      "jobs:",
      "  build:",
      "    runs-on: ubuntu-latest",
      "    steps:",
      "      - uses: actions/checkout@v4",
      "      - run: npm ci",
      "      - run: npm test",
      "      - run: npm run lint",
      "",
    ].join("\n");

    // Same workflow, same github_sha, drifted identically in both repos -
    // this is what triggers the "N identical - select all" grouping link.
    await mockDriftResponse(page, {
      lastChecked: DOCS_LAST_CHECKED,
      driftedWorkflows: [
        {
          workflow_name: "build-and-test.yml",
          workflow_filename: "build-and-test.yml",
          repo: "acme-corp/node-api",
          has_drift: true,
          github_sha: "f4a1c9e",
          actionsmanager_yaml: managedYaml,
          github_yaml: githubYaml,
        },
        {
          workflow_name: "build-and-test.yml",
          workflow_filename: "build-and-test.yml",
          repo: "acme-corp/node-worker",
          has_drift: true,
          github_sha: "f4a1c9e",
          actionsmanager_yaml: managedYaml,
          github_yaml: githubYaml,
        },
      ],
    });
    await installApiMocks(page, createMockState({ projects: [project] }));
    await seedDocsUserProfile(page);

    await page.goto(`/project/${DOCS_USER}/${encodeURIComponent("NodeBuilds")}`);
    await page.getByTestId("drift-banner").waitFor({ timeout: 15000 });
    await page.getByTestId("review-drift-button").click();
    await page.getByTestId("drift-modal").waitFor();
    await page.waitForTimeout(300);

    await waitForAppHeader(page);
    await page.screenshot({ path: "../docs/src/content/docs/assets/screenshots/drift-detection/drift-bulk-select.png" });

    // Expand one row's diff to show what an actual drift looks like -
    // managed version on the left, current GitHub version (with the extra
    // lint step) on the right.
    await page.getByRole("button", { name: /View Diff/i }).first().click();
    await page.getByText("npm run lint", { exact: false }).waitFor({ timeout: 5000 });
    await page.waitForTimeout(300);

    await page.screenshot({ path: "../docs/src/content/docs/assets/screenshots/drift-detection/drift-diff-view.png" });

    await page.getByRole("button", { name: /Hide Diff/i }).first().click();
    await page.waitForTimeout(300);

    await page.getByTestId("select-all-drifts").click();
    await page.waitForTimeout(300);

    await page.screenshot({ path: "../docs/src/content/docs/assets/screenshots/drift-detection/drift-bulk-toolbar.png" });
  });

  test("drift detection — status row with no drift", async ({ page }) => {
    const project = makeProject({
      ...DOCS_DRIFT_PROJECT,
      workflows: [makeWorkflow({ name: "build-and-test.yml", lastModifiedBy: DOCS_USER })],
    });
    await mockDriftResponse(page, { lastChecked: DOCS_LAST_CHECKED, driftedWorkflows: [] });
    await installApiMocks(page, createMockState({ projects: [project] }));
    await seedDocsUserProfile(page);

    await page.goto(`/project/${DOCS_USER}/${encodeURIComponent("NodeBuilds")}`);
    const statusRow = page.getByTestId("drift-status-row");
    await statusRow.waitFor({ timeout: 15000 });
    await page.waitForTimeout(300);

    // The row itself, not the whole page: it is one thin strip near the top,
    // and a full-page capture would bury the thing the doc is pointing at.
    await statusRow.screenshot({
      path: "../docs/src/content/docs/assets/screenshots/drift-detection/drift-status-row.png",
    });
  });

  test("drift detection — per-project schedule", async ({ page }) => {
    const project = makeProject({
      ...DOCS_DRIFT_PROJECT,
      workflows: [makeWorkflow({ name: "build-and-test.yml", lastModifiedBy: DOCS_USER })],
    });
    await mockDriftResponse(page, { lastChecked: DOCS_LAST_CHECKED, driftedWorkflows: [] });
    await installApiMocks(page, createMockState({ projects: [project] }));
    await seedDocsUserProfile(page);
    await page.route("**/api/drift/settings", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          sweep_enabled: true,
          recheck_interval_minutes: 30,
          batch_size: 5,
          poll_interval_seconds: 60,
        }),
      })
    );

    await page.goto(`/project/${DOCS_USER}/${encodeURIComponent("NodeBuilds")}`);
    // The Project Configs group is collapsed until opened.
    await page.getByRole("button", { name: "Project Configs" }).click();
    await page.getByRole("button", { name: /Drift Detection/i }).click();
    await page.getByTestId("project-drift-interval").waitFor({ timeout: 15000 });
    await page.waitForTimeout(300);

    await waitForAppHeader(page);
    await page.screenshot({
      path: "../docs/src/content/docs/assets/screenshots/drift-detection/project-drift-schedule.png",
    });
  });

  test("drift detection — automatic checks paused", async ({ page }) => {
    const project = makeProject({
      ...DOCS_DRIFT_PROJECT,
      workflows: [makeWorkflow({ name: "build-and-test.yml", lastModifiedBy: DOCS_USER })],
    });
    await mockDriftResponse(page, {
      lastChecked: DOCS_LAST_CHECKED,
      driftedWorkflows: [],
      staleReason: NO_CREDENTIAL_REASON,
    });
    await installApiMocks(page, createMockState({ projects: [project] }));
    await seedDocsUserProfile(page);

    await page.goto(`/project/${DOCS_USER}/${encodeURIComponent("NodeBuilds")}`);
    const statusRow = page.getByTestId("drift-status-row");
    await statusRow.waitFor({ timeout: 15000 });
    await page.waitForTimeout(300);

    await statusRow.screenshot({
      path: "../docs/src/content/docs/assets/screenshots/drift-detection/drift-checks-paused.png",
    });
  });

  test("drift detection — workflow deleted in GitHub", async ({ page }) => {
    const project = makeProject({
      ...DOCS_DRIFT_PROJECT,
      workflows: [makeWorkflow({ name: "deploy-production.yml", lastModifiedBy: DOCS_USER })],
    });
    await mockDriftResponse(page, {
      lastChecked: DOCS_LAST_CHECKED,
      driftedWorkflows: [
        {
          workflow_name: "deploy-production.yml",
          workflow_filename: "deploy-production.yml",
          repo: "acme-corp/node-worker",
          has_drift: true,
          deleted_in_github: true,
          // Empty rather than absent: the panel replaces the diff either way,
          // but a null here makes the row fetch GitHub's side on expand.
          github_yaml: "",
          message: "Workflow was deleted from acme-corp/node-worker",
        },
      ],
    });
    await installApiMocks(page, createMockState({ projects: [project] }));
    await seedDocsUserProfile(page);

    await page.goto(`/project/${DOCS_USER}/${encodeURIComponent("NodeBuilds")}`);
    await page.getByTestId("drift-banner").waitFor({ timeout: 15000 });
    await page.getByTestId("review-drift-button").click();
    await page.getByTestId("drift-modal").waitFor();
    await page.getByRole("button", { name: /View Diff/i }).first().click();
    await page.getByTestId("deleted-in-github-panel").waitFor({ timeout: 5000 });
    await page.waitForTimeout(300);

    await waitForAppHeader(page);
    await page.screenshot({
      path: "../docs/src/content/docs/assets/screenshots/drift-detection/drift-deleted-in-github.png",
    });
  });

  test("drift detection — CODEOWNERS changed on GitHub", async ({ page }) => {
    const repo = "acme-corp/node-api";
    await installApiMocks(page, codeownersProjectState());
    await seedDocsUserProfile(page);
    await mockManagedFiles(page);
    await mockDriftResponse(page, { lastChecked: DOCS_LAST_CHECKED, driftedCodeowners: [{ repo }] });
    await mockCodeowners(page, DOCS_CODEOWNERS);

    await page.goto(`/project/${DOCS_USER}/${encodeURIComponent(DOCS_CODEOWNERS_PROJECT.project_name)}`);
    await page.getByRole("button", { name: "Load GitHub version" }).waitFor({ timeout: 15000 });
    await page.waitForTimeout(300);

    await waitForAppHeader(page);
    await page.screenshot({
      path: "../docs/src/content/docs/assets/screenshots/drift-detection/codeowners-drift.png",
    });
  });

  test("managed files — the section", async ({ page }) => {
    const checkout = makeProject({
      ...DOCS_CODEOWNERS_PROJECT,
      ...DOCS_MANAGED_ROW,
      project_id: 2,
      project_name: "Checkout",
      project_code: "CHK",
      repo_count: 5,
      file_path: "CODEOWNERS",
      drift_status: "drifted",
      drifted_repos: ["acme-corp/checkout-api"],
    });
    const owners = makeProject({ ...DOCS_CODEOWNERS_PROJECT, ...DOCS_MANAGED_ROW });
    await installApiMocks(page, createMockState({ projects: [owners, checkout] }));
    await seedDocsUserProfile(page);
    await mockManagedFiles(page, { ownedCount: 2 });

    await page.goto(`/project/${DOCS_USER}/managed-files`);
    await page.getByTestId("managed-files-new-card").waitFor({ timeout: 15000 });
    await waitForAppHeader(page);
    await page.waitForTimeout(300);

    await page.screenshot({
      path: "../docs/src/content/docs/assets/screenshots/managed-files/section.png",
    });
  });

  test("managed files — a new project", async ({ page }) => {
    const repos = ["node-api", "node-worker", "ledger-service", "identity-api", "notifications-service"]
      .map((name, index) => ({
        id: index + 1, name, full_name: `${DOCS_USER}/${name}`, private: false, default_branch: "main",
      }));
    const owners = { project_id: 1, project_name: DOCS_CODEOWNERS_PROJECT.project_name };
    await installApiMocks(page, createMockState({
      projects: [makeProject({ ...DOCS_CODEOWNERS_PROJECT, ...DOCS_MANAGED_ROW })],
    }));
    await seedDocsUserProfile(page);
    await seedDocsRepos(page, repos);
    await mockManagedFiles(page, {
      ownedCount: 1,
      taken: {
        [`${DOCS_USER}/node-api`]: owners,
        [`${DOCS_USER}/ledger-service`]: { project_id: null, project_name: null },
      },
    });

    // Tall enough for the whole dialog: below 90vh of its height it scrolls, hiding Create project.
    await page.setViewportSize({ width: 1710, height: 1069 });
    await page.goto(`/project/${DOCS_USER}/managed-files`);
    await page.getByTestId("managed-files-new-card").click({ timeout: 15000 });
    await page.getByTestId("managed-files-project-name").fill("Platform services");
    await page.getByText("Used by another project").waitFor({ timeout: 10000 });
    await waitForAppHeader(page);
    for (const name of ["identity-api", "notifications-service"]) {
      await page.getByRole("checkbox", { name: `Select ${DOCS_USER}/${name}` }).click();
    }
    await page.waitForTimeout(300);

    await page.screenshot({
      path: "../docs/src/content/docs/assets/screenshots/managed-files/new-project.png",
    });
  });

  test("managed files — a CODEOWNERS project", async ({ page }) => {
    await installApiMocks(page, codeownersProjectState());
    await seedDocsUserProfile(page);
    await mockManagedFiles(page);
    await mockDriftResponse(page, { lastChecked: DOCS_LAST_CHECKED });
    await mockCodeowners(page, DOCS_CODEOWNERS);

    await page.goto(`/project/${DOCS_USER}/${encodeURIComponent(DOCS_CODEOWNERS_PROJECT.project_name)}`);
    await page.getByRole("button", { name: "Commit Locally" }).waitFor({ timeout: 15000 });
    await page.getByText("No drift detected").waitFor({ timeout: 10000 });
    await page.waitForTimeout(300);

    await waitForAppHeader(page);
    await page.screenshot({
      path: "../docs/src/content/docs/assets/screenshots/managed-files/codeowners-project.png",
    });
  });

  test("projects — one project across several repositories", async ({ page }) => {
    // More than DOCS_REPOS: the point of this image is one project spanning
    // many repositories, with a couple left available so the picker still
    // reads as a choice.
    const repos = [
      "node-api",
      "node-worker",
      "node-web",
      "checkout-api",
      "ledger-service",
      "notifications-worker",
      "marketing-site",
      "design-system",
    ].map((name, i) => ({
      id: i + 1,
      name,
      full_name: `${DOCS_USER}/${name}`,
      private: false,
      default_branch: "main",
      owner_type: "Organization",
    }));
    const selected = repos.slice(0, 6);
    const project = makeProject({
      ...DOCS_SYNCED_PROJECT,
      selected_repos: selected.map((repo) => repo.full_name),
      workflows: [
        makeWorkflow({
          name: "build-and-test.yml",
          lastModifiedBy: DOCS_USER,
          workflowStatus: "synced_with_github",
        }),
      ],
    });
    await installApiMocks(page, createMockState({ projects: [project] }));
    await seedDocsUserProfile(page, undefined, { plan: BETA_PLAN });
    await mockDriftResponse(page, { lastChecked: DOCS_LAST_CHECKED });
    await seedDocsRepos(page, repos);
    await mockDocsRepoBranchConfigs(page, selected);
    // 16:10, the landing page's card ratio, so the card shows the whole frame.
    await page.setViewportSize({ width: 1710, height: 1069 });

    await page.goto(`/project/${DOCS_USER}/${encodeURIComponent("NodeBuilds")}`);
    const reposNav = page.getByRole("button", { name: "Repositories & Branches" });
    await reposNav.waitFor({ timeout: 15000 });
    await reposNav.click();
    await page.getByTestId(`selected-repo-${selected[5].full_name}`).waitFor({ timeout: 15000 });
    await page.waitForTimeout(500);

    await waitForAppHeader(page);
    await page.screenshot({
      path: "../docs/src/content/docs/assets/screenshots/projects/project-repositories.png",
    });
  });

  test("projects — new repository delivery prompt", async ({ page }) => {
    const project = makeProject({
      project_name: "NodeBuilds",
      project_code: "NODE",
      github_user: DOCS_USER,
      last_modified_by: DOCS_USER,
      updated_at: "2026-07-24T00:00:00Z",
      pr_state: "synced",
      selected_repos: ["acme-corp/node-api", "acme-corp/node-worker"],
      workflows: [
        makeWorkflow({
          name: "build-and-test.yml",
          workflowStatus: "synced_with_github",
          lastModifiedBy: DOCS_USER,
        }),
      ],
    });
    await installApiMocks(page, createMockState({ projects: [project] }));
    await seedDocsUserProfile(page);
    // Registered after installApiMocks - routes are LIFO - so the picker
    // offers node-web as the repository to add.
    await seedDocsRepos(page);
    await mockDocsRepoBranchConfigs(page, DOCS_REPOS);

    await page.goto(`/project/${DOCS_USER}/${encodeURIComponent("NodeBuilds")}`);
    const configGroup = page.getByRole("button", { name: "Project Configs" });
    await configGroup.waitFor({ timeout: 15000 });
    if ((await configGroup.getAttribute("aria-expanded")) !== "true") {
      await configGroup.click();
    }
    await page.getByRole("button", { name: "Repositories & Branches" }).click();

    // Add the third repo, then save: the prompt fires on a successful save.
    await page.getByTestId("available-checkbox-acme-corp/node-web").click();
    await page.getByRole("button", { name: /Save/ }).first().click();

    const dialog = page.getByRole("dialog");
    await dialog.waitFor({ timeout: 15000 });
    await page.waitForTimeout(300);

    await waitForAppHeader(page);
    await page.screenshot({
      path: "../docs/src/content/docs/assets/screenshots/projects/new-repo-delivery-prompt.png",
    });
  });

  test("projects — pending delivery reminder", async ({ page }) => {
    const project = makeProject({
      project_name: "NodeBuilds",
      project_code: "NODE",
      github_user: DOCS_USER,
      last_modified_by: DOCS_USER,
      updated_at: "2026-07-24T00:00:00Z",
      pr_state: "synced",
      selected_repos: [
        "acme-corp/node-api",
        "acme-corp/node-worker",
        "acme-corp/node-web",
      ],
      pending_delivery_repos: ["acme-corp/node-web"],
      workflows: [
        makeWorkflow({
          name: "build-and-test.yml",
          workflowStatus: "synced_with_github",
          lastModifiedBy: DOCS_USER,
        }),
      ],
    });
    await mockDriftResponse(page, { lastChecked: DOCS_LAST_CHECKED, driftedWorkflows: [] });
    await installApiMocks(page, createMockState({ projects: [project] }));
    await seedDocsUserProfile(page);
    await seedDocsRepos(page);

    await page.goto(`/project/${DOCS_USER}/${encodeURIComponent("NodeBuilds")}`);
    const notice = page.getByTestId("pending-delivery-notice");
    await notice.waitFor({ timeout: 15000 });
    await page.waitForTimeout(300);

    await waitForAppHeader(page);
    await page.screenshot({
      path: "../docs/src/content/docs/assets/screenshots/projects/pending-delivery-reminder.png",
    });
  });

  test("notifications settings page", async ({ page }) => {
    const project = makeProject({
      project_name: "NodeBuilds",
      project_code: "NODE",
      github_user: DOCS_USER,
      last_modified_by: DOCS_USER,
      updated_at: "2026-07-24T00:00:00Z",
      pr_state: "synced",
      selected_repos: ["acme-corp/node-api"],
    });
    await installApiMocks(page, createMockState({ projects: [project] }));
    await seedDocsUserProfile(page);

    // Override the default empty-state mocks with example data so the
    // screenshot shows what a configured installation looks like.
    await page.route("**/api/notifications/subscriptions", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          {
            subscription_id: 1,
            recipient_email: "platform-team@acme-corp.example",
            project_id: null,
            project_name: null,
            event_types: ["drift.detected", "drift.resolved"],
            notify_on_resolved: true,
          },
        ]),
      }),
    );
    await page.route("**/api/notifications/deliveries", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          {
            delivery_id: 1,
            event_type: "drift.detected",
            project_id: 1,
            project_name: "NodeBuilds",
            recipient_email: "platform-team@acme-corp.example",
            status: "sent",
            attempt_count: 1,
            last_error: null,
            created_at: "2026-07-24T09:00:00Z",
            sent_at: "2026-07-24T09:00:02Z",
          },
        ]),
      }),
    );

    await page.goto(`/workspace/notifications`);
    await page.getByText("platform-team@acme-corp.example").first().waitFor({ timeout: 15000 });
    await waitForAppHeader(page);
    await page.waitForTimeout(300);

    await page.screenshot({ path: "../docs/src/content/docs/assets/screenshots/notifications/notifications-settings.png" });
  });

  test("build metrics — project overview", async ({ page }) => {
    const project = makeProject({
      ...DOCS_BUILD_METRICS_PROJECT,
      workflows: [
        makeWorkflow({ name: "build-and-test.yml", lastModifiedBy: DOCS_USER }),
        makeWorkflow({ name: "deploy-production.yml", lastModifiedBy: DOCS_USER }),
      ],
    });
    await installApiMocks(page, createMockState({ projects: [project] }));
    await seedDocsUserProfile(page);
    await mockDriftResponse(page, { lastChecked: DOCS_LAST_CHECKED });
    await mockBuildMetricsResponse(page, DOCS_BUILD_METRICS);

    await page.goto(`/project/${DOCS_USER}/${encodeURIComponent("NodeBuilds")}`);
    await page.getByRole("button", { name: "Build Metrics" }).click();
    await page.getByTestId("build-metrics-trend").waitFor({ timeout: 15000 });
    await page.waitForTimeout(300);

    await waitForAppHeader(page);
    await page.screenshot({ path: "../docs/src/content/docs/assets/screenshots/build-metrics/build-metrics-overview.png" });
  });

  test("build metrics — scoped to one workflow", async ({ page }) => {
    const project = makeProject({
      ...DOCS_BUILD_METRICS_PROJECT,
      workflows: [makeWorkflow({ name: "deploy-production.yml", lastModifiedBy: DOCS_USER })],
    });
    await installApiMocks(page, createMockState({ projects: [project] }));
    await seedDocsUserProfile(page);
    await mockDriftResponse(page, { lastChecked: DOCS_LAST_CHECKED });
    await mockBuildMetricsResponse(page, DOCS_BUILD_METRICS);

    await page.goto(`/project/${DOCS_USER}/${encodeURIComponent("NodeBuilds")}`);
    await page.getByRole("button", { name: "Build Metrics" }).click();
    await page.getByTestId("build-metrics-workflow-filter").waitFor({ timeout: 15000 });
    await page
      .getByTestId("build-metrics-workflow-filter")
      .selectOption("deploy-production.yml");
    await page.getByText("Last 30 days · deploy-production").waitFor({ timeout: 15000 });
    await page.waitForTimeout(300);

    await waitForAppHeader(page);
    await page.screenshot({ path: "../docs/src/content/docs/assets/screenshots/build-metrics/build-metrics-by-workflow.png" });
  });
});

/**
 * First-boot restore scenes.
 *
 * Deliberately outside the describe above: that block seeds an authenticated
 * session in beforeEach, and these shots are of the sign-in screen, before any
 * account exists.
 */
test.describe("docs screenshots — first-boot restore", () => {
  const REPORT_BODY = {
    upload_token: "docs",
    ok: true,
    errors: [],
    warnings: [],
    total_rows: 1284,
    tables: {
      accounts: 12,
      projects: 8,
      repos: 46,
      workflows: 122,
      workflow_versions: 940,
      project_workflows: 122,
      rulesets: 6,
      codeowners: 8,
      custom_files: 14,
      project_pull_requests: 6,
    },
    app_version: "1.0.0",
    created_at: "2026-08-11T18:30:00+00:00",
    dialect: "postgresql",
  };

  const backupFile = {
    name: "actionsmanager-backup-2026-08-11.tar.gz",
    mimeType: "application/gzip",
    buffer: Buffer.from("archive-bytes"),
  };

  async function uninitialisedInstall(page: import("@playwright/test").Page) {
    await installApiMocks(page, createMockState());
    await page.route("**/api/setup/status", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ uninitialized: true }),
      })
    );
    // Restore is a self-hosted feature, and self-hosted offers PAT sign-in, so
    // the shot keeps the PAT form. installApiMocks only covers /api/*.
    await page.route((url) => url.pathname === "/healthz", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: corsHeaders(route),
        body: JSON.stringify({ allow_insecure_http: false, pat_login_enabled: true }),
      })
    );
  }

  async function mockReport(page: import("@playwright/test").Page, warnings: string[]) {
    await page.route("**/api/setup/restore/validate", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ ...REPORT_BODY, warnings }),
      })
    );
  }


  test("sign-in screen offering restore", async ({ page }) => {
    await uninitialisedInstall(page);

    await page.goto("/");
    await page.getByRole("button", { name: /restore from a backup/i }).waitFor({ timeout: 15000 });
    await page.getByLabel("Personal Access Token login").waitFor({ timeout: 15000 });
    await page.waitForTimeout(300);

    await page.screenshot({
      path: "../docs/src/content/docs/assets/screenshots/backup-restore/first-boot-restore-prompt.png",
    });
  });

  test("restore report before confirming", async ({ page }) => {
    await uninitialisedInstall(page);
    await mockReport(page, []);

    await page.goto("/");
    await page.getByRole("button", { name: /restore from a backup/i }).click();
    await page.getByLabel(/backup archive/i).setInputFiles(backupFile);
    await page.getByText(/1284 row\(s\)/).waitFor({ timeout: 15000 });
    await page.waitForTimeout(300);

    await page.screenshot({
      path: "../docs/src/content/docs/assets/screenshots/backup-restore/first-boot-restore-report.png",
    });
  });

  test("restore report warning about a different SECRET_KEY", async ({ page }) => {
    await uninitialisedInstall(page);
    await mockReport(page, [
      "SECRET_KEY differs from the one this backup was written under. Saved personal access tokens will not decrypt and must be re-entered after restoring.",
    ]);

    await page.goto("/");
    await page.getByRole("button", { name: /restore from a backup/i }).click();
    await page.getByLabel(/backup archive/i).setInputFiles(backupFile);
    await page.getByText(/SECRET_KEY differs/).waitFor({ timeout: 15000 });
    await page.waitForTimeout(300);

    await page.screenshot({
      path: "../docs/src/content/docs/assets/screenshots/backup-restore/first-boot-restore-warning.png",
    });
  });

  test("restore complete", async ({ page }) => {
    await uninitialisedInstall(page);
    await mockReport(page, []);
    await page.route("**/api/setup/restore/apply", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          restored_rows: 1284,
          restored_tables: 31,
          skipped_tables: [],
          warnings: [],
          migrations_ran: true,
        }),
      })
    );

    await page.goto("/");
    await page.getByRole("button", { name: /restore from a backup/i }).click();
    await page.getByLabel(/backup archive/i).setInputFiles(backupFile);
    await page.getByText(/1284 row\(s\)/).waitFor({ timeout: 15000 });
    await page.getByLabel(/type restore to confirm/i).fill("restore");
    await page.getByRole("button", { name: /restore this backup/i }).click();
    await page.getByText(/restore complete/i).waitFor({ timeout: 15000 });
    await page.waitForTimeout(300);

    await page.screenshot({
      path: "../docs/src/content/docs/assets/screenshots/backup-restore/first-boot-restore-complete.png",
    });
  });
});
