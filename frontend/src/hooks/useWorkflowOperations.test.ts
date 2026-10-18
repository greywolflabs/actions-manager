import { renderHook, act } from '@testing-library/react';
import { useWorkflowOperations, type UseWorkflowOperationsProps } from './useWorkflowOperations';
import { saveDraftWorkflow, commitAndUpdatePRWorkflow } from '../utils/workflowOperations';

vi.mock('../utils/workflowOperations', () => ({
  saveDraftWorkflow: vi.fn(),
  commitAndUpdatePRWorkflow: vi.fn(),
  commitAndUpdatePRLinkedWorkflow: vi.fn(),
  saveDraftLinkedWorkflow: vi.fn(),
  deleteWorkflow: vi.fn(),
  createBlankWorkflow: vi.fn(),
}));

const mockedSave = vi.mocked(saveDraftWorkflow);
const mockedCommitAndUpdatePR = vi.mocked(commitAndUpdatePRWorkflow);

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => { resolve = r; });
  return { promise, resolve };
}

const renderOperations = (refreshDrift: () => void) =>
  renderHook(() => useWorkflowOperations({
    workflows: [{ name: 'ci', content: 'on: push' }],
    rxworkflows: [],
    user: 'alice',
    projectName: 'proj',
    projectCode: 'P001',
    selectedRepos: ['acme/api'],
    regexPattern: '',
    detectedBuildTypes: [],
    refreshDrift,
  } as unknown as UseWorkflowOperationsProps));

describe('useWorkflowOperations — a save re-reads the drift it resolved', () => {
  beforeEach(() => vi.clearAllMocks());

  it('re-reads drift once Commit Locally has been saved, not before', async () => {
    const save = deferred<void>();
    mockedSave.mockReturnValue(save.promise);
    const refreshDrift = vi.fn();
    const { result } = renderOperations(refreshDrift);

    let saving!: Promise<void>;
    act(() => { saving = result.current.handleSaveDraftWorkflow(0, 'regular'); });
    // Asked before the save commits, the server would still report the drift.
    expect(refreshDrift).not.toHaveBeenCalled();

    await act(async () => { save.resolve(); await saving; });
    expect(refreshDrift).toHaveBeenCalledTimes(1);
  });

  it('re-reads drift after Commit and Update PR, even when the push fails', async () => {
    mockedCommitAndUpdatePR.mockResolvedValue(false);
    const refreshDrift = vi.fn();
    const { result } = renderOperations(refreshDrift);

    let pushed: boolean | undefined;
    await act(async () => { pushed = await result.current.handleCommitAndUpdatePR(0, 'regular'); });

    expect(pushed).toBe(false);
    expect(refreshDrift).toHaveBeenCalledTimes(1);
  });
});
