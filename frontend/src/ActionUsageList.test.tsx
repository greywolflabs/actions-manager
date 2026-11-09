import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import '@testing-library/jest-dom';
import type { Mock } from 'vitest';
import ActionUsageList from './ActionUsageList';
import { applyPin, fetchActionUsage, fetchLatestVersion, previewPin, updateWorkflow, type ActionUsage, type ActionUsageEntry, type ActionVersion } from './api/actionUsage';
import { toast } from './utils/toast';

const mockNavigate = vi.fn();
vi.mock('react-router', () => ({ useNavigate: () => mockNavigate }));

vi.mock('./utils/toast', () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() },
}));

vi.mock('./api/actionUsage', () => ({
  fetchActionUsage: vi.fn(),
  fetchLatestVersion: vi.fn(),
  updateWorkflow: vi.fn(),
  previewPin: vi.fn(),
  applyPin: vi.fn(),
}));

const LATEST = { action: 'actions/checkout', ref: 'v5.0.0', sha: '2'.repeat(40), comment: 'v5.0.0' };

const FULL_SHA = '11bd71901bbe5b1630ceea73d27597364c9af683';

const entry = (overrides: Partial<ActionUsageEntry> = {}): ActionUsageEntry => ({
  project_id: 1,
  project_name: 'Payments',
  workflow_id: 10,
  workflow_name: 'ci',
  workflow_file: 'AM_PAY_ci.yml',
  workflow_status: 'synced_with_github',
  reusable: false,
  project_type: 'standard',
  editable: true,
  repos: ['octo/api', 'octo/web'],
  override: false,
  paths: [],
  ...overrides,
});

const version = (overrides: Partial<ActionVersion> = {}): ActionVersion => ({
  ref: 'v3',
  kind: 'tag',
  pinned: false,
  workflow_count: 1,
  repo_count: 2,
  usages: [entry()],
  ...overrides,
});

const usage = (overrides: Partial<ActionUsage> = {}): ActionUsage => ({
  actions: [
    {
      action: 'actions/checkout',
      versions: [version(), version({ ref: FULL_SHA, kind: 'sha', pinned: true, usages: [entry({ workflow_id: 11, override: true, repos: ['octo/web'] })] })],
      workflow_count: 2,
      unpinned_workflow_count: 1,
    },
    {
      action: 'actions/setup-node',
      versions: [version({ ref: FULL_SHA, kind: 'sha', pinned: true, usages: [entry({ workflow_id: 12 })] })],
      workflow_count: 1,
      unpinned_workflow_count: 0,
    },
  ],
  other: { local: 2, docker: 1 },
  summary: { action_count: 2, workflows_read: 3, unpinned_workflows: 1 },
  ...overrides,
});

const open = async (user: ReturnType<typeof userEvent.setup>, index = 0) => {
  const row = (await screen.findAllByTestId('action-usage-action'))[index];
  await user.click(within(row).getByTestId('action-usage-toggle'));
  return row;
};

