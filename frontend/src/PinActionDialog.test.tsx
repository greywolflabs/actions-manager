import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import '@testing-library/jest-dom';
import type { Mock } from 'vitest';
import PinActionDialog from './PinActionDialog';
import { applyPin, previewPin, type PinPreview, type PinProjectChange } from './api/actionUsage';

vi.mock('./api/actionUsage', () => ({
  previewPin: vi.fn(),
  applyPin: vi.fn(),
}));

const SHA = '11bd71901bbe5b1630ceea73d27597364c9af683';

const project = (overrides: Partial<PinProjectChange> = {}): PinProjectChange => ({
  project_id: 1,
  project_name: 'Payments',
  project_type: 'standard',
  editable: true,
  skipped_repos: [],
  workflows: [
    {
      workflow_id: 10,
      workflow_name: 'ci',
      workflow_file: 'AM_PAY_ci.yml',
      reusable: false,
      blocked: null,
      lines: [{ line: 7, before: '      - uses: actions/checkout@v3', after: `      - uses: actions/checkout@${SHA} # v5.0.0` }],
    },
  ],
  ...overrides,
});

const preview = (projects: PinProjectChange[], overrides: Partial<PinPreview> = {}): PinPreview => ({
  action: 'actions/checkout',
  ref: 'v5.0.0',
  source: 'latest release',
  sha: SHA,
  comment: 'v5.0.0',
  projects,
  ...overrides,
});

const open = (onClose = vi.fn(), onApplied = vi.fn()) => {
  const user = userEvent.setup();
  render(<PinActionDialog action="actions/checkout" onClose={onClose} onApplied={onApplied} />);
  return { user, onClose, onApplied };
};

