import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import '@testing-library/jest-dom';
import type { Mock } from 'vitest';
import ManagedFilesList from './ManagedFilesList';
import { listProjects, saveProject, type Project } from './api/projects';
import { fetchManagedFiles, fetchTakenRepos, type ManagedFilesSection } from './api/managedFiles';
import { fetchRepos } from './api/repos';

const mockNavigate = vi.fn();

vi.mock('react-router', () => ({
  useNavigate: () => mockNavigate,
}));

vi.mock('./api/projects', () => ({
  listProjects: vi.fn(),
  saveProject: vi.fn(),
}));

vi.mock('./api/managedFiles', () => ({
  fetchManagedFiles: vi.fn(),
  fetchTakenRepos: vi.fn(),
}));

vi.mock('./api/repos', () => ({
  fetchRepos: vi.fn(),
  fetchRwxRepos: vi.fn(),
}));

vi.mock('./utils/toast', () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() },
}));

const CODEOWNERS = { kind: 'codeowners', label: 'CODEOWNERS', paths: ['.github/CODEOWNERS', 'CODEOWNERS', 'docs/CODEOWNERS'] };

const section = (overrides: Partial<ManagedFilesSection> = {}): ManagedFilesSection => ({
  kinds: [CODEOWNERS],
  owned_count: 1,
  limit: 3,
  limit_message: 'Beta limit reached (3/3 Managed Files projects).',
  ...overrides,
});

const project = (overrides: Partial<Project> = {}): Project => ({
  project_id: 7,
  project_name: 'Payments services',
  project_code: 'PAY',
  updated_at: '2026-10-17T10:00:00Z',
  pr_state: 'synced',
  project_type: 'managed_files',
  project_color: null,
  file_kind: 'codeowners',
  owner: 'alice',
  repo_count: 12,
  file_path: '.github/CODEOWNERS',
  drifted_repos: [],
  drift_status: 'clean',
  last_drift_check_at: '2026-10-17T11:00:00Z',
  ...overrides,
});

const renderList = () => render(<ManagedFilesList user="alice" />);

