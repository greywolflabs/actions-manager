import React, { useState } from 'react';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import '@testing-library/jest-dom';
import UnifiedWorkflows from './UnifiedWorkflows';
import { toast } from '../utils/toast';
import { guiToYaml } from '../utils/workflowGuiConversion';
import * as yaml from 'js-yaml';

// The list only needs to let a test select a workflow, and to show the
// Managed Files link it was handed.
vi.mock('./UnifiedWorkflowList', () => ({
  default: function UnifiedWorkflowList({
    unifiedWorkflows,
    handleSelectWorkflow,
    selectedWorkflowId,
    onOpenManagedFiles,
  }: any) {
    return (
      <div>
        {onOpenManagedFiles && (
          <button type="button" data-testid="open-managed-files" onClick={onOpenManagedFiles}>
            Managed Files
          </button>
        )}
        {unifiedWorkflows.map((w: any) => (
          <button
            type="button"
            key={w.id}
            data-testid={`select-${w.id}`}
            aria-pressed={w.id === selectedWorkflowId}
            onClick={() => handleSelectWorkflow(w.id)}
          >
            {w.name}
          </button>
        ))}
      </div>
    );
  },
}));

// Surfaces exactly what the editor was handed, so the test can assert on the
// model the GUI would render and on the mode it is actually in.
vi.mock('./UnifiedWorkflowEditor', () => ({
  default: function UnifiedWorkflowEditor({
    editMode,
    regularGuiWorkflow,
    handleWorkflowChange,
    setEditMode,
  }: any) {
    return (
      <div>
        <span data-testid="edit-mode">{editMode}</span>
        <span data-testid="gui-model">{JSON.stringify(regularGuiWorkflow)}</span>
        <button data-testid="switch-to-gui" onClick={() => setEditMode('gui')}>
          GUI
        </button>
        <button data-testid="switch-to-yaml" onClick={() => setEditMode('yaml')}>
          YAML
        </button>
        <button
          data-testid="edit-yaml"
          onClick={() =>
            handleWorkflowChange(
              'content',
              'name: Edited\non: push\njobs:\n  edited-job:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo edited\n'
            )
          }
        >
          Edit YAML
        </button>
        <button
          data-testid="break-yaml"
          onClick={() => handleWorkflowChange('content', 'name: [unclosed\non: push')}
        >
          Break YAML
        </button>
        {/* Mirrors the real editor's handleGUIChange: any GUI edit serialises the
            loaded model straight back over the document. */}
        <button
          data-testid="edit-in-gui"
          onClick={() => handleWorkflowChange('content', guiToYaml(regularGuiWorkflow))}
        >
          Edit in GUI
        </button>
        <button
          type="button"
          data-testid="edit-step-in-gui"
          onClick={() => {
            const [job, ...otherJobs] = regularGuiWorkflow.jobs;
            const [step, ...otherSteps] = job.steps;
            const edited = { ...job, steps: [{ ...step, run: 'echo changed' }, ...otherSteps] };
            handleWorkflowChange('content', guiToYaml({ ...regularGuiWorkflow, jobs: [edited, ...otherJobs] }));
          }}
        >
          Edit a step in GUI
        </button>
        <button type="button" data-testid="rename-file" onClick={() => handleWorkflowChange('name', 'renamed')}>
          Rename file
        </button>
      </div>
    );
  },
}));

vi.mock('./WorkflowCreationDialog', () => ({ default: () => null }));
vi.mock('./TemplateSelectionModal', () => ({ default: () => null }));
vi.mock('./CustomFiles', () => ({ CustomFilePanel: () => null }));

vi.mock('../utils/toast', () => ({
  toast: { error: vi.fn(), success: vi.fn(), info: vi.fn(), warning: vi.fn() },
}));

// Captured so a test can hand the component a save's kind changes the way the
// editor's draft save does.
let capturedOperationsProps: any = null;

vi.mock('../hooks/useWorkflowOperations', () => ({
  useWorkflowOperations: (props: any) => {
    capturedOperationsProps = props;
    return {
    fetchWorkflowsCount: vi.fn(),
    openWorkflowCreationDialog: vi.fn(),
    selectWorkflowType: vi.fn(),
    handleGenerateTemplates: vi.fn(),
    handleSaveDraftWorkflow: vi.fn(),
    handleSaveDraftLinkedWorkflow: vi.fn(),
    handleCommitAndUpdatePR: vi.fn(),
    handleCommitAndUpdatePRLinked: vi.fn(),
    handleDeleteWorkflow: vi.fn(),
    handleUnlinkWorkflow: vi.fn(),
    };
  },
}));