describe('PinActionDialog', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('opens already on the latest release, with its tag and commit in one line and the change one click away', async () => {
    (previewPin as Mock).mockResolvedValue(preview([project()]));
    const { user } = open();

    const target = await screen.findByTestId('pin-preview-result');

    expect(previewPin).toHaveBeenCalledTimes(1);
    expect(previewPin).toHaveBeenCalledWith('actions/checkout', undefined);
    expect(target).toHaveTextContent('Latest release');
    expect(target).toHaveTextContent('v5.0.0 → 11bd719');
    expect(screen.queryByTestId('pin-ref')).not.toBeInTheDocument();
    expect(screen.getByTestId('pin-summary')).toHaveTextContent('1 project · 1 workflow selected');
    expect(screen.getByTestId('pin-apply')).toHaveTextContent('Update 1 workflow');
    expect(screen.queryByText('AM_PAY_ci.yml')).not.toBeInTheDocument();

    await user.click(screen.getByTestId('pin-show-changes'));

    const card = screen.getByTestId('pin-project');
    expect(within(card).getByText('AM_PAY_ci.yml')).toBeInTheDocument();
    expect(within(card).getByText('- uses: actions/checkout@v3', { exact: false })).toBeInTheDocument();
    expect(within(card).getByText(`- uses: actions/checkout@${SHA} # v5.0.0`, { exact: false })).toBeInTheDocument();
    expect(applyPin).not.toHaveBeenCalled();
  });

  it('says it is looking up the latest release until the answer comes', async () => {
    let answer: (value: PinPreview) => void = () => undefined;
    (previewPin as Mock).mockReturnValue(new Promise<PinPreview>((resolve) => { answer = resolve; }));

    open();

    expect(screen.getByTestId('pin-looking-up')).toHaveTextContent('Looking up the latest release of actions/checkout');
    answer(preview([project()]));
    await screen.findByTestId('pin-project');
    expect(screen.queryByTestId('pin-looking-up')).not.toBeInTheDocument();
  });

  it('lists the repositories skipped for having their own copy, and a project the caller cannot change', async () => {
    (previewPin as Mock).mockResolvedValue(
      preview([
        project({ skipped_repos: ['octo/web'] }),
        project({ project_id: 2, project_name: 'Platform', editable: false }),
      ]),
    );

    open();

    expect(await screen.findByTestId('pin-skipped')).toHaveTextContent('octo/web');
    expect(screen.getByTestId('pin-not-editable')).toBeInTheDocument();
    expect(screen.getAllByTestId('pin-include')[1]).toBeDisabled();
    expect(screen.getByTestId('pin-apply')).toHaveTextContent('Update 1 workflow');
  });

  it('says so when nothing needs to change and offers no campaign', async () => {
    (previewPin as Mock).mockResolvedValue(preview([]));

    open();

    expect(await screen.findByTestId('pin-nothing')).toBeInTheDocument();
    expect(screen.queryByTestId('pin-apply')).not.toBeInTheDocument();
  });

  it('lets a project be left out, and updates only the ones left in', async () => {
    (previewPin as Mock).mockResolvedValue(
      preview([project(), project({ project_id: 2, project_name: 'Platform' }), project({ project_id: 3, project_name: 'Web' })]),
    );
    (applyPin as Mock).mockResolvedValue({});
    const { user } = open();
    await screen.findByTestId('pin-apply');
    expect(screen.getByTestId('pin-apply')).toHaveTextContent('Update 3 workflows');

    await user.click(screen.getAllByTestId('pin-include')[1]);

    expect(screen.getByTestId('pin-apply')).toHaveTextContent('Update 2 workflows');
    expect(screen.getByTestId('pin-summary')).toHaveTextContent('2 projects · 2 workflows selected');
    await user.click(screen.getByTestId('pin-apply'));
    await waitFor(() => expect(applyPin).toHaveBeenCalledTimes(2));
    expect((applyPin as Mock).mock.calls.map(([request]) => request.project_id)).toEqual([1, 3]);
  });

  it('cannot update once every project is left out', async () => {
    (previewPin as Mock).mockResolvedValue(preview([project()]));
    const { user } = open();

    await user.click(await screen.findByTestId('pin-include'));

    expect(screen.getByTestId('pin-apply')).toBeDisabled();
    expect(screen.getByTestId('pin-apply')).toHaveTextContent('Update 0 workflows');
  });

  it('when there is no latest release to default to, says why and shows the field to type in', async () => {
    (previewPin as Mock)
      .mockRejectedValueOnce({ response: { data: { detail: 'octo/tool has no release or version tag to default to. Enter a tag, branch or commit SHA.' } } })
      .mockResolvedValueOnce(preview([project()], { ref: 'main', source: 'requested', comment: 'main' }));
    const { user } = open();

    expect(await screen.findByRole('alert')).toHaveTextContent('Enter a tag, branch or commit SHA');
    expect(screen.getByTestId('pin-ref')).toHaveValue('');
    expect(screen.getByTestId('pin-preview')).toBeDisabled();

    await user.type(screen.getByTestId('pin-ref'), 'main');
    await user.click(screen.getByTestId('pin-preview'));

    await screen.findByTestId('pin-project');
    expect(previewPin).toHaveBeenLastCalledWith('actions/checkout', 'main');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByTestId('pin-preview-result')).toHaveTextContent('Selected version');
  });

  it('Change reveals the field, typing another ref overrides the default, and Use latest release brings it back', async () => {
    (previewPin as Mock)
      .mockResolvedValueOnce(preview([project()]))
      .mockResolvedValueOnce(preview([project()], { ref: 'v4.1.0', source: 'requested', comment: 'v4.1.0' }))
      .mockResolvedValueOnce(preview([project()]));
    const { user } = open();
    await screen.findByTestId('pin-preview-result');

    await user.click(screen.getByTestId('pin-change'));
    expect(screen.getByTestId('pin-ref')).toHaveValue('v5.0.0');
    expect(screen.queryByTestId('pin-latest')).not.toBeInTheDocument();

    await user.clear(screen.getByTestId('pin-ref'));
    await user.type(screen.getByTestId('pin-ref'), 'v4.1.0');
    expect(screen.queryByTestId('pin-preview-result')).not.toBeInTheDocument();
    expect(screen.queryByTestId('pin-apply')).not.toBeInTheDocument();

    await user.click(screen.getByTestId('pin-preview'));
    await screen.findByTestId('pin-preview-result');
    expect(previewPin).toHaveBeenLastCalledWith('actions/checkout', 'v4.1.0');
    expect(screen.getByTestId('pin-preview-result')).toHaveTextContent('Selected version');

    await user.click(screen.getByTestId('pin-change'));
    await user.click(screen.getByTestId('pin-latest'));
    await waitFor(() => expect(screen.getByTestId('pin-preview-result')).toHaveTextContent('Latest release'));
    expect(previewPin).toHaveBeenLastCalledWith('actions/checkout', undefined);
  });

  it('updates one project after another, and a failure in one does not stop the rest', async () => {
    (previewPin as Mock).mockResolvedValue(
      preview([project(), project({ project_id: 2, project_name: 'Platform' }), project({ project_id: 3, project_name: 'Web' })]),
    );
    (applyPin as Mock)
      .mockResolvedValueOnce({})
      .mockRejectedValueOnce({ response: { data: { detail: 'GitHub API rate limit reached.' } } })
      .mockResolvedValueOnce({});
    const { user, onApplied } = open();

    await user.click(await screen.findByTestId('pin-apply'));

    await waitFor(() => expect(onApplied).toHaveBeenCalledTimes(1));
    expect((applyPin as Mock).mock.calls.map(([request]) => request.project_id)).toEqual([1, 2, 3]);
    expect(applyPin).toHaveBeenCalledWith({ project_id: 1, action: 'actions/checkout', sha: SHA, comment: 'v5.0.0' });
    const cards = screen.getAllByTestId('pin-project');
    expect(within(cards[0]).getByText('Updated')).toBeInTheDocument();
    expect(within(cards[1]).getByText('Failed')).toBeInTheDocument();
    expect(within(cards[1]).getByRole('alert')).toHaveTextContent('GitHub API rate limit reached.');
    expect(within(cards[2]).getByText('Updated')).toBeInTheDocument();
  });

  it('does not apply to a project the caller cannot change', async () => {
    (previewPin as Mock).mockResolvedValue(preview([project({ editable: false }), project({ project_id: 2, project_name: 'Mine' })]));
    (applyPin as Mock).mockResolvedValue({});
    const { user } = open();

    await user.click(await screen.findByTestId('pin-apply'));

    await waitFor(() => expect(applyPin).toHaveBeenCalledTimes(1));
    expect((applyPin as Mock).mock.calls[0][0].project_id).toBe(2);
  });

  it('does not count or update a workflow with an open pull request, and says why', async () => {
    const blocked = {
      workflow_id: 11, workflow_name: 'lint', workflow_file: 'AM_PAY_lint.yml', reusable: false,
      blocked: 'Under review: it has an open pull request. Close or merge that first.',
      lines: [{ line: 7, before: '      - uses: actions/checkout@v3', after: `      - uses: actions/checkout@${SHA}` }],
    };
    const base = project();
    (previewPin as Mock).mockResolvedValue(preview([{ ...base, workflows: [...base.workflows, blocked] }]));
    const { user } = open();

    await screen.findByTestId('pin-project');
    expect(screen.getByTestId('pin-summary')).toHaveTextContent('1 project · 1 workflow selected');
    expect(screen.getByTestId('pin-project')).toHaveTextContent('1 workflow');
    await user.click(screen.getByTestId('pin-show-changes'));
    expect(screen.getByTestId('pin-blocked')).toHaveTextContent('Under review: it has an open pull request.');
    expect(screen.getByTestId('pin-blocked')).toHaveTextContent('It is not updated.');
  });

  it('offers no checkbox for a project whose only changed workflows have open pull requests', async () => {
    const base = project();
    (previewPin as Mock).mockResolvedValue(preview([{ ...base, workflows: base.workflows.map((w) => ({ ...w, blocked: 'Under review: it has an open pull request.' })) }]));

    open();

    expect(await screen.findByTestId('pin-include')).toBeDisabled();
    expect(screen.getByTestId('pin-include')).not.toBeChecked();
    expect(screen.getByTestId('pin-apply')).toBeDisabled();
  });

  it('says nothing is opened: the copy promises no pull requests', async () => {
    (previewPin as Mock).mockResolvedValue(preview([project()]));

    open();

    expect(await screen.findByText(/No pull requests are opened/)).toBeInTheDocument();
    expect(screen.getByTestId('pin-apply')).toHaveTextContent(/^Update 1 workflow$/);
  });
});
