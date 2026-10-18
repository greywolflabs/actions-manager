import React, { useState } from 'react';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import '@testing-library/jest-dom';
import GUIWorkflowEditor from './GUIWorkflowEditor';
import type { WorkflowGUI } from '../utils/workflowGuiConversion';
import type { ActionsProject } from '../api/actionsProjects';
import type { ActionGroup } from '../api/actionGroups';

function makeWorkflow(overrides: Partial<WorkflowGUI> = {}): WorkflowGUI {
  return {
    name: 'CI',
    events: [{ type: 'push' }],
    jobs: [
      {
        id: 'build',
        name: 'Build',
        runsOn: 'ubuntu-latest',
        steps: [
          { id: 'step-1', name: 'Checkout' },
          { id: 'step-2', name: 'Setup Node' },
        ],
      },
    ],
    ...overrides,
  };
}

/**
 * Step ids are only unique within a job - `yamlToGui` mints `step-1`, `step-2`
 * per job - so every job in a real workflow has a `step-1`.
 */
function makeTwoJobWorkflow(): WorkflowGUI {
  return {
    name: 'CI',
    events: [{ type: 'push' }],
    jobs: [
      {
        id: 'build',
        name: 'Build',
        runsOn: 'ubuntu-latest',
        steps: [{ id: 'step-1', name: 'Compile', run: 'make' }],
      },
      {
        id: 'test',
        name: 'Test',
        runsOn: 'ubuntu-latest',
        steps: [{ id: 'step-1', name: 'Run tests', uses: 'actions/checkout@v4' }],
      },
    ],
  };
}

interface HarnessProps {
  initial: WorkflowGUI;
  onChange?: (w: WorkflowGUI) => void;
}

/** Wraps the editor in the controlled-parent contract it actually ships with. */
function Harness({ initial, onChange }: Readonly<HarnessProps>) {
  const [workflow, setWorkflow] = useState(initial);
  return (
    <GUIWorkflowEditor
      workflow={workflow}
      onChange={(w) => {
        setWorkflow(w);
        onChange?.(w);
      }}
      importedActions={[] as ActionsProject[]}
      actionGroups={[] as ActionGroup[]}
    />
  );
}

const panel = () => screen.getByRole('complementary');
const stepNameFields = () => screen.queryAllByLabelText('Step Name (optional)');
const selectStep = async (user: ReturnType<typeof userEvent.setup>, name: string) =>
  user.click(screen.getByRole('button', { name: new RegExp(name) }));