const ORIGINAL_YAML =
  'name: Original\non: push\njobs:\n  original-job:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo original\n';

function renderSubject(overrides: Record<string, unknown> = {}) {
  const setWorkflows = vi.fn();
  const props: any = {
    user: 'test-user',
    projectName: 'test-project',
    projectCode: 'TEST',
    selectedRepos: ['acme/web'],
    regexPattern: '',
    workflows: [{ name: 'build', content: ORIGINAL_YAML }],
    setWorkflows,
    rxworkflows: [],
    setRXWorkflows: vi.fn(),
    addWorkflowToMain: vi.fn(),
    onGenerateTemplates: vi.fn(),
    onAddRXWorkflow: vi.fn(),
    detectedBuildTypes: [],
    reusableWorkflowsEnabled: false,
    ...overrides,
  };

  const view = render(<UnifiedWorkflows {...props} />);
  return { ...view, setWorkflows, props };
}

const guiModel = () => JSON.parse(screen.getByTestId('gui-model').textContent || '{}');

// The parent owns workflow state; apply the last write the way ProjectMgmt does.
const lastWrite = (setWorkflows: ReturnType<typeof vi.fn>, previous: unknown[]) => {
  const update = setWorkflows.mock.calls.at(-1)?.[0];
  return typeof update === 'function' ? update(previous) : update;
};

const LABELLED_YAML =
  'name: Build and Test\non: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo original\n';

// The GUI model stores branch filters as a list, so a GUI edit would rewrite
// `branches: main` as one.
const LOSSY_YAML =
  'name: CI\non:\n  push:\n    branches: main\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: make\n';
const LOSSY_REFUSAL =
  "GUI mode can't edit this workflow without changing it (on.push.branches). Keep editing in YAML.";
const BROKEN_YAML = 'name: [unclosed\non: push';

