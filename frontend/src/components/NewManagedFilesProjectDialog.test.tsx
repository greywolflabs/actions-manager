import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import '@testing-library/jest-dom';
import type { Mock } from 'vitest';
import NewManagedFilesProjectDialog from './NewManagedFilesProjectDialog';
import { saveProject } from '../api/projects';
import { fetchTakenRepos, type ManagedFilesSection } from '../api/managedFiles';
import { fetchRepos } from '../api/repos';
import type { Plan } from '../api/user';

vi.mock('../api/projects', () => ({
  saveProject: vi.fn(),
}));

vi.mock('../api/managedFiles', () => ({
  fetchTakenRepos: vi.fn(),
}));

vi.mock('../api/repos', () => ({
  fetchRepos: vi.fn(),
  fetchRwxRepos: vi.fn(),
}));

vi.mock('../utils/toast', () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() },
}));

const SECTION: ManagedFilesSection = {
  kinds: [
    { kind: 'codeowners', label: 'CODEOWNERS', paths: ['.github/CODEOWNERS', 'CODEOWNERS', 'docs/CODEOWNERS'] },
    { kind: 'dependabot', label: 'Dependabot', paths: ['.github/dependabot.yml'] },
  ],
  owned_count: 0,
  limit: 3,
  limit_message: null,
};

const REPOS = [
  { id: 1, name: 'api', full_name: 'octo/api', private: false, default_branch: 'main' },
  { id: 2, name: 'billing', full_name: 'octo/billing', private: false, default_branch: 'main' },
  { id: 3, name: 'web', full_name: 'octo/web', private: false, default_branch: 'main' },
  { id: 4, name: 'secret', full_name: 'octo/secret', private: true, default_branch: 'main' },
];

const PLAN = {
  private_repositories: true,
  private_repositories_help: 'Private repositories only.',
  limit_messages: {},
} as unknown as Plan;

const onCreated = vi.fn();
const onClose = vi.fn();

const renderDialog = (section: ManagedFilesSection = SECTION, plan: Plan = PLAN) =>
  render(
    <NewManagedFilesProjectDialog
      open
      user="alice"
      section={section}
      plan={plan}
      onClose={onClose}
      onCreated={onCreated}
    />,
  );

