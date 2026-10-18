import { renderHook, act } from '@testing-library/react';
import { useWorkflowSelectionLogic, GuiRefusal } from './useWorkflowSelectionLogic';
import { WorkflowGUI, DEFAULT_WORKFLOW_GUI, DEFAULT_REUSABLE_WORKFLOW_GUI } from '../utils/workflowGuiConversion';
import { UnifiedWorkflowItem, RXWorkflow } from '../types/workflow';
import { createBlankWorkflow } from '../utils/workflowOperations';

// Mock the workflowGuiConversion module
vi.mock('../utils/workflowGuiConversion', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../utils/workflowGuiConversion')>();
  return {
    ...actual,
    yamlToGuiResult: vi.fn(),
  };
});

import { yamlToGuiResult, guiToYaml } from '../utils/workflowGuiConversion';
import * as yaml from 'js-yaml';

import type { Mock } from 'vitest';
// The hook converts through yamlToGuiResult so it can report a failure; these
// helpers keep the tests reading in terms of the model, not the wrapper shape.
const mockConversion = (gui: WorkflowGUI) =>
  (yamlToGuiResult as Mock).mockReturnValue({ gui, error: null });
const mockConversionFailure = (error: string) =>
  (yamlToGuiResult as Mock).mockReturnValue({ gui: DEFAULT_WORKFLOW_GUI, error });
const useRealConversion = async () => {
  const actual = await vi.importActual<typeof import('../utils/workflowGuiConversion')>(
    '../utils/workflowGuiConversion'
  );
  (yamlToGuiResult as Mock).mockImplementation(actual.yamlToGuiResult);
};

