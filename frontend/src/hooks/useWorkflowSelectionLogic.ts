import { useCallback } from 'react';
import {
  yamlToGuiResult,
  findGuiRoundTripLosses,
  WorkflowGUI,
  DEFAULT_REUSABLE_WORKFLOW_GUI,
  DEFAULT_WORKFLOW_GUI
} from '../utils/workflowGuiConversion';
import { UnifiedWorkflowItem } from '../types/workflow';

/** Why the loaded model must not be edited in GUI mode. */
export type GuiRefusal =
  | { reason: 'parse'; error: string }
  | { reason: 'lossy'; paths: string[] };

export interface UseWorkflowSelectionLogicProps {
  unifiedWorkflows: UnifiedWorkflowItem[];
  setSelectedWorkflowId: (id: string) => void;
  setGuiWorkflow: (workflow: WorkflowGUI) => void;
  setRegularGuiWorkflow: (workflow: WorkflowGUI) => void;
}

export const useWorkflowSelectionLogic = ({
  unifiedWorkflows,
  setSelectedWorkflowId,
  setGuiWorkflow,
  setRegularGuiWorkflow
}: UseWorkflowSelectionLogicProps) => {
  
  // Empty template carrying the workflow's own name — used for new workflows and
  // whenever the YAML can't be converted. The name is applied as-is, even when
  // empty, rather than falling back to the template's default.
  const applyDefaultModel = useCallback((workflow: UnifiedWorkflowItem) => {
    if (workflow.isReusable) {
      setGuiWorkflow({ ...DEFAULT_REUSABLE_WORKFLOW_GUI, name: workflow.name });
    } else {
      setRegularGuiWorkflow({ ...DEFAULT_WORKFLOW_GUI, name: workflow.name });
    }
  }, [setGuiWorkflow, setRegularGuiWorkflow]);

  // gui.name is the YAML `name:` label, not workflow.name: that is the filename
  // stem, and seeding the label from it rewrote `name:` on the first GUI edit.
  // Returns the model as set. It is a copy when adjusted, never the parsed
  // object, which can be the shared default template.
  const applyParsedModel = useCallback((workflow: UnifiedWorkflowItem, gui: WorkflowGUI): WorkflowGUI => {
    if (!workflow.isReusable) {
      setRegularGuiWorkflow(gui);
      return gui;
    }

    const model = gui.events.some(e => e.type === 'workflow_call')
      ? gui
      : { ...gui, events: [{ type: 'workflow_call' as const, inputs: {} }] };
    setGuiWorkflow(model);
    return model;
  }, [setGuiWorkflow, setRegularGuiWorkflow]);

  /**
   * Rebuilds the GUI model from a workflow's YAML.
   *
   * Returns why GUI mode must not edit the result, or `null` when it may. The
   * first GUI edit writes the model back over the whole document, so a caller
   * entering GUI mode refuses on a conversion error (the model is a default
   * template) and on a lossy round trip (the model can't represent part of the
   * YAML). The check runs on the model exactly as set, after the reusable
   * workflow_call adjustment.
   */
  const initializeWorkflowGUI = useCallback((workflow: UnifiedWorkflowItem): GuiRefusal | null => {
    // New workflow with nothing in it yet.
    if (!workflow.content?.trim()) {
      applyDefaultModel(workflow);
      return null;
    }

    try {
      const { gui, error } = yamlToGuiResult(workflow.content);
      if (error) {
        applyDefaultModel(workflow);
        return { reason: 'parse', error };
      }
      const model = applyParsedModel(workflow, gui);
      const paths = findGuiRoundTripLosses(workflow.content, model);
      return paths.length > 0 ? { reason: 'lossy', paths } : null;
    } catch (error) {
      console.warn('Failed to convert YAML to GUI:', error);
      applyDefaultModel(workflow);
      return {
        reason: 'parse',
        error: error instanceof Error ? error.message : 'Failed to convert YAML to GUI.'
      };
    }
  }, [applyDefaultModel, applyParsedModel]);

  const handleSelectWorkflow = useCallback((workflowId: string): GuiRefusal | null => {
    setSelectedWorkflowId(workflowId);
    const workflow = unifiedWorkflows.find(w => w.id === workflowId);
    if (workflow) {
      return initializeWorkflowGUI(workflow);
    }
    setRegularGuiWorkflow(DEFAULT_WORKFLOW_GUI);
    return null;
  }, [unifiedWorkflows, setSelectedWorkflowId, initializeWorkflowGUI, setRegularGuiWorkflow]);

  return { handleSelectWorkflow, initializeWorkflowGUI };
};