describe('NewManagedFilesProjectDialog', () => {
  const user = userEvent.setup();

  beforeEach(() => {
    vi.clearAllMocks();
    (fetchRepos as Mock).mockResolvedValue(REPOS);
    (fetchTakenRepos as Mock).mockResolvedValue({});
  });

  it('says which project slot it uses', async () => {
    renderDialog({ ...SECTION, owned_count: 2 });

    expect(await screen.findByTestId('managed-files-slot')).toHaveTextContent('This uses your last project (3 of 3).');
  });

  it('counts from the projects already owned', async () => {
    renderDialog();

    expect(await screen.findByTestId('managed-files-slot')).toHaveTextContent('This uses project 1 of 3.');
  });

  it('offers the kinds the server defines', async () => {
    renderDialog();

    const select = await screen.findByTestId('managed-files-kind');
    expect(within(select).getAllByRole('option').map((option) => option.textContent)).toEqual(['CODEOWNERS', 'Dependabot']);
  });

  it('locks repositories another project of the kind uses, and labels them', async () => {
    (fetchTakenRepos as Mock).mockResolvedValue({
      'octo/api': { project_id: 5, project_name: 'Payments services' },
      'octo/billing': { project_id: null, project_name: null },
    });

    renderDialog();

    const api = await screen.findByTestId('available-repo-octo/api');
    await waitFor(() => expect(api).toHaveAttribute('data-unavailable', 'true'));
    expect(api).toHaveTextContent('Used by Payments services');
    expect(screen.getByTestId('available-repo-octo/billing')).toHaveTextContent('Used by another project');
    expect(screen.getByTestId('available-checkbox-octo/api')).toBeDisabled();
    expect(screen.getByTestId('available-checkbox-octo/web')).toBeEnabled();
    expect(fetchTakenRepos).toHaveBeenLastCalledWith('codeowners', ['octo/api', 'octo/billing', 'octo/web'], undefined);
  });

  it('asks again for another kind, and drops picks that kind holds', async () => {
    (fetchTakenRepos as Mock).mockImplementation(async (kind: string) => (
      kind === 'dependabot' ? { 'octo/web': { project_id: 6, project_name: 'Updates' } } : {}
    ));

    renderDialog();
    await user.click(await screen.findByTestId('available-checkbox-octo/web'));
    expect(screen.getByTestId('managed-files-selected-count')).toHaveTextContent('1 repository selected');

    await user.selectOptions(screen.getByTestId('managed-files-kind'), 'dependabot');

    await waitFor(() => expect(screen.getByTestId('available-repo-octo/web')).toHaveAttribute('data-unavailable', 'true'));
    expect(screen.getByTestId('managed-files-selected-count')).toHaveTextContent('0 repositories selected');
  });

  it('creates only with a name and a repository, and sends the project', async () => {
    (saveProject as Mock).mockResolvedValue({ project_id: '9', project_code: 'OWN' });
    renderDialog();
    const create = screen.getByTestId('managed-files-create-button');

    expect(create).toBeDisabled();
    await user.type(screen.getByTestId('managed-files-project-name'), 'Owners');
    expect(create).toBeDisabled();
    await user.click(await screen.findByTestId('available-checkbox-octo/api'));
    expect(screen.getByTestId('managed-files-selected-count')).toHaveTextContent('1 repository selected');
    await user.click(create);

    await waitFor(() => expect(onCreated).toHaveBeenCalledWith('Owners'));
    expect(saveProject).toHaveBeenCalledWith(expect.objectContaining({
      project_name: 'Owners',
      selected_repos: ['octo/api'],
      workflows: [],
      project_type: 'managed_files',
      file_kind: 'codeowners',
      repository_visibility_scope: 'public',
    }));
    expect(saveProject).not.toHaveBeenCalledWith(expect.objectContaining({ project_color: expect.anything() }));
  });

  it('switching visibility lists the other repositories and clears the selection', async () => {
    renderDialog();
    await user.click(await screen.findByTestId('available-checkbox-octo/api'));

    await user.click(screen.getByTestId('visibility-option-private'));

    expect(screen.getByTestId('managed-files-selected-count')).toHaveTextContent('0 repositories selected');
    expect(await screen.findByTestId('available-repo-octo/secret')).toBeInTheDocument();
    expect(screen.queryByTestId('available-repo-octo/api')).not.toBeInTheDocument();
  });

  it('keeps private repositories off when the plan refuses them', async () => {
    renderDialog(SECTION, { ...PLAN, private_repositories: false } as Plan);

    expect(await screen.findByRole('radio', { name: /Private repositories/ })).toBeDisabled();
  });

  it("shows the server's reason when it refuses the project", async () => {
    (saveProject as Mock).mockRejectedValue({
      response: { status: 409, data: { detail: "A repository can be in one CODEOWNERS project. octo/api is used by 'Payments'." } },
    });
    renderDialog();
    await user.type(screen.getByTestId('managed-files-project-name'), 'Owners');
    await user.click(await screen.findByTestId('available-checkbox-octo/api'));

    await user.click(screen.getByTestId('managed-files-create-button'));

    expect(await screen.findByTestId('managed-files-create-error')).toHaveTextContent(
      "A repository can be in one CODEOWNERS project. octo/api is used by 'Payments'.",
    );
    expect(onCreated).not.toHaveBeenCalled();
  });

  it('has no color picker', async () => {
    renderDialog();

    await screen.findByTestId('managed-files-project-name');
    expect(screen.queryByRole('radiogroup', { name: 'Project Color' })).not.toBeInTheDocument();
    expect(screen.queryByText(/color/i)).not.toBeInTheDocument();
  });
});
