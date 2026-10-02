import type { Plan } from '../api/user';

// The plans GET /api/user/{username} returns (backend/tests/test_tier_plan.py
// and tests/cloud/test_tier_plan.py pin the same values). The frontend has no
// limits or plan wording of its own, so tests take them from here.
export const BETA_PLAN: Plan = {
  tier: 'self-hosted-beta',
  label: 'Self-Hosted Beta',
  summary: 'Self-hosted beta access. Paid plans are not currently available.',
  limits: {
    projects: null,
    caller_projects: 4,
    reusable_projects: 2,
    secrets_per_project: 6,
    env_vars_per_project: 6,
    environments_per_project: 6,
  },
  private_repositories: true,
  private_repositories_help:
    'Use this project for private GitHub repositories only. Requires GitHub credentials with private repository access.',
  limit_messages: {
    caller_projects: 'Beta limit reached (4/4 Caller Workflow Projects).',
    reusable_projects: 'Beta limit reached (2/2 Reusable Workflow Projects).',
    secrets: 'Self-Hosted Beta: You can create up to 6 secrets per project. You have reached the limit.',
    env_vars: 'Self-Hosted Beta: You can create up to 6 environment variables per project. You have reached the limit.',
    environments: 'Self-Hosted Beta: You can create up to 6 deployment environments per project. You have reached the limit.',
  },
  upgrade: null,
};

const PAID_PLAN_PRIVATE_HELP =
  'Use this project for private GitHub repositories only. Private repositories require a Professional or Enterprise plan and the correct GitHub permissions.';

// The cloud sends its own upgrade URL; tests only need one.
export const UPGRADE_URL = 'https://example.com/upgrade';

export const FREE_PLAN: Plan = {
  tier: 'free',
  label: 'Free Plan',
  summary: 'Your ActionsManager subscription tier.',
  limits: {
    projects: 3,
    caller_projects: null,
    reusable_projects: null,
    secrets_per_project: 2,
    env_vars_per_project: 2,
    environments_per_project: 2,
  },
  private_repositories: false,
  private_repositories_help: PAID_PLAN_PRIVATE_HELP,
  limit_messages: {
    secrets: 'Free plan users can create up to 2 secrets per project. You have reached the limit. Upgrade to Professional for up to 10 secrets per project.',
    env_vars: 'Free plan users can create up to 2 environment variables per project. You have reached the limit. Upgrade to Professional for unlimited environment variables.',
    environments: 'Free plan users can create up to 2 deployment environments per project. You have reached the limit. Upgrade to Professional for unlimited deployment environments.',
    private_repositories: 'Free plan accounts cannot create private repository projects. Upgrade to Professional or Enterprise to enable private repository projects.',
  },
  upgrade: {
    label: 'Upgrade',
    url: UPGRADE_URL,
    message: 'Upgrade to Professional for up to 10 projects and private repository support.',
  },
};

export const PROFESSIONAL_PLAN: Plan = {
  tier: 'professional',
  label: 'Professional Plan',
  summary: 'Your ActionsManager subscription tier.',
  limits: {
    projects: 10,
    caller_projects: null,
    reusable_projects: null,
    secrets_per_project: 10,
    env_vars_per_project: null,
    environments_per_project: null,
  },
  private_repositories: true,
  private_repositories_help: PAID_PLAN_PRIVATE_HELP,
  limit_messages: {
    secrets: 'Professional plan users can create up to 10 secrets per project. You have reached the limit. Upgrade to Enterprise for unlimited secrets.',
  },
  upgrade: {
    label: 'Upgrade',
    url: UPGRADE_URL,
    message: 'Upgrade to Enterprise for unlimited projects and expanded team usage.',
  },
};

export const ENTERPRISE_PLAN: Plan = {
  tier: 'enterprise',
  label: 'Enterprise Plan',
  summary: 'Your ActionsManager subscription tier.',
  limits: {
    projects: null,
    caller_projects: null,
    reusable_projects: null,
    secrets_per_project: null,
    env_vars_per_project: null,
    environments_per_project: null,
  },
  private_repositories: true,
  private_repositories_help: PAID_PLAN_PRIVATE_HELP,
  limit_messages: {},
  upgrade: null,
};