describe('ManagedFilesList', () => {
  const user = userEvent.setup();

  beforeEach(() => {
    vi.clearAllMocks();
    (listProjects as Mock).mockResolvedValue([project()]);
    (fetchManagedFiles as Mock).mockResolvedValue(section());
    (fetchTakenRepos as Mock).mockResolvedValue({});
    (fetchRepos as Mock).mockResolvedValue([
      { id: 1, name: 'api', full_name: 'octo/api', private: false, default_branch: 'main' },
    ]);
  });

  it('lists only the section, from the managed_files list', async () => {
    renderList();

    await screen.findByText('Payments services');
    expect(listProjects).toHaveBeenCalledWith('managed_files');
  });

  it('shows what each card needs', async () => {
    (listProjects as Mock).mockResolvedValue([
      project({
        drift_status: 'drifted',
        drifted_repos: ['your-org/billing-api', 'your-org/web'],
      }),
    ]);

    renderList();

    const card = await screen.findByTestId('managed-files-card-7');
    expect(within(card).getByTestId('managed-files-kind-7')).toHaveTextContent('CODEOWNERS');
    expect(within(card).getByTestId('managed-files-repos-7')).toHaveTextContent('12 repositories');
    expect(within(card).getByTestId('managed-files-repos-7')).toHaveTextContent('.github/CODEOWNERS');
    expect(within(card).getByTestId('managed-files-status-7')).toHaveTextContent('Synced');
    expect(within(card).getByTestId('managed-files-drift-7')).toHaveTextContent('Drift detected');
    expect(within(card).getByTestId('managed-files-drift-note-7')).toHaveTextContent(
      'your-org/billing-api and 1 more changed the file on GitHub',
    );
    expect(within(card).getByText(/^Updated /)).toBeInTheDocument();
    expect(within(card).getByTestId('managed-files-drift-checked-7')).toHaveTextContent(/^Drift checked /);
    // Kind sits under the name as plain text; status and drift sit with the menu, not in the body.
    expect(within(card).getByTestId('managed-files-kind-7').previousElementSibling).toHaveTextContent('Payments services');
    expect(within(card).getByTestId('managed-files-status-7').parentElement).toContainElement(
      within(card).getByTestId('managed-files-drift-7'),
    );
  });

  it('names the owner only on projects the caller does not own', async () => {
    (listProjects as Mock).mockResolvedValue([
      project(),
      project({ project_id: 8, project_name: 'Web', owner: 'bob' }),
    ]);

    renderList();

    expect(await screen.findByTestId('managed-files-owner-8')).toHaveTextContent('Owned by bob');
    expect(screen.queryByTestId('managed-files-owner-7')).not.toBeInTheDocument();
  });

  it('renders the neutral style, never the blue fallback', async () => {
    renderList();

    const icon = await screen.findByTestId('managed-files-icon-7');
    expect(icon.className).toContain('bg-slate-500/10');
    expect(icon.innerHTML).not.toMatch(/blue/);
  });

  it('shows the owned count against the limit, and what is left', async () => {
    renderList();

    expect(await screen.findByTestId('managed-files-meter')).toHaveTextContent('You own 1 of 3');
    expect(screen.getByTestId('managed-files-new-card')).toHaveTextContent('2 of 3 remaining');
    expect(screen.getByTestId('new-managed-files-project-button')).toBeEnabled();
    expect(screen.queryByTestId('managed-files-limit-message')).not.toBeInTheDocument();
  });

  it('counts owned projects, not the grid', async () => {
    (listProjects as Mock).mockResolvedValue([
      project(),
      project({ project_id: 8, project_name: 'Web', owner: 'bob' }),
    ]);

    renderList();

    expect(await screen.findByTestId('managed-files-meter')).toHaveTextContent('You own 1 of 3');
    expect(screen.getByTestId('managed-files-new-card')).toHaveTextContent('2 of 3 remaining');
  });

  it('at the limit, disables New project and says why', async () => {
    (fetchManagedFiles as Mock).mockResolvedValue(section({ owned_count: 3 }));

    renderList();

    expect(await screen.findByTestId('managed-files-meter')).toHaveTextContent('You own 3 of 3');
    expect(screen.getByTestId('new-managed-files-project-button')).toBeDisabled();
    expect(screen.getByTestId('managed-files-limit-message')).toHaveTextContent(
      'Beta limit reached (3/3 Managed Files projects).',
    );
    expect(screen.queryByTestId('managed-files-new-card')).not.toBeInTheDocument();
  });

  it('without a limit, shows no meter bar and no remaining count', async () => {
    (fetchManagedFiles as Mock).mockResolvedValue(section({ limit: null, limit_message: null }));

    renderList();

    const meter = await screen.findByTestId('managed-files-meter');
    expect(meter).toHaveTextContent('You own 1 (no limit)');
    expect(within(meter).queryByRole('progressbar')).not.toBeInTheDocument();
    expect(screen.getByTestId('managed-files-new-card')).toHaveTextContent(/^New project$/);
  });

  it('shows the empty state when there are no projects', async () => {
    (listProjects as Mock).mockResolvedValue([]);
    (fetchManagedFiles as Mock).mockResolvedValue(section({ owned_count: 0 }));

    renderList();

    const empty = await screen.findByTestId('managed-files-empty');
    expect(empty).toHaveTextContent('No Managed Files projects yet');
    expect(within(empty).getByTestId('managed-files-empty-new-button')).toBeEnabled();
  });

  it('renders a kind defined only by the server', async () => {
    (fetchManagedFiles as Mock).mockResolvedValue(
      section({ kinds: [{ kind: 'dependabot', label: 'Dependabot', paths: ['.github/dependabot.yml'] }] }),
    );
    (listProjects as Mock).mockResolvedValue([project({ file_kind: 'dependabot', file_path: '.github/dependabot.yml' })]);

    renderList();

    expect(await screen.findByTestId('managed-files-kind-7')).toHaveTextContent('Dependabot');
    // No icon of its own: the generic file icon.
    expect(screen.getByTestId('managed-files-icon-7').querySelector('svg')).toHaveClass('lucide-file-text');
    await user.click(screen.getByTestId('managed-files-new-card'));
    expect(await screen.findByRole('option', { name: 'Dependabot' })).toBeInTheDocument();
  });

  it('says the list failed to load, rather than that there are no projects', async () => {
    (listProjects as Mock).mockRejectedValue({ response: { status: 500, data: { detail: 'Database unavailable' } } });

    renderList();

    expect(await screen.findByRole('alert')).toHaveTextContent('Database unavailable');
    expect(screen.queryByTestId('managed-files-empty')).not.toBeInTheDocument();
  });

  it('opens a project by name, with its id', async () => {
    renderList();

    await user.click(await screen.findByRole('button', { name: 'Open project Payments services' }));

    expect(mockNavigate).toHaveBeenCalledWith('/project/alice/Payments services', { state: { projectId: 7 } });
  });

  it('shows a new project as soon as it is created, without a reload', async () => {
    (listProjects as Mock).mockResolvedValueOnce([]).mockResolvedValue([
      project({ project_id: 9, project_name: 'Owners', repo_count: 1, file_path: null, pr_state: 'new' }),
    ]);
    (fetchManagedFiles as Mock)
      .mockResolvedValueOnce(section({ owned_count: 0 }))
      .mockResolvedValue(section({ owned_count: 1 }));
    (saveProject as Mock).mockResolvedValue({ project_id: '9', project_code: 'OWN' });

    renderList();

    await user.click(within(await screen.findByTestId('managed-files-empty')).getByTestId('managed-files-empty-new-button'));
    await user.type(await screen.findByTestId('managed-files-project-name'), 'Owners');
    await user.click(await screen.findByTestId('available-checkbox-octo/api'));
    await user.click(screen.getByTestId('managed-files-create-button'));

    expect(await screen.findByTestId('managed-files-card-9')).toHaveTextContent('Owners');
    expect(screen.getByTestId('managed-files-meter')).toHaveTextContent('You own 1 of 3');
    await waitFor(() => expect(screen.queryByTestId('new-managed-files-dialog')).not.toBeInTheDocument());
  });
});