describe('GUIWorkflowEditor step detail panel', () => {
  it('renders the panel empty state when no step is selected', () => {
    render(<Harness initial={makeWorkflow()} />);

    expect(panel()).toHaveTextContent('Select a step to edit it.');
    expect(stepNameFields()).toHaveLength(0);
  });

  it('opens the selected step in the panel and marks its row current', async () => {
    const user = userEvent.setup();
    render(<Harness initial={makeWorkflow()} />);

    await selectStep(user, 'Checkout');

    expect(panel()).toHaveTextContent('Checkout');
    expect(screen.getByRole('button', { name: /Checkout/ })).toHaveAttribute('aria-current', 'true');
  });

  it('renders the step form exactly once when a step is selected', async () => {
    const user = userEvent.setup();
    render(<Harness initial={makeWorkflow()} />);

    await selectStep(user, 'Checkout');

    // Duplicate mounts would duplicate every `step-*-${step.id}` DOM id.
    expect(stepNameFields()).toHaveLength(1);
  });

  it('swaps the panel content when a different step is selected', async () => {
    const user = userEvent.setup();
    render(<Harness initial={makeWorkflow()} />);

    await selectStep(user, 'Checkout');
    await selectStep(user, 'Setup Node');

    expect(stepNameFields()).toHaveLength(1);
    expect(screen.getByLabelText('Step Name (optional)')).toHaveValue('Setup Node');
  });

  it('propagates panel edits to onChange and updates the row title without a refresh', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Harness initial={makeWorkflow()} onChange={onChange} />);

    await selectStep(user, 'Checkout');
    await user.clear(screen.getByLabelText('Step Name (optional)'));
    await user.type(screen.getByLabelText('Step Name (optional)'), 'Checkout repo');

    const last = onChange.mock.calls.at(-1)![0] as WorkflowGUI;
    expect(last.jobs[0].steps[0].name).toBe('Checkout repo');
    expect(screen.getByRole('button', { name: /Checkout repo/ })).toBeInTheDocument();
  });

  it('keeps focus while typing a multi-character step name in the panel', async () => {
    const user = userEvent.setup();
    render(<Harness initial={makeWorkflow()} />);

    await selectStep(user, 'Checkout');
    const field = screen.getByLabelText('Step Name (optional)');
    await user.clear(field);
    await user.type(field, 'abcde');

    expect(screen.getByLabelText('Step Name (optional)')).toHaveFocus();
    expect(screen.getByLabelText('Step Name (optional)')).toHaveValue('abcde');
  });

  it('keeps the same step selected after it is moved down', async () => {
    const user = userEvent.setup();
    render(<Harness initial={makeWorkflow()} />);

    await selectStep(user, 'Checkout');
    await user.click(screen.getAllByTitle('Move down')[0]);

    expect(panel()).toHaveTextContent('Checkout');
    expect(screen.getByLabelText('Step Name (optional)')).toHaveValue('Checkout');
  });

  it('falls back to the empty state when the selected step is deleted', async () => {
    const user = userEvent.setup();
    render(<Harness initial={makeWorkflow()} />);

    await selectStep(user, 'Checkout');
    await user.click(screen.getAllByTitle('Remove step')[0]);

    expect(panel()).toHaveTextContent('Select a step to edit it.');
  });

  it('keeps the selection when a different step is deleted', async () => {
    const user = userEvent.setup();
    render(<Harness initial={makeWorkflow()} />);

    await selectStep(user, 'Setup Node');
    await user.click(screen.getAllByTitle('Remove step')[0]);

    expect(panel()).toHaveTextContent('Setup Node');
  });

  it('closes the panel via the close button and restores focus to the row', async () => {
    const user = userEvent.setup();
    render(<Harness initial={makeWorkflow()} />);

    await selectStep(user, 'Checkout');
    await user.click(screen.getByRole('button', { name: 'Close step details' }));

    expect(panel()).toHaveTextContent('Select a step to edit it.');
    expect(screen.getByRole('button', { name: /Checkout/ })).toHaveFocus();
  });

  it('closes the panel on Escape', async () => {
    const user = userEvent.setup();
    render(<Harness initial={makeWorkflow()} />);

    await selectStep(user, 'Checkout');
    await user.keyboard('{Escape}');

    expect(panel()).toHaveTextContent('Select a step to edit it.');
  });

  it('opens a newly added step in the panel with no further clicks', async () => {
    const user = userEvent.setup();
    render(<Harness initial={makeWorkflow()} />);

    await user.click(screen.getByRole('button', { name: /Add Step/i }));

    expect(screen.getByLabelText('Step Name (optional)')).toHaveValue('Step 3');
    expect(screen.getByRole('button', { name: /Step 3/ })).toHaveAttribute('aria-current', 'true');
  });

  it('gives an added step a unique id after a middle step was deleted', async () => {
    const user = userEvent.setup();
    render(<Harness initial={makeWorkflow()} />);

    // Deleting step 1 of 2 leaves "step-2", so the naive `step-${len+1}`
    // would mint "step-2" again and the panel could open the wrong row.
    await user.click(screen.getAllByTitle('Remove step')[0]);
    await user.click(screen.getByRole('button', { name: /Add Step/i }));

    expect(screen.getAllByLabelText('Step Name (optional)')).toHaveLength(1);
    expect(screen.getByLabelText('Step Name (optional)')).toHaveValue('Step 2');
  });

  it('selects the copy when a step is duplicated', async () => {
    const user = userEvent.setup();
    render(<Harness initial={makeWorkflow()} />);

    await user.click(screen.getAllByTitle('Duplicate step')[0]);

    expect(screen.getByLabelText('Step Name (optional)')).toHaveValue('Checkout (Copy)');
  });

  it('gives each duplicate of the same step a distinct id', async () => {
    const user = userEvent.setup();
    render(<Harness initial={makeWorkflow()} />);

    await user.click(screen.getAllByTitle('Duplicate step')[0]);
    await user.click(screen.getAllByTitle('Duplicate step')[0]);

    // Two copy rows exist and only the latest is open - a colliding id would
    // mount the form twice or resolve to the wrong step.
    expect(screen.getAllByRole('button', { name: /Checkout \(Copy\)/ })).toHaveLength(2);
    expect(screen.getAllByLabelText('Step Name (optional)')).toHaveLength(1);
  });

  // GitHub rejects two steps with the same id in one job, and references to
  // `steps.<id>` should keep pointing at the original.
  it('does not copy the YAML id when a step is duplicated', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const withId = makeWorkflow({
      jobs: [{
        id: 'build',
        name: 'Build',
        runsOn: 'ubuntu-latest',
        steps: [{ id: 'meta', yamlId: 'meta', name: 'Read version', run: 'make' }],
      }],
    });
    render(<Harness initial={withId} onChange={onChange} />);

    await user.click(screen.getAllByTitle('Duplicate step')[0]);

    const [original, copy] = (onChange.mock.calls.at(-1)![0] as WorkflowGUI).jobs[0].steps;
    expect(original.yamlId).toBe('meta');
    expect(copy.yamlId).toBeUndefined();
  });

  // Step ids are scoped to their job, so the copied job's own references still resolve.
  it('keeps step YAML ids in a duplicated job', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const withId = makeWorkflow({
      jobs: [{
        id: 'build',
        name: 'Build',
        runsOn: 'ubuntu-latest',
        steps: [{ id: 'meta', yamlId: 'meta', name: 'Read version', run: 'make' }],
      }],
    });
    render(<Harness initial={withId} onChange={onChange} />);

    await user.click(screen.getAllByTitle('Duplicate job')[0]);

    const jobs = (onChange.mock.calls.at(-1)![0] as WorkflowGUI).jobs;
    expect(jobs.map(j => j.steps[0].yamlId)).toEqual(['meta', 'meta']);
  });

  // GitHub allows no runs-on, steps, env or timeout-minutes on a job that
  // calls a reusable workflow, so the GUI offers none of them there.
  it('shows a caller job the workflow it calls instead of a runner and steps', async () => {
    const user = userEvent.setup();
    const withCaller = makeWorkflow({
      jobs: [
        { id: 'build', name: 'Build', runsOn: 'ubuntu-latest', steps: [{ id: 'step-1', name: 'Checkout' }] },
        {
          id: 'deploy',
          name: 'Deploy',
          runsOn: 'ubuntu-latest',
          steps: [],
          uses: 'org/repo/.github/workflows/deploy.yml@main',
          unsupportedFields: { secrets: 'inherit' },
        },
      ],
    });
    render(<Harness initial={withCaller} />);

    for (const toggle of screen.getAllByRole('button', { name: /Advanced Settings/ })) {
      await user.click(toggle);
    }

    expect(screen.getByText('org/repo/.github/workflows/deploy.yml@main')).toBeInTheDocument();
    expect(screen.getAllByLabelText(/^Runner/)).toHaveLength(1);
    expect(screen.getAllByLabelText('Timeout (minutes)')).toHaveLength(1);
    expect(screen.getAllByText('Job Environment Variables')).toHaveLength(1);
    expect(screen.getAllByRole('button', { name: /Add Step/ })).toHaveLength(1);
    expect(screen.getAllByLabelText('If Condition (optional)')).toHaveLength(2);
  });

  // `needs: build` round-trips as a string; treating it as an array matched
  // `build-docker` by substring and spread the string into characters.
  it('edits a single needs: string as one dependency', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const job = (id: string, needs?: string) => ({ id, runsOn: 'ubuntu-latest', steps: [{ id: 'step-1', run: 'make' }], needs });
    render(
      <Harness initial={makeWorkflow({ jobs: [job('build'), job('build-docker'), job('test', 'build')] })} onChange={onChange} />
    );
    const needs = within(document.getElementById('job-needs-test')!);
    const lastNeeds = () => (onChange.mock.calls.at(-1)![0] as WorkflowGUI).jobs[2].needs;

    expect(needs.getByRole('checkbox', { name: 'build' })).toBeChecked();
    expect(needs.getByRole('checkbox', { name: 'build-docker' })).not.toBeChecked();

    await user.click(needs.getByRole('checkbox', { name: 'build-docker' }));
    expect(lastNeeds()).toEqual(['build', 'build-docker']);

    await user.click(needs.getByRole('checkbox', { name: 'build' }));
    expect(lastNeeds()).toEqual(['build-docker']);
  });

  it('remounts the form when switching between same-id steps in different jobs', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Harness initial={makeTwoJobWorkflow()} onChange={onChange} />);

    await user.click(screen.getByRole('button', { name: /Compile/ }));
    expect(screen.getByRole('radio', { name: /Run Script/ })).toBeChecked();

    // Both steps are `step-1`; keying the form on the step id alone would
    // reuse the mounted instance and leave stepType stuck on "run", so
    // editing the second step would emit both `uses:` and `run:`.
    await user.click(screen.getByRole('button', { name: /Run tests/ }));
    expect(screen.getByRole('radio', { name: /Use Action/ })).toBeChecked();
  });

  it('re-focuses the panel heading when switching between same-id steps', async () => {
    const user = userEvent.setup();
    render(<Harness initial={makeTwoJobWorkflow()} />);

    await user.click(screen.getByRole('button', { name: /Compile/ }));
    await user.click(screen.getByRole('button', { name: /Run tests/ }));

    expect(screen.getByRole('heading', { name: 'Run tests' })).toHaveFocus();
  });

  it('does not scroll the page when a step is clicked', async () => {
    // focus() scrolls its target into view unless told not to, and the panel is
    // its own scroll container - that combination made the clicked row slide
    // out from under the cursor. The focus move must still happen (see the
    // test above), so what's asserted here is the opt-out, not the absence of
    // the focus call.
    const focusSpy = vi.spyOn(HTMLHeadingElement.prototype, 'focus');
    const user = userEvent.setup();
    render(<Harness initial={makeWorkflow()} />);
    focusSpy.mockClear();

    await selectStep(user, 'Checkout');

    expect(focusSpy).toHaveBeenCalledWith({ preventScroll: true });
    expect(focusSpy).not.toHaveBeenCalledWith();
    focusSpy.mockRestore();
  });

  it('never commits a scroll for an already-visible panel', async () => {
    // `nearest` is what keeps the desktop (sticky, in-view) case a no-op while
    // still revealing the panel below lg, where it stacks under the job list.
    // A committed alignment like 'start'/'center' would scroll unconditionally.
    const scrollSpy = vi.spyOn(Element.prototype, 'scrollIntoView');
    const user = userEvent.setup();
    render(<Harness initial={makeWorkflow()} />);
    scrollSpy.mockClear();

    await selectStep(user, 'Checkout');

    for (const [options] of scrollSpy.mock.calls) {
      expect(options).toMatchObject({ block: 'nearest' });
    }
    scrollSpy.mockRestore();
  });

  it('gives each job row a distinct DOM id for its steps', async () => {
    const user = userEvent.setup();
    render(<Harness initial={makeTwoJobWorkflow()} />);

    await user.click(screen.getByRole('button', { name: /Run tests/ }));
    const rowId = screen.getByRole('button', { name: /Run tests/ }).id;

    // Duplicate ids would make close()'s getElementById restore focus to the
    // wrong job's row, and would be invalid HTML besides.
    expect(document.querySelectorAll(`[id="${rowId}"]`)).toHaveLength(1);
  });

  it('shows only the selected step\'s validation errors, not those of steps 10+', async () => {
    const user = userEvent.setup();
    const manySteps: WorkflowGUI = {
      name: 'CI',
      events: [{ type: 'push' }],
      jobs: [{
        id: 'build',
        name: 'Build',
        runsOn: 'ubuntu-latest',
        steps: Array.from({ length: 12 }, (_, i) => ({ id: `step-${i + 1}`, name: `Step ${i + 1}` })),
      }],
    };
    render(<Harness initial={manySteps} />);

    // steps[1] is a string prefix of steps[10] and steps[11].
    await user.click(screen.getByRole('button', { name: /Step 2$/ }));

    expect(panel()).not.toHaveTextContent('Step 11');
    expect(panel()).not.toHaveTextContent('Step 12');
  });

  it('gives each duplicate of the same job a distinct id', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Harness initial={makeTwoJobWorkflow()} onChange={onChange} />);

    await user.click(screen.getAllByTitle('Duplicate job')[0]);
    await user.click(screen.getAllByTitle('Duplicate job')[0]);

    const jobIds = (onChange.mock.calls.at(-1)![0] as WorkflowGUI).jobs.map(j => j.id);
    // Colliding job ids would make findSelectedStep resolve a click on the
    // third job's step to the second job's step.
    expect(new Set(jobIds).size).toBe(jobIds.length);
  });

  it('gives a new job a fresh id after a middle job is removed', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const threeJobs: WorkflowGUI = {
      name: 'CI',
      events: [{ type: 'push' }],
      jobs: [
        { id: 'job-1', name: 'Job 1', runsOn: 'ubuntu-latest', steps: [] },
        { id: 'job-2', name: 'Job 2', runsOn: 'ubuntu-latest', steps: [] },
        { id: 'job-3', name: 'Job 3', runsOn: 'ubuntu-latest', steps: [] },
      ],
    };
    render(<Harness initial={threeJobs} onChange={onChange} />);

    // Removing job-2 leaves [job-1, job-3]; naively minting off jobs.length
    // (now 2) for the next add would produce 'job-3' again.
    await user.click(screen.getAllByTitle('Remove job')[1]);
    await user.click(screen.getByRole('button', { name: /Add Job/i }));

    const jobIds = (onChange.mock.calls.at(-1)![0] as WorkflowGUI).jobs.map(j => j.id);
    expect(new Set(jobIds).size).toBe(jobIds.length);
  });

  it('moves focus to the panel heading on selection', async () => {
    const user = userEvent.setup();
    render(<Harness initial={makeWorkflow()} />);

    await selectStep(user, 'Checkout');

    expect(screen.getByRole('heading', { name: 'Checkout' })).toHaveFocus();
  });
});