describe('UnifiedWorkflows YAML/GUI mode switching', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  test('rebuilds the GUI model from YAML edited since the workflow was selected', async () => {
    const user = userEvent.setup();
    const { rerender, setWorkflows, props } = renderSubject();

    await user.click(screen.getByTestId('select-regular-0'));
    expect(guiModel().jobs[0].id).toBe('original-job');

    // Edit in YAML mode; the parent owns workflow state, so feed the update back
    // in the way ProjectMgmt does.
    await user.click(screen.getByTestId('edit-yaml'));
    const updated = setWorkflows.mock.calls.at(-1)?.[0];
    const nextWorkflows = typeof updated === 'function' ? updated(props.workflows) : updated;
    rerender(<UnifiedWorkflows {...props} workflows={nextWorkflows} />);

    await user.click(screen.getByTestId('switch-to-gui'));

    await waitFor(() => expect(screen.getByTestId('edit-mode')).toHaveTextContent('gui'));
    expect(guiModel().jobs[0].id).toBe('edited-job');
    expect(guiModel().jobs[0].steps[0].run).toBe('echo edited');
  });

  test('refuses to enter GUI mode when the YAML no longer parses', async () => {
    const user = userEvent.setup();
    const { rerender, setWorkflows, props } = renderSubject();

    await user.click(screen.getByTestId('select-regular-0'));
    await user.click(screen.getByTestId('break-yaml'));
    const updated = setWorkflows.mock.calls.at(-1)?.[0];
    const nextWorkflows = typeof updated === 'function' ? updated(props.workflows) : updated;
    rerender(<UnifiedWorkflows {...props} workflows={nextWorkflows} />);

    setWorkflows.mockClear();
    await user.click(screen.getByTestId('switch-to-gui'));

    expect(screen.getByTestId('edit-mode')).toHaveTextContent('yaml');
    expect(toast.error).toHaveBeenCalledWith('Fix the YAML errors before switching to GUI mode.');
  });

  // The bug's real cost: the GUI model is written back over the document on the
  // first GUI edit, so a stale model silently reverts the YAML edits.
  test('a GUI edit after switching does not revert the YAML edit', async () => {
    const user = userEvent.setup();
    const { rerender, setWorkflows, props } = renderSubject();

    await user.click(screen.getByTestId('select-regular-0'));
    await user.click(screen.getByTestId('edit-yaml'));
    const edited = setWorkflows.mock.calls.at(-1)?.[0];
    const editedWorkflows = typeof edited === 'function' ? edited(props.workflows) : edited;
    rerender(<UnifiedWorkflows {...props} workflows={editedWorkflows} />);

    await user.click(screen.getByTestId('switch-to-gui'));
    await waitFor(() => expect(screen.getByTestId('edit-mode')).toHaveTextContent('gui'));

    setWorkflows.mockClear();
    await user.click(screen.getByTestId('edit-in-gui'));

    const written = setWorkflows.mock.calls.at(-1)?.[0];
    const writtenWorkflows = typeof written === 'function' ? written(editedWorkflows) : written;
    expect(writtenWorkflows[0].content).toContain('edited-job');
    expect(writtenWorkflows[0].content).not.toContain('original-job');
  });

  test('a refused switch does not write anything back over the document', async () => {
    const user = userEvent.setup();
    const { rerender, setWorkflows, props } = renderSubject();

    await user.click(screen.getByTestId('select-regular-0'));
    await user.click(screen.getByTestId('break-yaml'));
    const updated = setWorkflows.mock.calls.at(-1)?.[0];
    const nextWorkflows = typeof updated === 'function' ? updated(props.workflows) : updated;
    rerender(<UnifiedWorkflows {...props} workflows={nextWorkflows} />);

    setWorkflows.mockClear();
    await user.click(screen.getByTestId('switch-to-gui'));

    expect(setWorkflows).not.toHaveBeenCalled();
  });

  test('a GUI step edit keeps a name: label that differs from the filename', async () => {
    const user = userEvent.setup();
    const { setWorkflows, props } = renderSubject({ workflows: [{ name: 'build', content: LABELLED_YAML }] });

    await user.click(screen.getByTestId('select-regular-0'));
    await user.click(screen.getByTestId('switch-to-gui'));
    await waitFor(() => expect(screen.getByTestId('edit-mode')).toHaveTextContent('gui'));

    setWorkflows.mockClear();
    await user.click(screen.getByTestId('edit-step-in-gui'));

    const written = yaml.load(lastWrite(setWorkflows, props.workflows)[0].content) as any;
    expect(written.name).toBe('Build and Test');
    expect(written.jobs.build.steps[0].run).toBe('echo changed');
  });

  test('renaming the file does not rewrite the name: label', async () => {
    const user = userEvent.setup();
    const { rerender, setWorkflows, props } = renderSubject({
      workflows: [{ name: 'build', content: LABELLED_YAML }],
    });

    await user.click(screen.getByTestId('select-regular-0'));
    await user.click(screen.getByTestId('rename-file'));
    const renamed = lastWrite(setWorkflows, props.workflows);
    rerender(<UnifiedWorkflows {...props} workflows={renamed} />);

    await user.click(screen.getByTestId('switch-to-gui'));
    await waitFor(() => expect(screen.getByTestId('edit-mode')).toHaveTextContent('gui'));
    expect(guiModel().name).toBe('Build and Test');

    await user.click(screen.getByTestId('edit-step-in-gui'));
    expect((yaml.load(lastWrite(setWorkflows, renamed)[0].content) as any).name).toBe('Build and Test');
  });

  test('refuses GUI mode for a workflow it cannot edit without changing it', async () => {
    const user = userEvent.setup();
    const { setWorkflows } = renderSubject({ workflows: [{ name: 'scalar', content: LOSSY_YAML }] });

    await user.click(screen.getByTestId('select-regular-0'));
    await user.click(screen.getByTestId('switch-to-gui'));

    expect(screen.getByTestId('edit-mode')).toHaveTextContent('yaml');
    expect(toast.error).toHaveBeenCalledWith(LOSSY_REFUSAL);
    expect(setWorkflows).not.toHaveBeenCalled();
  });

  test('names at most three paths in the refusal', async () => {
    const user = userEvent.setup();
    const content =
      'name: CI\non:\n  push:\n    branches: main\n    paths: src\n    tags: v1\n' +
      '  pull_request:\n    branches: main\n    paths: src\n' +
      'jobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: make\n';
    renderSubject({ workflows: [{ name: 'scalars', content }] });

    await user.click(screen.getByTestId('select-regular-0'));
    await user.click(screen.getByTestId('switch-to-gui'));

    expect(toast.error).toHaveBeenCalledWith(
      "GUI mode can't edit this workflow without changing it " +
        '(on.push.branches, on.push.paths, on.push.tags +2 more). Keep editing in YAML.'
    );
  });

  test('enters GUI mode for a workflow it can edit without changing it', async () => {
    const user = userEvent.setup();
    renderSubject();

    await user.click(screen.getByTestId('select-regular-0'));
    await user.click(screen.getByTestId('switch-to-gui'));

    await waitFor(() => expect(screen.getByTestId('edit-mode')).toHaveTextContent('gui'));
    expect(toast.error).not.toHaveBeenCalled();
  });

  test('in GUI mode, selecting a workflow it cannot edit drops to YAML', async () => {
    const user = userEvent.setup();
    const { setWorkflows } = renderSubject({
      workflows: [
        { name: 'build', content: ORIGINAL_YAML },
        { name: 'scalar', content: LOSSY_YAML },
      ],
    });

    await user.click(screen.getByTestId('select-regular-0'));
    await user.click(screen.getByTestId('switch-to-gui'));
    await waitFor(() => expect(screen.getByTestId('edit-mode')).toHaveTextContent('gui'));

    await user.click(screen.getByTestId('select-regular-1'));

    expect(screen.getByTestId('edit-mode')).toHaveTextContent('yaml');
    expect(toast.error).toHaveBeenCalledTimes(1);
    expect(toast.error).toHaveBeenCalledWith(LOSSY_REFUSAL);
    expect(setWorkflows).not.toHaveBeenCalled();
  });

  test('in GUI mode, selecting a workflow whose YAML does not parse drops to YAML', async () => {
    const user = userEvent.setup();
    const { setWorkflows } = renderSubject({
      workflows: [
        { name: 'build', content: ORIGINAL_YAML },
        { name: 'broken', content: BROKEN_YAML },
      ],
    });

    await user.click(screen.getByTestId('select-regular-0'));
    await user.click(screen.getByTestId('switch-to-gui'));
    await waitFor(() => expect(screen.getByTestId('edit-mode')).toHaveTextContent('gui'));

    await user.click(screen.getByTestId('select-regular-1'));

    expect(screen.getByTestId('edit-mode')).toHaveTextContent('yaml');
    expect(toast.error).toHaveBeenCalledWith('Fix the YAML errors before switching to GUI mode.');
    expect(setWorkflows).not.toHaveBeenCalled();
  });

  test('in GUI mode, selecting a linked workflow is left alone', async () => {
    const user = userEvent.setup();
    renderSubject({
      linkedWorkflows: [{ workflow_id: 7, workflow_name: 'shared', workflow_yaml: LOSSY_YAML }],
    });

    await user.click(screen.getByTestId('select-regular-0'));
    await user.click(screen.getByTestId('switch-to-gui'));
    await waitFor(() => expect(screen.getByTestId('edit-mode')).toHaveTextContent('gui'));

    await user.click(screen.getByTestId('select-linked-7'));

    expect(screen.getByTestId('edit-mode')).toHaveTextContent('gui');
    expect(toast.error).not.toHaveBeenCalled();
  });

  // Clicking GUI while already in GUI mode reloads the model from the current
  // YAML, which may have changed (a restored version) to something it can't edit.
  test('in GUI mode, clicking GUI for YAML it cannot edit drops to YAML', async () => {
    const user = userEvent.setup();
    const { rerender, setWorkflows, props } = renderSubject();

    await user.click(screen.getByTestId('select-regular-0'));
    await user.click(screen.getByTestId('switch-to-gui'));
    await waitFor(() => expect(screen.getByTestId('edit-mode')).toHaveTextContent('gui'));

    rerender(<UnifiedWorkflows {...props} workflows={[{ name: 'build', content: LOSSY_YAML }]} />);
    await user.click(screen.getByTestId('switch-to-gui'));

    expect(screen.getByTestId('edit-mode')).toHaveTextContent('yaml');
    expect(toast.error).toHaveBeenCalledWith(LOSSY_REFUSAL);
    expect(setWorkflows).not.toHaveBeenCalled();
  });

  test('switching back to YAML is never blocked', async () => {
    const user = userEvent.setup();
    renderSubject();

    await user.click(screen.getByTestId('select-regular-0'));
    await user.click(screen.getByTestId('switch-to-gui'));
    await waitFor(() => expect(screen.getByTestId('edit-mode')).toHaveTextContent('gui'));

    await user.click(screen.getByTestId('switch-to-yaml'));

    expect(screen.getByTestId('edit-mode')).toHaveTextContent('yaml');
    expect(toast.error).not.toHaveBeenCalled();
  });
});