describe('ActionUsageList', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (fetchLatestVersion as Mock).mockResolvedValue(LATEST);
  });

  it('shows one row per action with its status and its versions as chips', async () => {
    (fetchActionUsage as Mock).mockResolvedValue(usage());

    render(<ActionUsageList user="alice" />);

    const rows = await screen.findAllByTestId('action-usage-action');
    expect(rows.map((row) => within(row).getByRole('heading').textContent)).toEqual(['actions/checkout', 'actions/setup-node']);
    expect(within(rows[0]).getByTestId('action-usage-status')).toHaveTextContent('1 not pinned');
    expect(within(rows[1]).getByTestId('action-usage-status')).toHaveTextContent('All pinned');
    const chips = within(rows[0]).getAllByTestId('action-usage-version');
    expect(chips[0]).toHaveTextContent('v3');
    expect(chips[0]).toHaveAttribute('data-pinned', 'false');
    expect(chips[0]).toHaveAccessibleName('v3, tag, not pinned to a SHA, 1 workflow');
    expect(chips[1]).toHaveTextContent('11bd719');
    expect(chips[1]).toHaveAttribute('data-pinned', 'true');
    expect(chips[1]).toHaveAccessibleName('11bd719, pinned to a SHA, 1 workflow');
    expect(rows[0]).toHaveTextContent('2 workflows · 2 repositories');
  });

  it('opens and closes an action by clicking anywhere on its row, and says it is expanded', async () => {
    (fetchActionUsage as Mock).mockResolvedValue(usage());
    const user = userEvent.setup();
    render(<ActionUsageList user="alice" />);
    const row = (await screen.findAllByTestId('action-usage-action'))[0];
    const toggle = within(row).getByTestId('action-usage-toggle');
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(within(row).queryByTestId('action-usage-panel')).not.toBeInTheDocument();

    await user.click(within(row).getByRole('heading'));

    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(within(row).getByTestId('action-usage-panel')).toBeInTheDocument();

    await user.click(toggle);

    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(within(row).queryByTestId('action-usage-panel')).not.toBeInTheDocument();
  });

  it('lists an action\'s workflows in a table with labelled columns', async () => {
    (fetchActionUsage as Mock).mockResolvedValue(usage());
    const user = userEvent.setup();
    render(<ActionUsageList user="alice" />);

    const row = await open(user);

    const headers = within(row).getAllByRole('columnheader').map((header) => header.textContent?.trim());
    expect(headers).toEqual(['Workflow', 'Project', 'Repositories', 'Version', 'Update']);
    const cells = within(within(row).getAllByTestId('action-usage-usage')[0]).getAllByRole('cell');
    expect(cells[0]).toHaveTextContent('AM_PAY_ci.yml');
    expect(cells[1]).toHaveTextContent('Payments');
    expect(cells[2]).toHaveTextContent('2 repositories');
    expect(cells[2]).toHaveAttribute('title', 'octo/api, octo/web');
    expect(cells[3]).toHaveTextContent('v3');
  });

  it('puts the workflows that need an update first, then orders by project and file', async () => {
    (fetchActionUsage as Mock).mockResolvedValue(
      usage({
        actions: [{
          action: 'actions/checkout',
          versions: [
            version({ ref: LATEST.sha, kind: 'sha', pinned: true, usages: [entry({ workflow_id: 1, project_name: 'Alpha', workflow_file: 'a.yml' })] }),
            version({ usages: [
              entry({ workflow_id: 2, project_name: 'Zeta', workflow_file: 'z.yml' }),
              entry({ workflow_id: 3, project_name: 'Beta', workflow_file: 'b2.yml' }),
              entry({ workflow_id: 4, project_name: 'Beta', workflow_file: 'b1.yml' }),
            ] }),
            version({ ref: 'c'.repeat(40), kind: 'sha', pinned: true, usages: [entry({ workflow_id: 5, project_name: 'Alpha', workflow_file: 'old.yml' })] }),
          ],
          workflow_count: 5,
          unpinned_workflow_count: 3,
        }],
      }),
    );
    const user = userEvent.setup();
    render(<ActionUsageList user="alice" />);

    const row = await open(user);
    await within(row).findByTestId('action-usage-latest');

    const files = within(row).getAllByTestId('action-usage-usage').map((tr) => within(tr).getAllByRole('cell')[0].textContent);
    expect(files).toEqual(['b1.yml', 'b2.yml', 'z.yml', 'old.yml', 'a.yml']);
  });

  it('says what a workflow calls inside the action, and marks a reusable workflow and a repository\'s own copy', async () => {
    (fetchActionUsage as Mock).mockResolvedValue(
      usage({
        actions: [{
          action: 'octo/shared',
          versions: [version({ usages: [
            entry({ workflow_id: 1, paths: ['.github/workflows/build.yml'], project_type: 'rwx' }),
            entry({ workflow_id: 2, paths: ['init'], override: true }),
          ] })],
          workflow_count: 2,
          unpinned_workflow_count: 2,
        }],
      }),
    );
    const user = userEvent.setup();
    render(<ActionUsageList user="alice" />);

    const row = await open(user);

    const [first, second] = within(row).getAllByTestId('action-usage-usage');
    expect(first).toHaveTextContent('calls .github/workflows/build.yml');
    expect(first).toHaveTextContent('reusable workflow');
    expect(second).toHaveTextContent('uses init');
    expect(second).toHaveTextContent('own copy');
  });

  it('summarises the workflows read and how many are not pinned, and the references it does not count', async () => {
    (fetchActionUsage as Mock).mockResolvedValue(usage());

    render(<ActionUsageList user="alice" />);

    await screen.findByTestId('action-usage-summary');
    expect(screen.getByTestId('action-usage-stat-actions')).toHaveTextContent('2Actions in use');
    expect(screen.getByTestId('action-usage-stat-unpinned')).toHaveTextContent('1Workflows not pinned to a SHA');
    expect(screen.getByTestId('action-usage-stat-workflows')).toHaveTextContent('3Workflows read');
    expect(screen.getByTestId('action-usage-summary')).toHaveTextContent('2 local and 1 Docker references are not counted.');
    expect(screen.getByTestId('action-usage-help')).toHaveAttribute('title', expect.stringContaining('SHA pinning'));
  });

  it('the not-pinned count turns the Not pinned filter on and, pressed again, off', async () => {
    (fetchActionUsage as Mock).mockResolvedValue(usage());
    const user = userEvent.setup();
    render(<ActionUsageList user="alice" />);
    const tile = await screen.findByTestId('action-usage-stat-unpinned');
    expect(tile).toHaveAttribute('aria-pressed', 'false');
    expect(tile).toHaveTextContent('Show only these →');

    await user.click(tile);

    expect(screen.getByTestId('action-usage-unpinned-only')).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getAllByTestId('action-usage-action')).toHaveLength(1);
    expect(tile).toHaveAttribute('aria-pressed', 'true');
    expect(tile).toHaveTextContent('Show all →');

    await user.click(tile);

    expect(screen.getByTestId('action-usage-all')).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getAllByTestId('action-usage-action')).toHaveLength(2);
    expect(tile).toHaveTextContent('Show only these →');
  });

  it('says how many actions a filter hides, and Show all clears the filter and the search', async () => {
    (fetchActionUsage as Mock).mockResolvedValue(usage());
    const user = userEvent.setup();
    render(<ActionUsageList user="alice" />);
    await screen.findAllByTestId('action-usage-action');
    expect(screen.queryByTestId('action-usage-filtered')).not.toBeInTheDocument();

    await user.click(screen.getByTestId('action-usage-stat-unpinned'));
    expect(screen.getByTestId('action-usage-filtered')).toHaveTextContent('Showing 1 of 2 actions.');

    await user.type(screen.getByTestId('action-usage-filter'), 'zzz');
    expect(screen.getByTestId('action-usage-no-match')).toBeInTheDocument();
    expect(screen.getByTestId('action-usage-filtered')).toHaveTextContent('Showing 0 of 2 actions.');

    await user.click(screen.getByTestId('action-usage-show-all'));

    expect(screen.getAllByTestId('action-usage-action')).toHaveLength(2);
    expect(screen.getByTestId('action-usage-filter')).toHaveValue('');
    expect(screen.getByTestId('action-usage-all')).toHaveAttribute('aria-pressed', 'true');
    expect(screen.queryByTestId('action-usage-filtered')).not.toBeInTheDocument();
  });

  it('Expand all opens every action shown and Collapse all closes them', async () => {
    (fetchActionUsage as Mock).mockResolvedValue(usage());
    const user = userEvent.setup();
    render(<ActionUsageList user="alice" />);
    await screen.findAllByTestId('action-usage-action');

    await user.click(screen.getByTestId('action-usage-expand-all'));
    expect(screen.getAllByTestId('action-usage-panel')).toHaveLength(2);

    await user.click(screen.getByTestId('action-usage-collapse-all'));
    expect(screen.queryByTestId('action-usage-panel')).not.toBeInTheDocument();
  });

  it('filters by action name', async () => {
    (fetchActionUsage as Mock).mockResolvedValue(usage());
    const user = userEvent.setup();
    render(<ActionUsageList user="alice" />);

    await user.type(await screen.findByTestId('action-usage-filter'), 'setup');

    expect(screen.getAllByTestId('action-usage-action')).toHaveLength(1);
    expect(screen.getByRole('heading', { name: 'actions/setup-node' })).toBeInTheDocument();
  });

  it('can show only the actions not pinned to a SHA, and says when nothing matches', async () => {
    (fetchActionUsage as Mock).mockResolvedValue(usage());
    const user = userEvent.setup();
    render(<ActionUsageList user="alice" />);

    await user.click(await screen.findByTestId('action-usage-unpinned-only'));
    expect(screen.getAllByTestId('action-usage-action')).toHaveLength(1);
    expect(screen.getByRole('heading', { name: 'actions/checkout' })).toBeInTheDocument();

    await user.type(screen.getByTestId('action-usage-filter'), 'zzz');
    expect(screen.getByTestId('action-usage-no-match')).toBeInTheDocument();

    await user.click(screen.getByTestId('action-usage-all'));
    await user.clear(screen.getByTestId('action-usage-filter'));
    expect(screen.getAllByTestId('action-usage-action')).toHaveLength(2);
  });

  it('shows an empty state when no workflow uses an action', async () => {
    (fetchActionUsage as Mock).mockResolvedValue(
      usage({ actions: [], other: { local: 0, docker: 0 }, summary: { action_count: 0, workflows_read: 0, unpinned_workflows: 0 } }),
    );

    render(<ActionUsageList user="alice" />);

    expect(await screen.findByTestId('action-usage-empty')).toBeInTheDocument();
    expect(screen.queryByTestId('action-usage-filter')).not.toBeInTheDocument();
  });

  it('shows why it could not load', async () => {
    (fetchActionUsage as Mock).mockRejectedValue({ response: { data: { detail: 'Authentication required' } } });

    render(<ActionUsageList user="alice" />);

    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Authentication required'));
    expect(screen.queryByTestId('action-usage-empty')).not.toBeInTheDocument();
  });

  it('offers Update all only on actions with a workflow the caller can update', async () => {
    (fetchActionUsage as Mock).mockResolvedValue(
      usage({
        actions: [
          { action: 'actions/checkout', versions: [version()], workflow_count: 1, unpinned_workflow_count: 1 },
          { action: 'actions/cache', versions: [version({ usages: [entry({ editable: false })] })], workflow_count: 1, unpinned_workflow_count: 1 },
          { action: 'actions/setup-go', versions: [version({ usages: [entry({ override: true })] })], workflow_count: 1, unpinned_workflow_count: 1 },
          { action: 'actions/setup-java', versions: [version({ usages: [entry({ project_type: 'rwx' })] })], workflow_count: 1, unpinned_workflow_count: 1 },
        ],
      }),
    );

    render(<ActionUsageList user="alice" />);

    const rows = await screen.findAllByTestId('action-usage-action');
    expect(within(rows[0]).getByTestId('action-usage-pin')).toHaveTextContent('Update all');
    expect(within(rows[1]).queryByTestId('action-usage-pin')).not.toBeInTheDocument();
    expect(within(rows[2]).queryByTestId('action-usage-pin')).not.toBeInTheDocument();
    expect(within(rows[3]).getByTestId('action-usage-pin')).toBeInTheDocument();
  });
});

