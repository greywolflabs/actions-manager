import React, { useCallback, useEffect, useRef, useState } from 'react';
import UnifiedWorkflowList from './UnifiedWorkflowList';
import UnifiedWorkflowEditor from './UnifiedWorkflowEditor';
import WorkflowCreationDialog from './WorkflowCreationDialog';
import TemplateSelectionModal from './TemplateSelectionModal';
import { handleWorkflowChange } from '../utils/workflowOperations';
import { useUnifiedWorkflowsState } from '../hooks/useUnifiedWorkflowsState';
import { useWorkflowOperations } from '../hooks/useWorkflowOperations';
import { useWorkflowSelectionLogic, GuiRefusal } from '../hooks/useWorkflowSelectionLogic';
import { useParentCallbacks } from '../hooks/useParentCallbacks';
import { UnifiedWorkflowsProps, UnifiedWorkflowItem, RXWorkflow, Workflow } from '../types/workflow';
import { CustomFilePanel } from './CustomFiles';
import CodeownersManager from './CodeownersManager';
import { CustomFile } from '../api/customFiles';
import { toast } from '../utils/toast';
import { planWorkflowKindMoves, type WorkflowKindChange } from '../utils/workflowKindChanges';
// eslint-disable-next-line no-restricted-imports -- Legacy: TODO migrate CSS file to Tailwind CSS classes
import '../styles/UnifiedWorkflows.css';
// eslint-disable-next-line no-restricted-imports -- Legacy: TODO migrate CSS file to Tailwind CSS classes
import '../styles/TemplateModal.css';

const MAX_NAMED_LOSSES = 3;

function guiRefusalMessage(refusal: GuiRefusal): string {
  if (refusal.reason === 'parse') {
    return 'Fix the YAML errors before switching to GUI mode.';
  }
  const named = refusal.paths.slice(0, MAX_NAMED_LOSSES).join(', ');
  const hidden = refusal.paths.length - MAX_NAMED_LOSSES;
  const more = hidden > 0 ? ` +${hidden} more` : '';
  return `GUI mode can't edit this workflow without changing it (${named}${more}). Keep editing in YAML.`;
}