describe('reusable workflows owned by a caller project', () => {
  const REUSABLE_YAML = 'name: Shared\non:\n  workflow_call:\njobs:\n  build:\n    runs-on: ubuntu-latest\n';

  beforeEach(() => {
    vi.clearAllMocks();
  });

  // Regression: a reusable workflow imported into a caller project was written
  // to the database and then dropped from the rendered list, so the import
  // looked like it had done nothing and could not be retried.
  test('lists a reusable workflow even when reusable authoring is off', () => {
    renderSubject({
      rxworkflows: [{ name: 'shared-workflow', content: REUSABLE_YAML }],
      reusableWorkflowsEnabled: false,
      projectType: 'standard',
    });

    expect(screen.getByTestId('select-reusable-0')).toBeInTheDocument();
    expect(screen.getByText('shared-workflow')).toBeInTheDocument();
  });

  test('still lists it for a Reusable Workflow Project', () => {
    renderSubject({
      rxworkflows: [{ name: 'shared-workflow', content: REUSABLE_YAML }],
      reusableWorkflowsEnabled: true,
      projectType: 'rwx',
    });

    expect(screen.getByTestId('select-reusable-0')).toBeInTheDocument();
  });
});

describe('a save that changes a workflow\'s kind (#2318)', () => {
  const CHANGE = { name: 'deploy', is_reusable: true, workflow_status: 'committed_locally' };

  // ProjectMgmt owns the lists; this holds them in real state so a move has
  // to re-render to be seen, the way it must in the app. "parent-move" does
  // what ProjectMgmt does after a project save: follower first, then the move.
  function Harness({ onRegister }: Readonly<{ onRegister?: (fn: ((changes: any[]) => void) | null) => void }>) {
    const [workflows, setWorkflows] = useState<any[]>([
      { name: 'build', content: ORIGINAL_YAML },
      { name: 'deploy', content: 'on: workflow_call' },
    ]);
    const [rxworkflows, setRxworkflows] = useState<any[]>([]);
    const [follower, setFollower] = useState<((changes: any[]) => void) | null>(null);
    const register = (fn: ((changes: any[]) => void) | null) => {
      setFollower(() => fn);
      onRegister?.(fn);
    };
    return (
      <>
        <button
          type="button"
          data-testid="parent-move"
          onClick={() => {
            follower?.([CHANGE]);
            setWorkflows(prev => prev.filter(w => w.name !== 'deploy'));
            setRxworkflows(prev => [...prev, { ...workflows[1], isReusable: true }]);
          }}
        />
        <UnifiedWorkflows
          {...({
            user: 'test-user', projectName: 'test-project', projectCode: 'TEST',
            selectedRepos: ['acme/web'], regexPattern: '', detectedBuildTypes: [],
            reusableWorkflowsEnabled: false, addWorkflowToMain: vi.fn(),
            onGenerateTemplates: vi.fn(), onAddRXWorkflow: vi.fn(),
            workflows, setWorkflows, rxworkflows, setRXWorkflows: setRxworkflows,
            onRegisterSelectionFollower: register,
          } as any)}
        />
      </>
    );
  }

  const expectDeployMovedAndSelected = () => {
    expect(screen.queryByTestId('select-regular-1')).not.toBeInTheDocument();
    expect(screen.getByTestId('select-reusable-0')).toHaveTextContent('deploy');
    expect(screen.getByTestId('select-reusable-0')).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByTestId('select-regular-0')).toHaveTextContent('build');
  };

  beforeEach(() => {
    vi.clearAllMocks();
  });

  test('the editor\'s save moves it to the other section without a reload, still selected', async () => {
    const user = userEvent.setup();
    render(<Harness />);

    await user.click(screen.getByTestId('select-regular-1'));
    act(() => capturedOperationsProps.applyKindChanges([CHANGE]));

    expectDeployMovedAndSelected();
  });

  test('when the project save moves it, the selection follows', async () => {
    const user = userEvent.setup();
    render(<Harness />);

    await user.click(screen.getByTestId('select-regular-1'));
    await user.click(screen.getByTestId('parent-move'));

    expectDeployMovedAndSelected();
  });

  test('unmounting withdraws the follower, so the parent never calls a stale one', () => {
    const onRegister = vi.fn();
    const { unmount } = render(<Harness onRegister={onRegister} />);
    expect(onRegister).toHaveBeenLastCalledWith(expect.any(Function));

    unmount();

    expect(onRegister).toHaveBeenLastCalledWith(null);
  });
});

describe('UnifiedWorkflows – where CODEOWNERS went (#2367)', () => {
  test('hands the Managed Files link to the Project Files list', async () => {
    const onOpenManagedFiles = vi.fn();
    renderSubject({ onOpenManagedFiles });

    await userEvent.setup().click(screen.getByTestId('open-managed-files'));

    expect(onOpenManagedFiles).toHaveBeenCalledTimes(1);
  });
});
