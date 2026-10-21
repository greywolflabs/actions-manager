import type { Page } from "@playwright/test";
import type { Plan } from "../src/api/user";
import { BETA_PLAN } from "../src/test/plans";

/**
 * Shared seed data for docs-screenshots specs. Deliberately separate from
 * e2e/fixtures/mocks' TEST_USER/TEST_USER_DETAILS — docs assets published in
 * the public docs site use doc-friendly usernames/projects, never the shared
 * e2e fixtures' "octocat"/"demo-project".
 */
export const DOCS_USER = "acme-corp";

/** Repositories shown in docs screenshots. The shared e2e fixtures use
 *  octocat/hello-world, which must never appear in published documentation. */
export const DOCS_REPOS = [
  { id: 1, name: "node-api", full_name: `${DOCS_USER}/node-api`, private: false, default_branch: "main" },
  { id: 2, name: "node-worker", full_name: `${DOCS_USER}/node-worker`, private: false, default_branch: "main" },
  { id: 3, name: "node-web", full_name: `${DOCS_USER}/node-web`, private: false, default_branch: "main" },
];

/** Replaces the shared fixtures' repository list with doc-friendly names.
 *  Register after installApiMocks (routes are LIFO). */
export async function seedDocsRepos(page: Page, repos: readonly object[] = DOCS_REPOS) {
  await page.route(
    (url) => url.pathname === "/api/repos",
    (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(repos),
      })
  );
}

/** Overrides the /api/user/:username mock so the UI shows DOCS_USER instead
 *  of the shared fixtures' TEST_USER_DETAILS. Must be registered after
 *  installApiMocks (Playwright routes are LIFO) — same pattern as
 *  overrideUserAccountType in e2e/fixtures/mocks.ts. */
export interface DocsOnboarding {
  completed: boolean;
  completed_at: string | null;
  step: string | null;
}

/** Defaults to BETA_PLAN: the public docs document the self-hosted beta, so
 *  the plan usage pill and limit wording must match what that build renders. */
export async function seedDocsUserProfile(
  page: Page,
  onboarding?: DocsOnboarding,
  options: { plan?: Plan } = {},
) {
  // The onboarding write the tour makes when a step advances; screenshots
  // never assert on it, they just must not 404.
  await page.route(
    (url) => /^\/api\/user\/[^/]+\/onboarding$/.test(url.pathname),
    (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(onboarding ?? { completed: true, completed_at: null, step: null }),
      })
  );

  await page.route(
    (url) => /^\/api\/user\/[^/]+$/.test(url.pathname),
    (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          username: DOCS_USER,
          github_user: DOCS_USER,
          avatar_url: "",
          account_type: "Professional",
          github_account_type: "Organization",
          workspace_role: "admin",
          // Omitted by default so the welcome screen and tour never appear in
          // screenshots that are documenting something else.
          ...(onboarding ? { onboarding } : {}),
          plan: options.plan ?? BETA_PLAN,
        }),
      })
  );
}

/** The account menu renders once the user's details load; a capture taken
 *  before then shows the app header without it. CSS, not a role, so it still
 *  finds the menu behind a modal that hides the page from assistive tech. */
export async function waitForAppHeader(page: Page) {
  await page.locator('[data-testid="app-header"] button[aria-label^="User menu"]').waitFor({ timeout: 15000 });
}