const UnifiedWorkflows: React.FC<UnifiedWorkflowsProps> = ({
  user,
  projectName,
  projectCode,
  selectedRepos,
  regexPattern,
  accountType,
  usePrefix = true,
  isReadOnly = false,
  workflows,
  setWorkflows,
  onRefreshStatus,
  onAddWorkflow,
  onClearModifiedStates,
  onRegisterSelectionFollower,
  rxworkflows,
  setRXWorkflows,
  addWorkflowToMain,
  onGenerateTemplates,
  onAddRXWorkflow,
  detectedBuildTypes,
  reusableWorkflowsEnabled,
  projectType = 'standard',
  linkedWorkflows = [],
  setLinkedWorkflows,
  canLinkReusableWorkflows = false,
  onLinkReusableWorkflow,
  onImportExisting,
  refreshProjectsList,
  refreshProjectData,
  refreshDrift,
  onProjectStateChange,
  onReposDelivered,
  driftedWorkflowNames,
  customFiles = [],
  setCustomFiles,
  projectId,
  onCustomFilesChange,
  customFileDrift = [],
  driftedCustomFileIds,
  codeowners,
  codeownersDrift = [],
  onCodeownersSaved,
  codeownersOpenRequested = false,
  onCodeownersOpened,
  importedActions = [],
  actionGroups = [],
  secrets = [],
  envVars = [],
}) => {
  // Project Files: custom file and codeowners selection
  const [selectedCustomFileId, setSelectedCustomFileId] = useState<number | null>(null);
  const [codeownersSelected, setCodeownersSelected] = useState<boolean>(false);
  // -1 sentinel = "add new custom file" mode
  const ADD_CUSTOM_FILE_SENTINEL = -1;

  const handleCustomFilesChange = (updated: CustomFile[]) => {
    setCustomFiles?.(updated);
    onCustomFilesChange?.(updated);
  };

  // Use custom hooks for state management
  const state = useUnifiedWorkflowsState(workflows, setWorkflows, setRXWorkflows);

  // Read after a save's await: by then the edits it carried have rendered.
  const listsRef = useRef({ workflows, rxworkflows, selectedId: state.selectedWorkflowId });
  listsRef.current = { workflows, rxworkflows, selectedId: state.selectedWorkflowId };

  // Keeps the editor on the workflow it was showing when a save moves workflows
  // between sections (#2318). Called before that move renders, so the lists
  // here are still the ones the selection points into. The project save calls
  // it too: it moves its own lists, since this may not be mounted to do it.
  const { setSelectedWorkflowId } = state;
  const followKindChanges = useCallback((changes: WorkflowKindChange[]) => {
    const { workflows: regular, rxworkflows: reusable, selectedId } = listsRef.current;
    const moves = planWorkflowKindMoves<Workflow>(regular, reusable, changes, selectedId);
    if (moves) setSelectedWorkflowId(moves.selectedId);
  }, [setSelectedWorkflowId]);

  // The editor's own save moves the workflows here.
  const applyKindChanges = useCallback((changes: WorkflowKindChange[]) => {
    const { workflows: regular, rxworkflows: reusable } = listsRef.current;
    const moves = planWorkflowKindMoves<Workflow>(regular, reusable, changes, null);
    if (!moves) return;
    followKindChanges(changes);
    setWorkflows(prev => moves.nextRegular(prev));
    // A workflow moved in from the regular section was just saved, so it has content.
    setRXWorkflows(prev => moves.nextReusable(prev) as RXWorkflow[]);
  }, [followKindChanges, setWorkflows, setRXWorkflows]);

  useEffect(() => {
    onRegisterSelectionFollower?.(followKindChanges);
    return () => onRegisterSelectionFollower?.(null);
  }, [onRegisterSelectionFollower, followKindChanges]);

  const handleSelectCustomFile = (id: number) => {
    setSelectedCustomFileId(id);
    state.setSelectedWorkflowId(null);
    setCodeownersSelected(false);
  };

  const handleSelectCodeowners = useCallback(() => {
    setCodeownersSelected(true);
    state.setSelectedWorkflowId(null);
    setSelectedCustomFileId(null);
  }, [state.setSelectedWorkflowId]);

  // Asked for while this view may not have been mounted, so it is a flag the
  // page clears once handled rather than a counter that only a change reveals.
  useEffect(() => {
    if (!codeownersOpenRequested) return;
    handleSelectCodeowners();
    onCodeownersOpened?.();
  }, [codeownersOpenRequested, handleSelectCodeowners, onCodeownersOpened]);
  
  // Create unified workflow list
  const unifiedWorkflows: UnifiedWorkflowItem[] = [
    ...workflows.map((workflow, index) => ({
      id: `regular-${index}`,
      name: workflow.name,
      content: workflow.content,
      isReusable: false,
      isModified: workflow.isModified,
      gitHash: workflow.gitHash,
      workflowStatus: workflow.workflowStatus,
      originalIndex: index,
      type: 'regular' as const,
      savedName: workflow.savedName,
      lastModifiedBy: workflow.lastModifiedBy,
    })),
    // Always listed. A reusable workflow the project owns is a file the user
    // saved or imported here, so hiding it behind the authoring toggle only
    // made it unreachable - it stayed in the database, invisible.
    ...rxworkflows.map((workflow, index) => ({
      id: `reusable-${index}`,
      name: workflow.name,
      content: workflow.content,
      isReusable: true,
      isModified: workflow.isModified,
      gitHash: workflow.gitHash,
      workflowStatus: workflow.workflowStatus,
      originalIndex: index,
      type: 'reusable' as const,
      savedName: workflow.savedName,
      lastModifiedBy: workflow.lastModifiedBy,
    })),
    ...linkedWorkflows.map((workflow, index) => ({
      id: `linked-${workflow.workflow_id}`,
      name: workflow.workflow_name,
      content: workflow.workflow_yaml,
      isReusable: true,
      isModified: workflow.isModified ?? false,
      gitHash: undefined,
      workflowStatus: workflow.workflowStatus,
      originalIndex: index,
      type: 'linked' as const,
      rwxProjectId: workflow.rwx_project_id,
      rwxProjectName: workflow.rwx_project_name,
      rwxRepo: workflow.rwx_repo
    }))
  ];

  const selectedWorkflow = unifiedWorkflows.find(w => w.id === state.selectedWorkflowId);

  // Use workflow operations hook
  const operations = useWorkflowOperations({
    workflows,
    rxworkflows,
    linkedWorkflows,
    setLinkedWorkflows,
    user,
    projectName,
    projectCode,
    selectedRepos,
    regexPattern,
    accountType,
    detectedBuildTypes,
    setWorkflows,
    setRXWorkflows,
    setSelectedWorkflowId: state.setSelectedWorkflowId,
    markWorkflowAsSaved: state.markWorkflowAsSaved,
    setDetectedBuildTypes: state.setDetectedBuildTypes,
    setShowDetectionResults: state.setShowDetectionResults,
    setShowDetectionResultsInModal: state.setShowDetectionResultsInModal,
    setTemplatesByType: state.setTemplatesByType,
    setShowTemplateModal: state.setShowTemplateModal,
    setShowWorkflowCreationDialog: state.setShowWorkflowCreationDialog,
    setWorkflowCreationType: state.setWorkflowCreationType,
    setEditingWorkflowIndex: state.setEditingWorkflowIndex,
    setEditingWorkflowType: state.setEditingWorkflowType,
    setWorkflowsCount: state.setWorkflowsCount,
    setIsDetecting: state.setIsDetecting,
    setIsGeneratingTemplates: state.setIsGeneratingTemplates,
    refreshProjectsList,
    refreshProjectData,
    refreshDrift,
    onProjectStateChange,
    onReposDelivered,
    projectType,
    applyKindChanges,
  });

  // Use workflow selection logic hook
  const { handleSelectWorkflow: _handleSelectWorkflow, initializeWorkflowGUI } = useWorkflowSelectionLogic({
    unifiedWorkflows,
    setSelectedWorkflowId: state.setSelectedWorkflowId,
    setGuiWorkflow: state.setGuiWorkflow,
    setRegularGuiWorkflow: state.setRegularGuiWorkflow
  });

  // Wrap to also clear non-workflow selections.
  //
  // Selecting a workflow while already in GUI mode is also a way into GUI
  // editing, so it gets the same refusal as the mode switch, by dropping to
  // YAML. Linked workflows are exempt: they only ever render as read-only YAML.
  const handleSelectWorkflow = (workflowId: string) => {
    setSelectedCustomFileId(null);
    setCodeownersSelected(false);
    const refusal = _handleSelectWorkflow(workflowId);
    const isLinked = unifiedWorkflows.find(w => w.id === workflowId)?.type === 'linked';
    if (refusal && state.editMode === 'gui' && !isLinked) {
      state.setEditMode('yaml');
      toast.error(guiRefusalMessage(refusal));
    }
  };

  // The GUI model is only built when a workflow is selected, so entering GUI
  // mode has to re-read the current YAML. Without this the panel shows the
  // workflow as it was at selection time, and the first GUI edit serialises that
  // stale model back over every YAML edit made since.
  //
  // Stay in, or drop to, YAML if the YAML no longer converts, or if the GUI
  // can't represent all of it: the next GUI edit would write a default template,
  // or a model missing those parts, over the document. Clicking GUI while
  // already in GUI mode has just loaded that model, so it must drop too.
  const handleSetEditMode = (mode: 'yaml' | 'gui') => {
    if (mode === 'gui' && selectedWorkflow) {
      const refusal = initializeWorkflowGUI(selectedWorkflow);
      if (refusal) {
        state.setEditMode('yaml');
        toast.error(guiRefusalMessage(refusal));
        return;
      }
    }
    state.setEditMode(mode);
  };

  // Workflow change handler
  const handleWorkflowChangeWrapper = (field: string, value: string) => {
    handleWorkflowChange({
      field,
      value,
      selectedWorkflow,
      workflows,
      setWorkflows,
      setRXWorkflows,
      markWorkflowAsModified: state.markWorkflowAsModified,
      setLinkedWorkflows,
    });
  };

  // Use parent callbacks hook
  useParentCallbacks({
    onAddWorkflow,
    onAddRXWorkflow,
    onGenerateTemplates,
    onClearModifiedStates,
    openWorkflowCreationDialog: operations.openWorkflowCreationDialog,
    selectWorkflowType: operations.selectWorkflowType,
    handleGenerateTemplates: operations.handleGenerateTemplates,
    isGeneratingTemplates: state.isGeneratingTemplates,
    setModifiedWorkflows: state.setModifiedWorkflows,
    setModifiedRXWorkflows: state.setModifiedRXWorkflows
  });

  // Initialize workflows count for free accounts
  useEffect(() => {
    operations.fetchWorkflowsCount();
  }, []);

  // Initialize GUI state when a workflow is selected
  // This ensures that when createBlankWorkflow sets selectedWorkflowId,
  // the GUI state is properly initialized with the workflow's name
  useEffect(() => {
    if (state.selectedWorkflowId && selectedWorkflow) {
      handleSelectWorkflow(state.selectedWorkflowId);
    }
    // Only run when selectedWorkflowId changes, not on every render
  }, [state.selectedWorkflowId]);

  // Right panel — switches based on what is selected
  let editorPanel: React.ReactNode;
  if (selectedCustomFileId !== null) {
    editorPanel = (
      <div className="unified-workflows-editor">
        <CustomFilePanel
          cf={selectedCustomFileId === ADD_CUSTOM_FILE_SENTINEL
            ? null
            : (customFiles.find(f => f.id === selectedCustomFileId) ?? null)}
          allFiles={customFiles}
          projectId={projectId ?? 0}
          githubUser={user}
          onChange={handleCustomFilesChange}
          onAfterAdd={(newId) => setSelectedCustomFileId(newId)}
          onRemoved={() => setSelectedCustomFileId(null)}
          onProjectStateChange={onProjectStateChange}
          projectName={projectName}
          selectedRepos={selectedRepos}
          onReposDelivered={onReposDelivered}
          drift={customFileDrift.filter(d => d.custom_file_id === selectedCustomFileId)}
        />
      </div>
    );
  } else if (codeownersSelected) {
    editorPanel = (
      <div className="unified-workflows-editor overflow-auto">
        <CodeownersManager
          user={user}
          projectName={projectName}
          projectId={projectId ?? 0}
          selectedRepos={selectedRepos}
          isReadOnly={isReadOnly}
          codeowners={codeowners ?? null}
          drift={codeownersDrift}
          onSaved={onCodeownersSaved}
          onReposDelivered={onReposDelivered}
        />
      </div>
    );
  } else {
    editorPanel = (
      <UnifiedWorkflowEditor
        selectedWorkflow={selectedWorkflow}
        editMode={state.editMode}
        regularGuiWorkflow={state.regularGuiWorkflow}
        guiWorkflow={state.guiWorkflow}
        projectCode={projectCode}
        usePrefix={usePrefix}
        isReadOnly={isReadOnly}
        user={user}
        projectName={projectName}
        selectedRepos={selectedRepos}
        importedActions={importedActions}
        actionGroups={actionGroups}
        secrets={secrets}
        envVars={envVars}
        setEditMode={handleSetEditMode}
        setRegularGuiWorkflow={state.setRegularGuiWorkflow}
        setGuiWorkflow={state.setGuiWorkflow}
        handleWorkflowChange={handleWorkflowChangeWrapper}
        saveDraftWorkflow={operations.handleSaveDraftWorkflow}
        saveDraftLinkedWorkflow={operations.handleSaveDraftLinkedWorkflow}
        commitAndUpdatePR={operations.handleCommitAndUpdatePR}
        commitAndUpdatePRLinked={operations.handleCommitAndUpdatePRLinked}
        deleteWorkflow={operations.handleDeleteWorkflow}
        unlinkWorkflow={operations.handleUnlinkWorkflow}
        addWorkflowFn={operations.openWorkflowCreationDialog}
        onImportExisting={onImportExisting}
        projectType={projectType}
        onVersionRestored={refreshDrift}
      />
    );
  }

  return (
    <div className="unified-workflows-container">
      {/* Unified file list on the left — workflows + custom files + CODEOWNERS */}
      <UnifiedWorkflowList
        unifiedWorkflows={unifiedWorkflows}
        selectedWorkflowId={state.selectedWorkflowId}
        isCollapsed={state.isCollapsed}
        projectCode={projectCode}
        usePrefix={usePrefix}
        selectedRepos={selectedRepos}
        projectType={projectType}
        setIsCollapsed={state.setIsCollapsed}
        handleSelectWorkflow={handleSelectWorkflow}
        addWorkflowFn={operations.openWorkflowCreationDialog}
        driftedWorkflowNames={driftedWorkflowNames}
        customFiles={customFiles}
        selectedCustomFileId={selectedCustomFileId}
        onSelectCustomFile={handleSelectCustomFile}
        driftedCustomFileIds={driftedCustomFileIds}
        codeowners={codeowners}
        codeownersSelected={codeownersSelected}
        codeownersDrifted={codeownersDrift.length > 0}
        onSelectCodeowners={handleSelectCodeowners}
      />

      {editorPanel}
      
      {/* Workflow Creation Dialog */}
      <WorkflowCreationDialog
        showWorkflowCreationDialog={state.showWorkflowCreationDialog}
        workflowCreationType={state.workflowCreationType}
        showDetectionResultsInModal={state.showDetectionResultsInModal}
        isDetecting={state.isDetecting}
        detectedBuildTypesState={state.detectedBuildTypesState}
        isGeneratingTemplates={state.isGeneratingTemplates}
        reusableWorkflowsEnabled={reusableWorkflowsEnabled}
        showLinkReusableWorkflow={canLinkReusableWorkflows}
        existingWorkflowNames={[...workflows.map(workflow => workflow.name), ...rxworkflows.map(workflow => workflow.name)]}
        selectedRepos={selectedRepos}
        onAddCustomFile={() => handleSelectCustomFile(ADD_CUSTOM_FILE_SENTINEL)}
        onSelectCodeowners={handleSelectCodeowners}
        setShowWorkflowCreationDialog={state.setShowWorkflowCreationDialog}
        selectWorkflowType={operations.selectWorkflowType}
        onLinkReusableWorkflow={onLinkReusableWorkflow}
        createBlankWorkflow={operations.handleCreateBlankWorkflow}
        handleDetectBuildTypes={operations.handleDetectBuildTypes}
        handleCreateFromTemplates={operations.handleCreateFromTemplates}
        addWorkflowFromDetection={operations.handleAddWorkflowFromDetection}
        setWorkflowCreationType={state.setWorkflowCreationType}
        setShowDetectionResultsInModal={state.setShowDetectionResultsInModal}
        setDetectedBuildTypes={state.setDetectedBuildTypes}
      />
      
      {/* Template Selection Modal */}
      <TemplateSelectionModal
        showTemplateModal={state.showTemplateModal}
        templatesByType={state.templatesByType}
        setShowTemplateModal={state.setShowTemplateModal}
        selectTemplate={operations.handleSelectTemplate}
      />
    </div>
  );
};

export default UnifiedWorkflows;
