import type { WorkflowKindChange } from "../utils/workflowKindChanges";

// Workflow type definitions extracted from UnifiedWorkflows.tsx
import { RwxWorkflow } from '../api/projects';

export interface Workflow {
  name: string;
  content?: string;
  isReusable?: boolean;
  isModified?: boolean;
  gitHash?: string;
  workflowStatus?: string;
  /** Persisted name as stored in the database. Used to detect renames and pass `original_name` to the backend. */
  savedName?: string;
  /** GitHub username of the last user who saved this workflow. */
  lastModifiedBy?: string;
}

export interface RXWorkflow {
  name: string;
  content: string;
  isReusable: boolean;
  isModified?: boolean;
  gitHash?: string;
  workflowStatus?: string;
  /** Persisted name as stored in the database. Used to detect renames and pass `original_name` to the backend. */
  savedName?: string;
  /** GitHub username of the last user who saved this workflow. */
  lastModifiedBy?: string;
}

export interface BuildType {
  name: string;
  technology: string;
  confidence: number;
}

export interface DetectedBuildResult {
  repo: string;
  detected_build_types?: BuildType[];
  error?: string;
}

export interface WorkflowTemplate {
  name: string;
  content: string;
}

export interface TemplatesByType {
  standard?: WorkflowTemplate;
  reusable?: WorkflowTemplate;
  build?: WorkflowTemplate;
}

export interface WorkflowNames {
  reusable: string;
  caller: string;
  technology: string | null;
}

export interface UnifiedWorkflowItem {
  id: string;
  name: string;
  content?: string;
  isReusable: boolean;
  isModified?: boolean;
  gitHash?: string;
  workflowStatus?: string;
  originalIndex: number;
  type: 'regular' | 'reusable' | 'linked';
  /** Persisted name as stored in the database, so the editor can tell a rename from a first save. */
  savedName?: string;
  /** For linked workflows: the source RWX project id */
  rwxProjectId?: number;
  /** For linked workflows: the source RWX project name */
  rwxProjectName?: string;
  /** For linked workflows: first repo of the source RWX project ("owner/repo"). Used for "Open in GitHub". */
  rwxRepo?: string;
  /** GitHub username of the last user who saved this workflow. */
  lastModifiedBy?: string;
}

/** 'deleting': the project's removal pull requests are open, and it is read-only until they merge. */
export type ProjectPRState = 'new' | 'draft' | 'open' | 'synced' | 'deleting';

export interface UnifiedWorkflowsProps {
  // Common props
  user: string;
  projectName: string;
  projectCode: string | null;
  selectedRepos: string[];
  regexPattern: string;
  accountType?: string;
  usePrefix?: boolean; // Whether to use AM_{PROJECT_CODE}_ prefix
  isReadOnly?: boolean; // Whether the current user has read-only access

  // Regular workflows props
  workflows: Workflow[];
  setWorkflows: (workflows: Workflow[] | ((prev: Workflow[]) => Workflow[])) => void;
  onRefreshStatus?: (refreshFn: () => Promise<void>, isLoading: boolean) => void;
  onAddWorkflow?: (addWorkflowFn: () => void) => void;
  onClearModifiedStates?: (clearFn: () => void) => void;
  /**
   * Hands the parent the function that keeps the editor on a workflow the parent
   * is about to move between sections (#2318), or null once this unmounts.
   */
  onRegisterSelectionFollower?: (followFn: ((changes: WorkflowKindChange[]) => void) | null) => void;

  // Reusable workflows props
  rxworkflows: RXWorkflow[];
  setRXWorkflows: (workflows: RXWorkflow[] | ((prev: RXWorkflow[]) => RXWorkflow[])) => void;
  addWorkflowToMain: (workflow: { name: string; content: string; isReusable: boolean }) => void;
  onGenerateTemplates: (generateFn: () => Promise<void>, isGenerating: boolean) => void;
  onAddRXWorkflow: (addFn: () => void) => void;
  detectedBuildTypes: DetectedBuildResult[];
  
  // Whether *new* reusable workflows can be authored here. It never gates
  // whether the reusable workflows a project already owns are listed - see
  // UnifiedWorkflows.
  reusableWorkflowsEnabled: boolean;
  /** Project type, so a reusable workflow inside a caller project can be flagged. */
  projectType?: 'standard' | 'rwx';

  // Linked reusable workflows (standard projects only)
  linkedWorkflows?: RwxWorkflow[];
  setLinkedWorkflows?: (updater: (prev: RwxWorkflow[]) => RwxWorkflow[]) => void;
  canLinkReusableWorkflows?: boolean;
  onLinkReusableWorkflow?: () => void;
  // Opens the existing-workflow import panel — surfaced in the editor's empty state
  onImportExisting?: () => void;

  // Callback to refresh project list
  refreshProjectsList?: () => Promise<void>;
  /** Reloads the project itself (not just the list), so drift names and other
   *  state seeded by the project-load endpoint are corrected after a removal. */
  refreshProjectData?: () => Promise<void>;
  /** Re-reads the stored drift after a save, which resolves the edited workflow's drift. */
  refreshDrift?: () => void;
  // Callback to notify parent when project PR state changes
  onProjectStateChange?: (state: string) => void;
  /** Repositories a push opened or added to a pull request in, so they stop waiting for delivery. */
  onReposDelivered?: (repos: string[]) => void;
  // Set of workflow names currently drifted on GitHub (rendered as a badge in the list)
  driftedWorkflowNames?: Set<string>;

  // Project Files: custom files
  customFiles?: import('../api/customFiles').CustomFile[];
  setCustomFiles?: (files: import('../api/customFiles').CustomFile[]) => void;
  projectId?: number;
  onCustomFilesChange?: (files: import('../api/customFiles').CustomFile[]) => void;
  /** Each (repo, branch) where the last drift check found a custom file changed on GitHub. */
  customFileDrift?: import('../api/drift').CustomFileDriftDetail[];
  /** Ids of the drifted custom files; seeded by the project load until the drift check has loaded. */
  driftedCustomFileIds?: Set<number>;
  /** The project's CODEOWNERS file, null when it has none, undefined until loaded. */
  codeowners?: import('../api/codeowners').CodeownersRecord | null;
  /** Each (repo, branch) where the last drift check found CODEOWNERS changed on GitHub. */
  codeownersDrift?: import('../api/drift').CodeownersDriftDetail[];
  /** Called with what a Commit Locally saved and the project's resulting pr_state. */
  onCodeownersSaved?: (record: import('../api/codeowners').CodeownersRecord, prState?: string) => void;
  /** Asks the Project Files view to open the CODEOWNERS editor, e.g. from the drift panel. */
  codeownersOpenRequested?: boolean;
  /** Called once the CODEOWNERS editor has been opened for that request. */
  onCodeownersOpened?: () => void;

  /** Shared, workspace-wide catalog of imported Actions Projects, surfaced in the GUI step editor. */
  importedActions?: import('../api/actionsProjects').ActionsProject[];
  /** Shared, workspace-wide Action Groups, used to filter the GUI step editor's action picker. */
  actionGroups?: import('../api/actionGroups').ActionGroup[];

  /** Project secrets, surfaced in the editor's resource picker. Names only — values are never carried here. */
  secrets?: Array<{ secret_key?: string; name?: string; repo?: string }>;
  /** Project variables, surfaced in the editor's resource picker. Names only. */
  envVars?: Array<{ env_key?: string; repo?: string }>;
}