/**
 * Coverage for the reusable variant, which had none: it used to live in a
 * separate 277-line copy of this component with no test file of its own.
 */
describe('GUIWorkflowEditor variants', () => {
  const renderVariant = (variant?: 'regular' | 'reusable') =>
    render(
      <GUIWorkflowEditor
        variant={variant}
        workflow={makeWorkflow()}
        onChange={() => {}}
        importedActions={[] as ActionsProject[]}
        actionGroups={[] as ActionGroup[]}
      />
    );

  it('defaults to the regular variant', () => {
    renderVariant();
    expect(screen.getByText('Workflow Name (optional)')).toBeInTheDocument();
    expect(screen.getByPlaceholderText('Enter workflow name...')).toBeInTheDocument();
    expect(screen.getByText('Trigger Events *')).toBeInTheDocument();
  });

  it('shows no error for a workflow without a name', () => {
    render(
      <GUIWorkflowEditor
        workflow={makeWorkflow({ name: '' })}
        onChange={() => {}}
        importedActions={[] as ActionsProject[]}
        actionGroups={[] as ActionGroup[]}
      />
    );

    expect(screen.queryByText('Workflow name is required')).not.toBeInTheDocument();
    expect(screen.getByPlaceholderText('Enter workflow name...')).not.toHaveClass('error');
  });

  it('labels the reusable variant for reusable workflows', () => {
    renderVariant('reusable');
    expect(screen.getByText('Reusable Workflow Name (optional)')).toBeInTheDocument();
    expect(screen.getByPlaceholderText('Enter reusable workflow name...')).toBeInTheDocument();
    // Singular: a reusable workflow only ever triggers on workflow_call.
    expect(screen.getByText('Trigger Event')).toBeInTheDocument();
    expect(screen.queryByText('Trigger Events *')).not.toBeInTheDocument();
  });

  it('renders the same job and step editing surface for both variants', () => {
    const { unmount } = renderVariant('regular');
    expect(screen.getByRole('button', { name: /Checkout/ })).toBeInTheDocument();
    unmount();

    renderVariant('reusable');
    expect(screen.getByRole('button', { name: /Checkout/ })).toBeInTheDocument();
  });
});