describe('useWorkflowSelectionLogic', () => {
  const mockSetSelectedWorkflowId = vi.fn();
  const mockSetGuiWorkflow = vi.fn();
  const mockSetRegularGuiWorkflow = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    mockConversion({ ...DEFAULT_WORKFLOW_GUI, name: 'Parsed Workflow' });
  });

  test('should initialize with correct props', () => {
    const unifiedWorkflows: UnifiedWorkflowItem[] = [];
    
    const { result } = renderHook(() =>
      useWorkflowSelectionLogic({
        unifiedWorkflows,
        setSelectedWorkflowId: mockSetSelectedWorkflowId,
        setGuiWorkflow: mockSetGuiWorkflow,
        setRegularGuiWorkflow: mockSetRegularGuiWorkflow
      })
    );

    expect(result.current.handleSelectWorkflow).toBeDefined();
    expect(typeof result.current.handleSelectWorkflow).toBe('function');
  });

  test('should handle selecting a regular workflow', () => {
    const unifiedWorkflows: UnifiedWorkflowItem[] = [
      {
        id: 'regular-0',
        name: 'Test Workflow',
        content: 'name: Test\non: push',
        isReusable: false,
        isModified: false,
        originalIndex: 0,
        type: 'regular'
      }
    ];

    const { result } = renderHook(() =>
      useWorkflowSelectionLogic({
        unifiedWorkflows,
        setSelectedWorkflowId: mockSetSelectedWorkflowId,
        setGuiWorkflow: mockSetGuiWorkflow,
        setRegularGuiWorkflow: mockSetRegularGuiWorkflow
      })
    );

    act(() => {
      result.current.handleSelectWorkflow('regular-0');
    });

    expect(mockSetSelectedWorkflowId).toHaveBeenCalledWith('regular-0');
    expect(yamlToGuiResult).toHaveBeenCalledWith('name: Test\non: push');
    expect(mockSetRegularGuiWorkflow).toHaveBeenCalled();
  });

  test('should handle selecting a reusable workflow', () => {
    const mockGuiWorkflow: WorkflowGUI = {
      ...DEFAULT_REUSABLE_WORKFLOW_GUI,
      name: 'Reusable Workflow'
    };
    
    mockConversion(mockGuiWorkflow);

    const unifiedWorkflows: UnifiedWorkflowItem[] = [
      {
        id: 'reusable-0',
        name: 'Reusable Workflow',
        content: 'name: Reusable\non:\n  workflow_call:',
        isReusable: true,
        isModified: false,
        originalIndex: 0,
        type: 'reusable'
      }
    ];

    const { result } = renderHook(() =>
      useWorkflowSelectionLogic({
        unifiedWorkflows,
        setSelectedWorkflowId: mockSetSelectedWorkflowId,
        setGuiWorkflow: mockSetGuiWorkflow,
        setRegularGuiWorkflow: mockSetRegularGuiWorkflow
      })
    );

    act(() => {
      result.current.handleSelectWorkflow('reusable-0');
    });

    expect(mockSetSelectedWorkflowId).toHaveBeenCalledWith('reusable-0');
    expect(mockSetGuiWorkflow).toHaveBeenCalled();
    expect(mockSetRegularGuiWorkflow).not.toHaveBeenCalled();
  });

  test('should handle selecting a workflow with empty content', () => {
    const unifiedWorkflows: UnifiedWorkflowItem[] = [
      {
        id: 'regular-1',
        name: 'Empty Workflow',
        content: '',
        isReusable: false,
        isModified: true,
        originalIndex: 1,
        type: 'regular'
      }
    ];

    const { result } = renderHook(() =>
      useWorkflowSelectionLogic({
        unifiedWorkflows,
        setSelectedWorkflowId: mockSetSelectedWorkflowId,
        setGuiWorkflow: mockSetGuiWorkflow,
        setRegularGuiWorkflow: mockSetRegularGuiWorkflow
      })
    );

    act(() => {
      result.current.handleSelectWorkflow('regular-1');
    });

    expect(mockSetSelectedWorkflowId).toHaveBeenCalledWith('regular-1');
    // Should preserve the workflow's name even if it's empty string
    expect(mockSetRegularGuiWorkflow).toHaveBeenCalledWith(
      expect.objectContaining({
        name: 'Empty Workflow'
      })
    );
  });

  test('should handle selecting non-existent workflow', () => {
    const unifiedWorkflows: UnifiedWorkflowItem[] = [];

    const { result } = renderHook(() =>
      useWorkflowSelectionLogic({
        unifiedWorkflows,
        setSelectedWorkflowId: mockSetSelectedWorkflowId,
        setGuiWorkflow: mockSetGuiWorkflow,
        setRegularGuiWorkflow: mockSetRegularGuiWorkflow
      })
    );

    act(() => {
      result.current.handleSelectWorkflow('non-existent');
    });

    expect(mockSetSelectedWorkflowId).toHaveBeenCalledWith('non-existent');
    expect(mockSetRegularGuiWorkflow).toHaveBeenCalledWith(DEFAULT_WORKFLOW_GUI);
  });

  test('should handle YAML parsing errors gracefully', () => {
    mockConversionFailure('Parse error');

    const unifiedWorkflows: UnifiedWorkflowItem[] = [
      {
        id: 'regular-0',
        name: 'Invalid YAML',
        content: 'invalid yaml content',
        isReusable: false,
        isModified: false,
        originalIndex: 0,
        type: 'regular'
      }
    ];

    const { result } = renderHook(() =>
      useWorkflowSelectionLogic({
        unifiedWorkflows,
        setSelectedWorkflowId: mockSetSelectedWorkflowId,
        setGuiWorkflow: mockSetGuiWorkflow,
        setRegularGuiWorkflow: mockSetRegularGuiWorkflow
      })
    );

    // Should not throw error but handle it gracefully
    act(() => {
      result.current.handleSelectWorkflow('regular-0');
    });

    expect(mockSetSelectedWorkflowId).toHaveBeenCalledWith('regular-0');
    // Should use the workflow's name even when parsing fails
    expect(mockSetRegularGuiWorkflow).toHaveBeenCalledWith(
      expect.objectContaining({
        name: 'Invalid YAML'
      })
    );
  });

  test('should add workflow_call event if missing for reusable workflows', () => {
    const mockGuiWithoutWorkflowCall: WorkflowGUI = {
      name: 'Reusable Without Call',
      events: [{ type: 'push' }],
      jobs: []
    };
    
    mockConversion(mockGuiWithoutWorkflowCall);

    const unifiedWorkflows: UnifiedWorkflowItem[] = [
      {
        id: 'reusable-0',
        name: 'Reusable Without Call',
        content: 'name: Test\non: push',
        isReusable: true,
        isModified: false,
        originalIndex: 0,
        type: 'reusable'
      }
    ];

    const { result } = renderHook(() =>
      useWorkflowSelectionLogic({
        unifiedWorkflows,
        setSelectedWorkflowId: mockSetSelectedWorkflowId,
        setGuiWorkflow: mockSetGuiWorkflow,
        setRegularGuiWorkflow: mockSetRegularGuiWorkflow
      })
    );

    act(() => {
      result.current.handleSelectWorkflow('reusable-0');
    });

    expect(mockSetGuiWorkflow).toHaveBeenCalledWith(
      expect.objectContaining({
        events: expect.arrayContaining([
          expect.objectContaining({ type: 'workflow_call' })
        ])
      })
    );
  });

  // workflow.name is the filename stem; the GUI name box edits the YAML label.
  test('seeds the GUI name from the YAML name: label, not the filename', async () => {
    await useRealConversion();
    const unifiedWorkflows: UnifiedWorkflowItem[] = [
      {
        id: 'regular-0',
        name: 'Custom Name',
        content: 'name: Different Name\non: push',
        isReusable: false,
        isModified: false,
        originalIndex: 0,
        type: 'regular'
      }
    ];

    const { result } = renderHook(() =>
      useWorkflowSelectionLogic({
        unifiedWorkflows,
        setSelectedWorkflowId: mockSetSelectedWorkflowId,
        setGuiWorkflow: mockSetGuiWorkflow,
        setRegularGuiWorkflow: mockSetRegularGuiWorkflow
      })
    );

    act(() => {
      result.current.handleSelectWorkflow('regular-0');
    });

    const callArgs = (mockSetRegularGuiWorkflow as Mock).mock.calls[0][0];
    expect(callArgs.name).toBe('Different Name');
  });

  test('a workflow with no name: label gains none from a GUI round trip', async () => {
    await useRealConversion();
    const unifiedWorkflows: UnifiedWorkflowItem[] = [
      {
        id: 'regular-0',
        name: 'build',
        content: 'on: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n',
        isReusable: false,
        isModified: false,
        originalIndex: 0,
        type: 'regular'
      }
    ];

    const { result } = renderHook(() =>
      useWorkflowSelectionLogic({
        unifiedWorkflows,
        setSelectedWorkflowId: mockSetSelectedWorkflowId,
        setGuiWorkflow: mockSetGuiWorkflow,
        setRegularGuiWorkflow: mockSetRegularGuiWorkflow
      })
    );

    act(() => {
      result.current.handleSelectWorkflow('regular-0');
    });

    const model = (mockSetRegularGuiWorkflow as Mock).mock.calls[0][0];
    expect(yaml.load(guiToYaml(model))).not.toHaveProperty('name');
  });

  test('should update when unifiedWorkflows prop changes', () => {
    const initialWorkflows: UnifiedWorkflowItem[] = [
      {
        id: 'regular-0',
        name: 'Workflow 1',
        content: 'name: Test 1',
        isReusable: false,
        isModified: false,
        originalIndex: 0,
        type: 'regular'
      }
    ];

    const { result, rerender } = renderHook(
      (props) => useWorkflowSelectionLogic(props),
      {
        initialProps: {
          unifiedWorkflows: initialWorkflows,
          setSelectedWorkflowId: mockSetSelectedWorkflowId,
          setGuiWorkflow: mockSetGuiWorkflow,
          setRegularGuiWorkflow: mockSetRegularGuiWorkflow
        }
      }
    );

    const updatedWorkflows: UnifiedWorkflowItem[] = [
      {
        id: 'regular-1',
        name: 'Workflow 2',
        content: 'name: Test 2',
        isReusable: false,
        isModified: false,
        originalIndex: 1,
        type: 'regular'
      }
    ];

    rerender({
      unifiedWorkflows: updatedWorkflows,
      setSelectedWorkflowId: mockSetSelectedWorkflowId,
      setGuiWorkflow: mockSetGuiWorkflow,
      setRegularGuiWorkflow: mockSetRegularGuiWorkflow
    });

    act(() => {
      result.current.handleSelectWorkflow('regular-1');
    });

    expect(mockSetSelectedWorkflowId).toHaveBeenCalledWith('regular-1');
  });

  test('should preserve empty workflow name for new blank workflows', () => {
    // This test ensures the fix for the name erasure bug
    // When a new blank workflow is created with empty name and content,
    // the GUI workflow should be initialized with the empty name, not 'CI'
    const unifiedWorkflows: UnifiedWorkflowItem[] = [
      {
        id: 'regular-0',
        name: '',
        content: '',
        isReusable: false,
        isModified: true,
        originalIndex: 0,
        type: 'regular'
      }
    ];

    const { result } = renderHook(() =>
      useWorkflowSelectionLogic({
        unifiedWorkflows,
        setSelectedWorkflowId: mockSetSelectedWorkflowId,
        setGuiWorkflow: mockSetGuiWorkflow,
        setRegularGuiWorkflow: mockSetRegularGuiWorkflow
      })
    );

    act(() => {
      result.current.handleSelectWorkflow('regular-0');
    });

    expect(mockSetSelectedWorkflowId).toHaveBeenCalledWith('regular-0');
    // Should use empty string, NOT default 'CI'
    expect(mockSetRegularGuiWorkflow).toHaveBeenCalledWith(
      expect.objectContaining({
        name: ''
      })
    );
  });

  // The mode switch writes the resulting model back over the document on the
  // first GUI edit, so it needs to know whether the conversion actually worked.
  describe('initializeWorkflowGUI conversion reporting', () => {
    const workflow = (overrides: Partial<UnifiedWorkflowItem> = {}): UnifiedWorkflowItem => ({
      id: 'regular-0',
      name: 'Test Workflow',
      content: 'name: Test\non: push',
      isReusable: false,
      isModified: false,
      originalIndex: 0,
      type: 'regular',
      ...overrides
    });

    const renderSubject = () =>
      renderHook(() =>
        useWorkflowSelectionLogic({
          unifiedWorkflows: [],
          setSelectedWorkflowId: mockSetSelectedWorkflowId,
          setGuiWorkflow: mockSetGuiWorkflow,
          setRegularGuiWorkflow: mockSetRegularGuiWorkflow
        })
      );

    const LOSSLESS_YAML =
      'name: Test\non: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: make\n';

    const initialize = (subject: UnifiedWorkflowItem) => {
      const { result } = renderSubject();
      let outcome: GuiRefusal | null | 'unset' = 'unset';
      act(() => {
        outcome = result.current.initializeWorkflowGUI(subject);
      });
      return outcome;
    };

    test('returns null when the GUI can edit the workflow without changing it', async () => {
      await useRealConversion();

      expect(initialize(workflow({ content: LOSSLESS_YAML }))).toBeNull();
      expect(mockSetRegularGuiWorkflow).toHaveBeenCalled();
    });

    test('returns the error when the YAML does not convert', () => {
      mockConversionFailure('bad indentation');

      expect(initialize(workflow({ content: 'name: [unclosed' }))).toEqual({
        reason: 'parse',
        error: 'bad indentation'
      });
    });

    test('returns null for a new workflow with no content', () => {
      expect(initialize(workflow({ content: '' }))).toBeNull();
    });

    test('returns null for whitespace-only content, as for empty', () => {
      expect(initialize(workflow({ content: '\n', isReusable: true, type: 'reusable' }))).toBeNull();
    });

    test('returns null for a new blank reusable workflow', async () => {
      await useRealConversion();
      const setRXWorkflows = vi.fn();
      createBlankWorkflow('reusable', 'shared', [], vi.fn(), setRXWorkflows, vi.fn());
      const [created] = (setRXWorkflows.mock.calls[0][0] as (prev: RXWorkflow[]) => RXWorkflow[])([]);

      expect(initialize(workflow({ content: created.content, isReusable: true, type: 'reusable' }))).toBeNull();
    });

    // yamlToGuiResult can hand back the shared default template itself.
    test('does not change the parsed model when giving a reusable workflow workflow_call', () => {
      const events = DEFAULT_WORKFLOW_GUI.events;
      mockConversion(DEFAULT_WORKFLOW_GUI);

      initialize(workflow({ content: 'name: Shared\non: push', isReusable: true, type: 'reusable' }));

      expect(DEFAULT_WORKFLOW_GUI.events).toBe(events);
      expect(DEFAULT_WORKFLOW_GUI.events.map(e => e.type)).not.toContain('workflow_call');
    });

    // Checked on the model as set, so the workflow_call a reusable workflow is
    // given counts as a change to its triggers.
    test('returns the paths a GUI round trip would change', async () => {
      await useRealConversion();

      expect(initialize(workflow({ content: LOSSLESS_YAML, isReusable: true, type: 'reusable' }))).toEqual({
        reason: 'lossy',
        paths: ['on.push', 'on.workflow_call']
      });
    });
  });
});
