import { handleWorkflowChange } from './workflowOperations';
import { Workflow, RXWorkflow, UnifiedWorkflowItem } from '../types/workflow';

import type { Mock } from 'vitest';
describe('workflowChangeHandlers', () => {
  describe('handleWorkflowChange', () => {
    let mockWorkflows: Workflow[];
    let mockRXWorkflows: RXWorkflow[];
    let mockSetWorkflows: Mock;
    let mockSetRXWorkflows: Mock;
    let mockMarkWorkflowAsModified: Mock;

    beforeEach(() => {
      mockWorkflows = [
        { name: 'test-workflow', content: 'name: test\non: [push]', isModified: false, isReusable: false }
      ];
      mockRXWorkflows = [
        { name: 'reusable-workflow', content: 'name: reusable\non: [workflow_call]', isModified: false, isReusable: true }
      ];
      // Mock setWorkflows to execute the functional updater (like React's useState)
      mockSetWorkflows = vi.fn((updater) => {
        if (typeof updater === 'function') {
          updater(mockWorkflows);
        }
      });
      mockSetRXWorkflows = vi.fn();
      mockMarkWorkflowAsModified = vi.fn();
    });

    test('updates regular workflow name using functional updater', () => {
      const selectedWorkflow: UnifiedWorkflowItem = {
        id: 'regular-0',
        name: 'test-workflow',
        content: 'name: test\non: [push]',
        type: 'regular',
        isReusable: false,
        originalIndex: 0,
        isModified: false
      };

      handleWorkflowChange({
        field: 'name',
        value: 'new-name',
        selectedWorkflow,
        workflows: mockWorkflows,
        setWorkflows: mockSetWorkflows,
        setRXWorkflows: mockSetRXWorkflows,
        markWorkflowAsModified: mockMarkWorkflowAsModified,
      });

      // setWorkflows should be called with a functional updater
      expect(mockSetWorkflows).toHaveBeenCalledWith(expect.any(Function));
      // Execute the updater to verify the result
      const updater = mockSetWorkflows.mock.calls[0][0];
      const result = updater(mockWorkflows);
      expect(result[0].name).toBe('new-name');
      expect(result[0].isModified).toBe(true);
      expect(mockMarkWorkflowAsModified).toHaveBeenCalledWith(0, 'regular');
    });

    test('functional updater preserves other fields when updating content', () => {
      // This is the key test for the stale closure fix.
      // Simulate a stale closure scenario: the workflows array passed to
      // handleWorkflowChange has name: '' (stale), but the actual current
      // state has name: 'MyWorkflow'. The functional updater should read
      // from the actual current state (prev), not the stale closure.
      const selectedWorkflow: UnifiedWorkflowItem = {
        id: 'regular-0',
        name: '',  // stale name from old closure
        content: '',
        type: 'regular',
        isReusable: false,
        originalIndex: 0,
        isModified: false
      };

      const staleWorkflows: Workflow[] = [
        { name: '', content: '', isModified: false, isReusable: false }
      ];

      const currentWorkflows: Workflow[] = [
        { name: 'MyWorkflow', content: '', isModified: true, isReusable: false }
      ];

      handleWorkflowChange({
        field: 'content',
        value: 'name: CI\non: [push]',
        selectedWorkflow,
        workflows: staleWorkflows,  // stale closure value
        setWorkflows: mockSetWorkflows,
        setRXWorkflows: mockSetRXWorkflows,
        markWorkflowAsModified: mockMarkWorkflowAsModified,
      });

      expect(mockSetWorkflows).toHaveBeenCalledWith(expect.any(Function));
      const updater = mockSetWorkflows.mock.calls[0][0];

      // When the updater runs with the CURRENT state, it should
      // preserve 'MyWorkflow' name and only update the content
      const result = updater(currentWorkflows);
      expect(result[0].name).toBe('MyWorkflow');  // preserved from current state!
      expect(result[0].content).toBe('name: CI\non: [push]');  // updated
      expect(result[0].isModified).toBe(true);
    });

    test('updates reusable workflow name', () => {
      const selectedWorkflow: UnifiedWorkflowItem = {
        id: 'reusable-0',
        name: 'reusable-workflow',
        content: 'name: reusable\non: [workflow_call]',
        type: 'reusable',
        isReusable: true,
        originalIndex: 0,
        isModified: false
      };

      // Mock setRXWorkflows to execute the callback function
      const mockSetRXWorkflowsWithCallback = vi.fn((callback) => {
        if (typeof callback === 'function') {
          callback(mockRXWorkflows);
        }
      });

      handleWorkflowChange({
        field: 'name',
        value: 'new-reusable-name',
        selectedWorkflow,
        workflows: mockWorkflows,
        setWorkflows: mockSetWorkflows,
        setRXWorkflows: mockSetRXWorkflowsWithCallback,
        markWorkflowAsModified: mockMarkWorkflowAsModified,
      });

      expect(mockSetRXWorkflowsWithCallback).toHaveBeenCalled();
      expect(mockMarkWorkflowAsModified).toHaveBeenCalledWith(0, 'reusable');
    });

    test('does nothing when selectedWorkflow is undefined', () => {
      handleWorkflowChange({
        field: 'name',
        value: 'new-name',
        selectedWorkflow: undefined,
        workflows: mockWorkflows,
        setWorkflows: mockSetWorkflows,
        setRXWorkflows: mockSetRXWorkflows,
        markWorkflowAsModified: mockMarkWorkflowAsModified,
      });

      expect(mockSetWorkflows).not.toHaveBeenCalled();
      expect(mockSetRXWorkflows).not.toHaveBeenCalled();
      expect(mockMarkWorkflowAsModified).not.toHaveBeenCalled();
    });
  });
});