describe('ActionUsageList: updating a workflow from its row', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (fetchLatestVersion as Mock).mockResolvedValue(LATEST);
  });

  it('looks up the latest version only when an action is opened, and once per action', async () => {
    (fetchActionUsage as Mock).mockResolvedValue(usage());
    const user = userEvent.setup();
    render(<ActionUsageList user="alice" />);
    await screen.findAllByTestId('action-usage-action');
    expect(fetchLatestVersion).not.toHaveBeenCalled();

    const row = await open(user);
    await user.click(within(row).getByTestId('action-usage-toggle'));
    await user.click(within(row).getByTestId('action-usage-toggle'));

    await waitFor(() => expect(fetchLatestVersion).toHaveBeenCalledTimes(1));
    expect(fetchLatestVersion).toHaveBeenCalledWith('actions/checkout');
  });

  it('shows the latest release once, above the table', async () => {
    (fetchActionUsage as Mock).mockResolvedValue(usage());
    const user = userEvent.setup();
    render(<ActionUsageList user="alice" />);

    const row = await open(user);

    expect(await within(row).findByTestId('action-usage-latest')).toHaveTextContent('Latest releasev5.0.0→2222222');
    expect(within(row).getAllByTestId('action-usage-latest')).toHaveLength(1);
  });

  it('puts an Update to the latest button on every row that is not on it, and Up to date on the one that is', async () => {
    (fetchActionUsage as Mock).mockResolvedValue(
      usage({
        actions: [{
          action: 'actions/checkout',
          versions: [
            version({ usages: [entry({ workflow_id: 10 }), entry({ workflow_id: 11, workflow_file: 'AM_PAY_lint.yml' })] }),
            version({ ref: LATEST.sha, kind: 'sha', pinned: true, usages: [entry({ workflow_id: 12, workflow_file: 'AM_PAY_done.yml' })] }),
          ],
          workflow_count: 3,
          unpinned_workflow_count: 2,
        }],
      }),
    );
    const user = userEvent.setup();
    render(<ActionUsageList user="alice" />);

    const row = await open(user);

    await waitFor(() => expect(within(row).getAllByTestId('action-usage-update')).toHaveLength(2));
    expect(within(row).getAllByTestId('action-usage-update')[0]).toHaveTextContent('Update to v5.0.0');
    expect(within(row).getByTestId('action-usage-up-to-date')).toHaveTextContent('Up to date');
  });

  it('says it is looking up the latest release while it waits', async () => {
    (fetchActionUsage as Mock).mockResolvedValue(usage());
    (fetchLatestVersion as Mock).mockReturnValue(new Promise(() => undefined));
    const user = userEvent.setup();
    render(<ActionUsageList user="alice" />);

    const row = await open(user);

    expect(within(row).getByTestId('action-usage-latest-looking')).toHaveTextContent('Looking up the latest release');
    expect(within(row).getAllByTestId('action-usage-latest-loading')).not.toHaveLength(0);
  });

  it('explains once that an action has no release to update to, and disables the row buttons', async () => {
    (fetchActionUsage as Mock).mockResolvedValue(usage());
    (fetchLatestVersion as Mock).mockRejectedValue({ response: { data: { detail: 'actions/checkout has no release or version tag to default to.' } } });
    const user = userEvent.setup();
    render(<ActionUsageList user="alice" />);

    const row = await open(user);

    const note = await within(row).findByTestId('action-usage-no-release');
    expect(note).toHaveTextContent('No release to update to.');
    expect(note).toHaveTextContent('actions/checkout has no release or version tag to default to.');
    expect(note).toHaveTextContent('choose Update all and then Change');
    expect(within(row).getAllByTestId('action-usage-no-release')).toHaveLength(1);
    const button = within(row).getByTestId('action-usage-latest-error');
    expect(button).toBeDisabled();
    expect(button).toHaveTextContent('Not available');
    expect(within(row).queryByTestId('action-usage-update')).not.toBeInTheDocument();
  });

  it('shows no button on a repository\'s own copy or in a project the caller cannot edit, and a disabled one under review', async () => {
    (fetchActionUsage as Mock).mockResolvedValue(
      usage({
        actions: [{
          action: 'actions/checkout',
          versions: [version({
            usages: [
              entry({ workflow_id: 1, override: true }),
              entry({ workflow_id: 2, editable: false }),
              entry({ workflow_id: 3, workflow_status: 'under_review' }),
              entry({ workflow_id: 4, project_type: 'rwx' }),
            ],
          })],
          workflow_count: 4,
          unpinned_workflow_count: 4,
        }],
      }),
    );
    const user = userEvent.setup();
    render(<ActionUsageList user="alice" />);

    const row = await open(user);

    await waitFor(() => expect(within(row).getAllByTestId('action-usage-update')).toHaveLength(1));
    expect(within(row).getByTestId('action-usage-update-blocked')).toBeDisabled();
    expect(within(row).getByTestId('action-usage-update-blocked')).toHaveTextContent('Under review');
  });

  it('Update and commit locally saves it in the project, says so, and the inventory reloads without a refresh', async () => {
    const reloaded = usage({
      actions: [{ action: 'actions/checkout', versions: [version({ ref: LATEST.sha, kind: 'sha', pinned: true })], workflow_count: 1, unpinned_workflow_count: 0 }],
      summary: { action_count: 1, workflows_read: 1, unpinned_workflows: 0 },
    });
    (fetchActionUsage as Mock).mockResolvedValueOnce(usage()).mockResolvedValue(reloaded);
    (updateWorkflow as Mock).mockResolvedValue({
      workflow_id: 10, workflow_name: 'ci', workflow_file: 'AM_PAY_ci.yml', project_id: 1, project_name: 'Payments',
      reusable: false, changed: 1, pr_state: 'draft', workflow_status: 'committed_locally',
    });
    const user = userEvent.setup();
    render(<ActionUsageList user="alice" />);
    const row = await open(user);

    await user.click((await within(row).findAllByTestId('action-usage-update'))[0]);
    await user.click(await screen.findByTestId('action-usage-update-commit'));

    await waitFor(() => expect(updateWorkflow).toHaveBeenCalledWith({
      workflow_id: 10, action: 'actions/checkout', sha: LATEST.sha, comment: 'v5.0.0', commit: true,
    }));
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith(expect.stringContaining('AM_PAY_ci.yml in Payments and committed it locally')));
    await waitFor(() => expect(screen.getByTestId('action-usage-stat-unpinned')).toHaveTextContent('0Workflows not pinned to a SHA'));
    expect(fetchActionUsage).toHaveBeenCalledTimes(2);
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it('Update and review opens the project with the updated workflow as an unsaved edit, and saves nothing', async () => {
    (fetchActionUsage as Mock).mockResolvedValue(usage());
    (updateWorkflow as Mock).mockResolvedValue({
      workflow_id: 10, workflow_name: 'ci', workflow_file: 'AM_PAY_ci.yml', project_id: 1, project_name: 'Payments',
      reusable: false, changed: 1, content: 'name: ci\n',
    });
    const user = userEvent.setup();
    render(<ActionUsageList user="alice" />);
    const row = await open(user);

    await user.click((await within(row).findAllByTestId('action-usage-update'))[0]);
    await user.click(await screen.findByTestId('action-usage-update-review'));

    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith('/project/alice/Payments', {
      state: { projectId: 1, draftWorkflow: { name: 'ci', content: 'name: ci\n', reusable: false } },
    }));
    expect(updateWorkflow).toHaveBeenCalledWith(expect.objectContaining({ commit: false }));
    expect(toast.success).not.toHaveBeenCalled();
    expect(fetchActionUsage).toHaveBeenCalledTimes(1);
  });

  it('says why an update failed and leaves the page as it was', async () => {
    (fetchActionUsage as Mock).mockResolvedValue(usage());
    (updateWorkflow as Mock).mockRejectedValue({ response: { data: { detail: 'Under review: it has an open pull request. Close or merge that first.' } } });
    const user = userEvent.setup();
    render(<ActionUsageList user="alice" />);
    const row = await open(user);

    await user.click((await within(row).findAllByTestId('action-usage-update'))[0]);
    await user.click(await screen.findByTestId('action-usage-update-commit'));

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Under review: it has an open pull request. Close or merge that first.'));
    expect(fetchActionUsage).toHaveBeenCalledTimes(1);
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it('Update all opens the dialog for the action without opening the row, and reloads once it has updated the workflows', async () => {
    (fetchActionUsage as Mock).mockResolvedValue(usage());
    (previewPin as Mock).mockResolvedValue({
      action: 'actions/checkout', ref: 'v5.0.0', source: 'latest release', sha: FULL_SHA, comment: 'v5.0.0',
      projects: [{
        project_id: 1, project_name: 'Payments', project_type: 'standard', editable: true, skipped_repos: [],
        workflows: [{ workflow_id: 10, workflow_name: 'ci', workflow_file: 'AM_PAY_ci.yml', reusable: false, lines: [] }],
      }],
    });
    (applyPin as Mock).mockResolvedValue({ updated: 1, workflows: ['ci'], pr_state: 'draft' });
    const user = userEvent.setup();
    render(<ActionUsageList user="alice" />);
    const row = (await screen.findAllByTestId('action-usage-action'))[0];

    await user.click(within(row).getByTestId('action-usage-pin'));

    expect(within(row).getByTestId('action-usage-toggle')).toHaveAttribute('aria-expanded', 'false');
    await user.click(await screen.findByTestId('pin-apply'));
    await waitFor(() => expect(applyPin).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(fetchActionUsage).toHaveBeenCalledTimes(2));
  });
});
